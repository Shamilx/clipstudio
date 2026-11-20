"""Prepare stage — normalise media into what later stages expect.

Produces a small 16 kHz mono Opus file (what Whisper wants; ~11 MB per hour, so
Groq takes an hour of speech in a single request), a thumbnail strip for UI
scrubbing, and a silence map used later to place clip boundaries in audio
troughs rather than mid-breath.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import resources
from . import ffmpeg

log = logging.getLogger(__name__)

#: Whisper resamples to 16 kHz mono internally; doing it once up front avoids
#: repeating the work on every model invocation.
AUDIO_SAMPLE_RATE = 16_000
#: ~24 kbps Opus is transparent for speech recognition and 20x smaller than PCM.
AUDIO_BITRATE = "24k"

THUMBNAIL_INTERVAL_S = 5
THUMBNAIL_WIDTH = 160


@dataclass
class Silence:
    start: float
    end: float

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2

    @property
    def duration(self) -> float:
        return self.end - self.start


def extract_audio(
    source: Path,
    destination: Path,
    *,
    duration_s: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Extract compressed mono 16 kHz speech audio (Opus in Ogg) from any input.

    Video is never decoded (``-vn``); this is a demux plus a tiny audio encode.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        [
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-c:a",
            "libopus",
            "-b:a",
            AUDIO_BITRATE,
            "-compression_level",
            "2",
            "-application",
            "voip",
            str(destination),
        ],
        total_duration_s=duration_s,
        on_progress=on_progress,
    )
    return destination


def generate_thumbnails(
    source: Path,
    destination_dir: Path,
    *,
    interval_s: int = THUMBNAIL_INTERVAL_S,
    width: int = THUMBNAIL_WIDTH,
) -> list[Path]:
    """Write one thumbnail every ``interval_s`` seconds for UI scrubbing.

    Only keyframes are decoded (``-skip_frame nokey``), at low resolution, so
    this costs a fraction of one full decode. Frames are then thinned to at
    least ``interval_s`` apart. Returns the generated files in chronological
    order. An audio-only source yields an empty list rather than an error.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    pattern = destination_dir / "thumb_%05d.jpg"

    try:
        ffmpeg.run(
            [
                "-skip_frame",
                "nokey",
                "-i",
                str(source),
                "-an",
                "-vf",
                (
                    f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval_s})',"
                    f"scale={width}:-2"
                ),
                "-fps_mode",
                "vfr",
                "-q:v",
                "5",
                str(pattern),
            ]
        )
    except ffmpeg.FFmpegError:
        # Audio-only input has no video stream to sample.
        log.debug("No thumbnails generated for %s (likely audio-only).", source.name)
        return []

    return sorted(destination_dir.glob("thumb_*.jpg"))


_SILENCE_START = re.compile(r"silence_start:\s*([-\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([-\d.]+)")


def detect_silences(
    audio: Path,
    *,
    noise_db: float = -32.0,
    min_duration_s: float = 0.20,
) -> list[Silence]:
    """Find silent spans using ffmpeg's ``silencedetect``.

    Clip boundaries land far better inside these than at a fixed offset from a
    sentence end: a fixed pad clips breaths and plosives, while a trough is
    where a human editor would cut.

    Preconditions:
        audio is a decodable audio file; video inputs work but waste decode time.
    """
    command = [
        ffmpeg.ffmpeg_path(),
        "-hide_banner",
        "-nostdin",
        "-i",
        str(audio),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_duration_s}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        **resources.popen_kwargs(),
    )
    if proc.returncode != 0:
        log.warning("silencedetect failed; boundary refinement will fall back to fixed padding.")
        return []

    silences: list[Silence] = []
    pending_start: float | None = None
    for line in (proc.stderr or "").splitlines():
        if match := _SILENCE_START.search(line):
            pending_start = float(match.group(1))
        elif match := _SILENCE_END.search(line):
            end = float(match.group(1))
            start = pending_start if pending_start is not None else end
            silences.append(Silence(start=max(0.0, start), end=end))
            pending_start = None

    return silences
