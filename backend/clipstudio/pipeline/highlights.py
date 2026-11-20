"""Highlight detection — transcript in, ranked clips out.

The transcript is cut into overlapping windows so that long videos fit inside
small context windows, each window is scored by the configured provider, and the
resulting candidates are merged, deduplicated, boundary-refined, and ranked.

Overlap matters: a clip straddling a window edge would otherwise be seen only in
halves by both windows and proposed by neither. The overlap costs tokens and
buys back the clips that live on the seams.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .. import activity
from ..db.models import REJECT_REASON_LABELS, Clip, new_id
from ..providers import ClipCandidate, DetectionConfig, LLMProvider, TranscriptWindow
from . import boundaries, scoring
from .prepare import Silence
from .transcript import Transcript

log = logging.getLogger(__name__)

#: Window length and overlap in seconds of speech.
WINDOW_S = 8 * 60
OVERLAP_S = 60

#: Two candidates covering this much of the same words are the same clip.
DEDUPE_IOU = 0.5
#: Short videos are one story told from several angles, so their clips overlap
#: by design; only near-identical spans count as duplicates there.
SHORT_VIDEO_DEDUPE_IOU = 0.8

#: Hosted providers tolerate parallel windows; a local model is already
#: saturating the GPU, so extra concurrency only adds contention.
HOSTED_CONCURRENCY = 3
LOCAL_CONCURRENCY = 1


class HighlightError(RuntimeError):
    """Highlight detection produced nothing usable."""


#: A hook is an on-screen opener, not a transcript excerpt.
HOOK_MAX_WORDS = 8
HOOK_MAX_CHARS = 64


def _shorten(text: str, max_words: int = HOOK_MAX_WORDS) -> str:
    words = text.split()
    short = " ".join(words[:max_words])
    return short.rstrip(",;:-") if len(words) > max_words else short


def clean_hook(hook: str, title: str, transcript_start: str = "") -> str:
    """Return a short hook: the model's if it is short and not a transcript quote.

    Older prompts (and some models) return the clip's opening sentence verbatim;
    that is too long to show on screen. Fall back to a shortened title.
    """
    hook = " ".join((hook or "").split())
    title = " ".join((title or "").split())
    norm = transcript_start.lower().strip()
    copied = len(hook) > 25 and bool(norm) and norm.startswith(hook.lower()[:40])
    if hook and len(hook.split()) <= HOOK_MAX_WORDS and len(hook) <= HOOK_MAX_CHARS and not copied:
        return hook
    return _shorten(title or hook)


def build_windows(
    transcript: Transcript,
    *,
    window_s: float = WINDOW_S,
    overlap_s: float = OVERLAP_S,
) -> list[TranscriptWindow]:
    """Split a transcript into overlapping windows.

    Preconditions:
        overlap_s is less than window_s, otherwise windows would never advance.
    """
    if not transcript.words:
        return []
    if overlap_s >= window_s:
        raise ValueError("overlap_s must be smaller than window_s")

    windows: list[TranscriptWindow] = []
    total_words = len(transcript.words)
    cursor = 0

    while cursor < total_words:
        window_start_time = transcript.words[cursor].start
        window_end_time = window_start_time + window_s

        last = cursor
        while last + 1 < total_words and transcript.words[last + 1].end <= window_end_time:
            last += 1

        windows.append(_make_window(transcript, cursor, last))
        if cursor == 0 and len(windows) == 1:
            windows[0].start_s = 0.0  # cover a speech-less intro

        if last >= total_words - 1:
            break

        # Step forward by (window - overlap), measured in time then converted
        # back to a word index so overlap stays constant regardless of pace.
        next_time = transcript.words[last].end - overlap_s
        next_cursor = transcript.index_at_time(next_time)
        cursor = max(cursor + 1, next_cursor)

    if windows:
        windows[-1].end_s = 0.0  # 0 = open-ended: covers speech-less outro too
    return windows


def _make_window(transcript: Transcript, first: int, last: int) -> TranscriptWindow:
    start_s, end_s = transcript.time_range(first, last)
    words = transcript.slice(first, last)
    speakers = sorted({w.speaker for w in words if w.speaker})
    return TranscriptWindow(
        text=render_window_text(transcript, first, last),
        first_word=first,
        last_word=last,
        duration_s=end_s - start_s,
        speakers=speakers,
        start_s=start_s,
        end_s=end_s,
    )


def render_window_text(transcript: Transcript, first: int, last: int) -> str:
    """Render words as ``[index]word`` so the model can cite exact positions.

    Tagging every word is verbose, but it's the reason the model can't
    miscount — it never has to derive an index, only copy one.
    """
    parts: list[str] = []
    current_speaker: str | None = None

    for index in range(first, min(last + 1, len(transcript.words))):
        word = transcript.words[index]
        if word.speaker and word.speaker != current_speaker:
            current_speaker = word.speaker
            parts.append(f"\n<{word.speaker}>")
        parts.append(f"[{index}]{word.text}")

    return " ".join(parts).strip()


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


async def detect(
    transcript: Transcript,
    provider: LLMProvider,
    config: DetectionConfig,
    *,
    job_id: str,
    silences: list[Silence] | None = None,
    on_progress: Callable[[float], None] | None = None,
    existing: list[Clip] | None = None,
    visual: list[tuple[float, str, bool]] | None = None,
) -> list[Clip]:
    """Run detection across the whole transcript and return ranked clips.

    With ``existing`` (generate-more mode) the model is told which ranges are
    taken and which clips the user rejected, and candidates overlapping an
    existing clip are dropped.
    """
    if existing:
        apply_exclusions(config, existing)
    if visual:
        config.visual = list(visual)
    windows = build_windows(transcript)
    if not windows:
        raise HighlightError("The transcript is empty, so there is nothing to clip.")

    log.info("Detecting highlights across %d window(s) with %s.", len(windows), provider.name)

    concurrency = LOCAL_CONCURRENCY if provider.name == "ollama" else HOSTED_CONCURRENCY
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0
    lock = asyncio.Lock()

    label = activity.describe(provider)
    total = len(windows)

    async def run_window(index: int, window: TranscriptWindow) -> list[ClipCandidate]:
        nonlocal completed
        async with semaphore:
            try:
                async with activity.waiting(
                    f"Asking {label} for moments: window {index + 1}/{total} sent"
                ):
                    result = await provider.detect_highlights(window, config)
                candidates = result.clips
                activity.note(f"Window {index + 1}/{total} done: {len(candidates)} candidates")
            except Exception as exc:
                # One bad window shouldn't lose the whole video's other windows.
                log.warning(
                    "Window %d-%d failed (%s); continuing with the remaining windows.",
                    window.first_word,
                    window.last_word,
                    exc,
                )
                candidates = []
                activity.note(f"Window {index + 1}/{total} failed, skipped")
            async with lock:
                completed += 1
                if on_progress:
                    on_progress(completed / len(windows))
            return candidates

    results = await asyncio.gather(*(run_window(i, w) for i, w in enumerate(windows)))
    candidates = [c for group in results for c in group]

    if not candidates and not existing:
        fallback = whole_video_candidate(transcript, config)
        if fallback is not None:
            log.warning("No candidates from the model; using the whole short video as one story.")
            candidates = [fallback]

    if not candidates:
        raise HighlightError(
            "No clips were found. This can mean the video genuinely has no "
            "self-contained highlights, or that the model struggled with the "
            "transcript — try a larger model or a different provider."
        )

    log.info("Providers proposed %d raw candidates.", len(candidates))
    return build_clips(
        transcript, candidates, config, job_id=job_id, silences=silences or [], existing=existing
    )


def dedupe_threshold(transcript: Transcript, config: DetectionConfig) -> float:
    """Looser dedupe for videos only a little longer than one clip."""
    if not transcript.words:
        return DEDUPE_IOU
    length = transcript.words[-1].end
    return SHORT_VIDEO_DEDUPE_IOU if length < 2.5 * config.max_duration_s else DEDUPE_IOU


def whole_video_candidate(transcript: Transcript, config: DetectionConfig) -> ClipCandidate | None:
    """A short video the model returned nothing for is still one story: clip all of it."""
    if not transcript.words:
        return None
    if transcript.words[-1].end > 1.5 * config.max_duration_s:
        return None
    return ClipCandidate(
        start_word_index=0,
        end_word_index=len(transcript.words) - 1,
        score=60,
        reason="Fallback: the whole short video as one story.",
    )


def build_clips(
    transcript: Transcript,
    candidates: list[ClipCandidate],
    config: DetectionConfig,
    *,
    job_id: str,
    silences: list[Silence] | None = None,
    existing: list[Clip] | None = None,
) -> list[Clip]:
    """Deduplicate, refine, rank, and truncate raw candidates into clips."""
    for candidate in candidates:
        if candidate.start_s is not None and candidate.end_s is not None:
            first, last = transcript.indices_in_range(candidate.start_s, candidate.end_s)
            if last > first:
                candidate.start_word_index, candidate.end_word_index = first, last - 1
            else:
                candidate.start_word_index = candidate.end_word_index = transcript.index_at_time(
                    candidate.start_s
                )
    threshold = dedupe_threshold(transcript, config)
    deduped = dedupe(candidates, iou_threshold=threshold)
    log.info("%d candidates remain after dedupe.", len(deduped))

    clips: list[Clip] = []
    for candidate in deduped:
        if candidate.start_s is not None and candidate.end_s is not None:
            boundary = _time_boundary(transcript, candidate, config)
        else:
            boundary = boundaries.refine(
                transcript,
                candidate.start_word_index,
                candidate.end_word_index,
                silences=silences or [],
                min_duration_s=config.min_duration_s,
                max_duration_s=config.max_duration_s,
            )
        if boundary is None:
            log.debug(
                "Dropped candidate %d-%d: no valid boundary within the duration range.",
                candidate.start_word_index,
                candidate.end_word_index,
            )
            continue

        clips.append(
            Clip(
                id=new_id(),
                job_id=job_id,
                start_s=boundary.start_s,
                end_s=boundary.end_s,
                start_word=boundary.start_word,
                end_word=boundary.end_word,
                title=candidate.title.strip()
                or transcript.text_between(
                    boundary.start_word, min(boundary.start_word + 8, boundary.end_word)
                ),
                hook=clean_hook(
                    candidate.hook,
                    candidate.title,
                    transcript.text_between(
                        boundary.start_word, min(boundary.start_word + 12, boundary.end_word)
                    ),
                ),
                score=candidate.score,
                llm_score=candidate.score,
                reason=candidate.reason.strip(),
                description=candidate.description.strip(),
                hashtags=candidate.hashtags,
                emphasis=[
                    i for i in candidate.emphasis if boundary.start_word <= i <= boundary.end_word
                ],
            )
        )
        words = clip_words(
            transcript, boundary.start_s, boundary.end_s, boundary.start_word, boundary.end_word
        )
        # A speech-less clip has no words to score; trust the model's score.
        pre = (
            scoring.heuristic_score(
                words,
                min_duration_s=config.min_duration_s,
                max_duration_s=config.max_duration_s,
            )
            if words
            else candidate.score
        )
        clips[-1].heuristic_score = pre
        clips[-1].score = scoring.blend(candidate.score, pre, config.heuristic_weight)

    if existing:
        clips = drop_overlapping(clips, existing)

    # Refinement can move edges enough that two survivors now overlap.
    clips = _dedupe_clips(clips, iou_threshold=threshold)
    clips.sort(key=lambda c: c.score, reverse=True)
    clips = clips[: config.max_clips]
    clips = scoring.quality_gate(
        clips, threshold=config.quality_threshold, min_keep=config.min_keep
    )

    for rank, clip in enumerate(clips, start=1):
        clip.rank = rank

    log.info("Produced %d final clips.", len(clips))
    return clips


def clip_words(transcript: Transcript, start_s: float, end_s: float, first: int, last: int):
    """Words of a clip that actually fall inside its time span.

    Time-based (speech-less) clips are anchored to the nearest word, which can sit
    outside the clip; those words must not leak into captions or scoring.
    """
    return [
        w
        for w in transcript.slice(first, last)
        if w.end > start_s - 0.05 and w.start < end_s + 0.05
    ]


def _time_boundary(transcript: Transcript, candidate: ClipCandidate, config: DetectionConfig):
    """Boundary for a time-ranged candidate (no word/sentence snapping)."""
    start_s = max(0.0, candidate.start_s)
    end_s = candidate.end_s
    if end_s - start_s > config.max_duration_s:
        end_s = start_s + config.max_duration_s
    elif end_s - start_s < config.min_duration_s:
        pad = (config.min_duration_s - (end_s - start_s)) / 2
        start_s = max(0.0, start_s - pad)
        end_s = start_s + config.min_duration_s
    first, last = transcript.indices_in_range(start_s, end_s)
    if last > first:
        first, last = first, last - 1
    else:
        first = last = transcript.index_at_time(start_s)
    return boundaries.Boundary(start_s=start_s, end_s=end_s, start_word=first, end_word=last)


#: A new clip covering more than this share of its own length with an
#: existing clip is dropped.
MAX_EXISTING_OVERLAP = 0.30


def overlap_fraction(clip: Clip, others: list[Clip]) -> float:
    """Share of ``clip``'s duration covered by the union of ``others`` (0..1)."""
    length = clip.end_s - clip.start_s
    if length <= 0:
        return 1.0
    spans = sorted(
        (max(clip.start_s, o.start_s), min(clip.end_s, o.end_s))
        for o in others
        if o.end_s > clip.start_s and o.start_s < clip.end_s
    )
    covered = 0.0
    cursor = clip.start_s
    for start, end in spans:
        start = max(start, cursor)
        if end > start:
            covered += end - start
            cursor = end
    return covered / length


