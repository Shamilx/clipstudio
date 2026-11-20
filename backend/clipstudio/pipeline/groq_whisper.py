"""Hosted transcription through Groq's Whisper (OpenAI-compatible API).

Why hosted: the target machine is a weak CPU with no useful GPU, where local
Whisper would take longer than the video itself. Groq's ``whisper-large-v3-turbo``
transcribes an hour of audio in well under a minute.

Audio above the upload limit is split into chunks; every chunk's timestamps are
shifted by its offset, so the result is one continuous :class:`Transcript`.
Requests are retried with backoff on 429 / 5xx / network errors.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .. import activity
from ..config import GROQ_BASE_URL, WhisperSettings
from . import ffmpeg
from .transcript import Segment, Transcript, Word

log = logging.getLogger(__name__)

#: Groq's upload limit is 25 MB on the free tier; stay clear of it.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
#: Fallback bytes/second when the real file size is unknown (16 kHz mono PCM).
WAV_BYTES_PER_SECOND = 32_000

MAX_ATTEMPTS = 6
REQUEST_TIMEOUT_S = 300.0


class GroqError(RuntimeError):
    """Groq transcription failed. The message is meant for the end user."""


def plan_chunks(duration_s: float, *, file_bytes: int) -> list[tuple[float, float]]:
    """Return ``(offset, length)`` pairs covering the audio.

    A file under the limit is a single chunk. Otherwise chunks are sized from the
    file's own bytes-per-second so each stays under the upload limit.
    """
    if duration_s <= 0 or file_bytes <= MAX_UPLOAD_BYTES:
        return [(0.0, duration_s)]
    bytes_per_second = file_bytes / duration_s if file_bytes else WAV_BYTES_PER_SECOND
    chunk_s = float(int(MAX_UPLOAD_BYTES * 0.9 / bytes_per_second))
    chunks: list[tuple[float, float]] = []
    offset = 0.0
    while offset < duration_s - 0.01:
        length = min(chunk_s, duration_s - offset)
        chunks.append((offset, length))
        offset += length
    return chunks


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        header = response.headers.get("retry-after")
        if header:
            try:
                return min(90.0, max(1.0, float(header)))
            except ValueError:
                pass
    return min(60.0, 2.0 * (2**attempt))


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error", body)
        return str(error.get("message", error))[:300] if isinstance(error, dict) else str(error)
    except ValueError:
        return response.text[:300]


def _wait(sleep: Callable[[float], None], delay: float, message: str) -> None:
    with activity.waiting_sync(message):
        sleep(delay)


def _post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    api_key: str,
    audio: Path,
    data: dict[str, Any],
    cancelled: Callable[[], bool] | None,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "Transcribing with Groq Whisper",
) -> dict[str, Any]:
    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        if cancelled is not None and cancelled():
            raise GroqError("Transcription cancelled.")
        response: httpx.Response | None = None
        started = time.monotonic()
        try:
            with activity.waiting_sync(label), audio.open("rb") as handle:
                response = client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data=data,
                    files={"file": (audio.name, handle, "application/octet-stream")},
                )
        except httpx.HTTPError as exc:
            last_error = f"network error: {exc}"
            delay = _retry_delay(None, attempt)
            log.warning(
                "Groq request failed after %.1fs (%s); waiting %.0fs, retry %d.",
                time.monotonic() - started,
                exc,
                delay,
                attempt + 1,
            )
            _wait(sleep, delay, f"Groq network error — retrying in {delay:.0f}s")
            continue

        log.info(
            "Groq request %s (%.1f MB): HTTP %d in %.1fs (attempt %d)",
            audio.name,
            audio.stat().st_size / 1e6,
            response.status_code,
            time.monotonic() - started,
            attempt + 1,
        )
        if response.status_code == 200:
            return response.json()

        detail = _error_detail(response)
        if response.status_code in (401, 403):
            raise GroqError(
                "Groq rejected the API key. Check GROQ_API_KEY in the .env file."
            )
        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"{response.status_code}: {detail}"
            delay = _retry_delay(response, attempt)
            log.warning("Groq returned %s; waiting %.0fs.", response.status_code, delay)
            why = "rate limit" if response.status_code == 429 else f"HTTP {response.status_code}"
            _wait(sleep, delay, f"Groq {why} — retrying in {delay:.0f}s")
            continue
        raise GroqError(f"Groq transcription error ({response.status_code}): {detail}")

    raise GroqError(
        "Groq transcription failed after several attempts "
        f"(last error: {last_error}). If you hit a rate limit, wait a minute and retry the job."
    )


def map_response(payload: dict[str, Any], offset_s: float, transcript: Transcript) -> None:
    """Append one verbose_json response to ``transcript``, shifted by ``offset_s``."""
    first_word = len(transcript.words)
    for raw in payload.get("words") or []:
        text = str(raw.get("word", "")).strip()
        if not text:
            continue
        start = float(raw.get("start", 0.0)) + offset_s
        end = max(start, float(raw.get("end", 0.0)) + offset_s)
        transcript.words.append(Word(text=text, start=start, end=end))
    if len(transcript.words) == first_word:
        return

    cursor = first_word
    segments = payload.get("segments") or []
    for raw in segments:
        seg_start = float(raw.get("start", 0.0)) + offset_s
        seg_end = float(raw.get("end", 0.0)) + offset_s
        first = cursor
        # A word belongs to the segment that holds its midpoint.
        while cursor < len(transcript.words):
            word = transcript.words[cursor]
            if (word.start + word.end) / 2 > seg_end + 1e-3:
                break
            cursor += 1
        if cursor == first:
            continue  # segment with no timed words (silence / music)
        transcript.segments.append(
            Segment(
                text=str(raw.get("text", "")).strip(),
                start=seg_start,
                end=seg_end,
                first_word=first,
                last_word=cursor - 1,
            )
        )

    # Words after the final segment (or a response without segments) still need
    # a home so every word index is covered.
    if cursor < len(transcript.words):
        tail = transcript.words[cursor:]
        transcript.segments.append(
            Segment(
                text=" ".join(w.text for w in tail),
                start=tail[0].start,
                end=tail[-1].end,
                first_word=cursor,
                last_word=len(transcript.words) - 1,
            )
        )


def transcribe_groq(
    audio: Path,
    settings: WhisperSettings,
    *,
    api_key: str | None,
    duration_s: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    scratch_dir: Path | None = None,
    prompt: str = "",
) -> Transcript:
    """Transcribe ``audio`` with Groq Whisper into a word-timed transcript."""
    if not api_key:
        raise GroqError(
            "No Groq API key found. Add GROQ_API_KEY=... to the .env file "
            "(free at console.groq.com)."
        )

    total = duration_s or ffmpeg.probe(audio).duration_s
    chunks = plan_chunks(total, file_bytes=audio.stat().st_size)
    scratch = scratch_dir or audio.parent / "groq_chunks"
    url = f"{GROQ_BASE_URL}/audio/transcriptions"
    transcript = Transcript(model=settings.groq_model, source="whisper")
    owns_client = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT_S)

    try:
        for index, (offset, length) in enumerate(chunks):
            if len(chunks) == 1:
                part = audio
            else:
                scratch.mkdir(parents=True, exist_ok=True)
                part = scratch / f"chunk_{index:03d}{audio.suffix}"
                # Stream copy: the audio is already compressed speech, so cutting
                # a chunk is I/O only.
                ffmpeg.run(
                    [
                        "-ss", f"{offset:.3f}", "-t", f"{length:.3f}", "-i", str(audio),
                        "-vn", "-c:a", "copy", str(part),
                    ]  # fmt: skip
                )
            data: dict[str, Any] = {
                "model": settings.groq_model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"],
                "temperature": "0",
            }
            if settings.language:
                data["language"] = settings.language
            if prompt:
                # Spelling hints (names, titles) biasing recognition; Groq caps ~224 tokens.
                data["prompt"] = prompt
            payload = _post_with_retry(
                http, url, api_key=api_key, audio=part, data=data, cancelled=cancelled, sleep=sleep,
                label=(
                    f"Transcribing with Groq Whisper (chunk {index + 1}/{len(chunks)})"
                    if len(chunks) > 1 else "Transcribing with Groq Whisper"
                ),
            )
            if not transcript.language:
                transcript.language = str(payload.get("language", "") or settings.language)
            map_response(payload, offset, transcript)
            if on_progress:
                on_progress((index + 1) / len(chunks))
    finally:
        if owns_client:
            http.close()

    if not transcript.words:
        raise GroqError(
            "No speech was detected. Check the file has audible speech and the right language."
        )
    log.info(
        "Groq transcribed %d words in %d segments (%d chunk(s)).",
        len(transcript.words),
        len(transcript.segments),
        len(chunks),
    )
    return transcript