def drop_overlapping(
    clips: list[Clip], existing: list[Clip], *, max_fraction: float = MAX_EXISTING_OVERLAP
) -> list[Clip]:
    """Drop clips that overlap already-existing clips by more than ``max_fraction``."""
    kept = [c for c in clips if overlap_fraction(c, existing) <= max_fraction]
    if len(kept) != len(clips):
        log.info("Dropped %d candidate(s) overlapping existing clips.", len(clips) - len(kept))
    return kept


def apply_exclusions(config: DetectionConfig, existing: list[Clip]) -> None:
    """Tell the prompt which word ranges are used and which clips were rejected."""
    config.used_ranges = [(c.start_word, c.end_word) for c in existing]
    disliked: list[tuple[str, str]] = []
    for clip in existing:
        if clip.status != "discarded":
            continue
        reason = REJECT_REASON_LABELS.get(clip.reject_reason, "")
        if clip.reject_note:
            reason = f"{reason}: {clip.reject_note}" if reason else clip.reject_note
        disliked.append((clip.title, reason))
    config.disliked = disliked


def dedupe(
    candidates: list[ClipCandidate], *, iou_threshold: float = DEDUPE_IOU
) -> list[ClipCandidate]:
    """Greedily keep the highest-scoring candidate from each overlapping cluster.

    Overlapping windows mean the same moment is often proposed two or three
    times, sometimes with slightly different edges. Highest score wins.
    """
    ordered = sorted(candidates, key=lambda c: c.score, reverse=True)
    kept: list[ClipCandidate] = []

    for candidate in ordered:
        if any(
            _time_iou(candidate, existing) > iou_threshold
            if candidate.start_s is not None and existing.start_s is not None
            else _iou(
                candidate.start_word_index,
                candidate.end_word_index,
                existing.start_word_index,
                existing.end_word_index,
            )
            > iou_threshold
            for existing in kept
        ):
            continue
        kept.append(candidate)

    return kept


def _time_iou(a: ClipCandidate, b: ClipCandidate) -> float:
    inter = max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))
    union = (a.end_s - a.start_s) + (b.end_s - b.start_s) - inter
    return inter / union if union > 0 else 0.0


def _dedupe_clips(clips: list[Clip], *, iou_threshold: float = DEDUPE_IOU) -> list[Clip]:
    ordered = sorted(clips, key=lambda c: c.score, reverse=True)
    kept: list[Clip] = []
    for clip in ordered:
        if any(_clips_iou(clip, other) > iou_threshold for other in kept):
            continue
        kept.append(clip)
    return kept


def _clips_iou(a: Clip, b: Clip) -> float:
    # A speech-less clip is anchored to a single word, so word overlap says
    # nothing about it; compare by time instead.
    if a.start_word == a.end_word or b.start_word == b.end_word:
        inter = max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))
        union = (a.end_s - a.start_s) + (b.end_s - b.start_s) - inter
        return inter / union if union > 0 else 0.0
    return _iou(a.start_word, a.end_word, b.start_word, b.end_word)


def _iou(a_start: int, a_end: int, b_start: int, b_end: int) -> float:
    """Intersection over union of two inclusive index ranges."""
    intersection = max(0, min(a_end, b_end) - max(a_start, b_start) + 1)
    if intersection == 0:
        return 0.0
    union = (a_end - a_start + 1) + (b_end - b_start + 1) - intersection
    return intersection / union if union else 0.0
