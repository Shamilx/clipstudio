"""Teaser (flash-forward): show the best moment first, then tell the story.

The teaser is a 1.0-1.5 s excerpt of the *edited* clip (so it already has the cuts,
zooms and crop), rendered in front of the full clip in the same ffmpeg pass. The
exporter splits the edited video/audio, trims the excerpt, and concatenates
``[teaser][clip]`` with a short white flash on the join. Everything that lives on
the edited timeline (captions, seam spans, face zones, emoji) is shifted by the
teaser length here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .placement import ZoneSegment
from .transcript import Word

TEASER_MIN_S = 1.0
TEASER_MAX_S = 1.5
#: Flash: the teaser fades out to white and the clip fades in from white.
FLASH_FADE_S = 0.08
#: Don't bother with clips too short for a flash-forward to make sense.
MIN_CLIP_S = 6.0
#: The strongest moment is searched in the last 60% of the clip.
SEARCH_FROM = 0.4
AUDIO_FADE_S = 0.02


@dataclass(frozen=True)
class Teaser:
    """Excerpt of the edited timeline (seconds)."""

    start: float
    end: float

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    @property
    def shift_s(self) -> float:
        """How far everything after the teaser moves on the final timeline."""
        return self.duration_s


def select_teaser(
    words: Sequence[Word],
    total_s: float,
    *,
    emphasis: Sequence[float] = (),
    zooms: Sequence[float] = (),
    max_total_s: float | None = None,
) -> Teaser | None:
    """Pick the strongest 1.0-1.5 s of the edited clip, or None to skip the teaser.

    ``words`` / ``emphasis`` / ``zooms`` are on the edited timeline. Windows start at
    a word in the last 60% of the clip and score by word density, plus a bonus for
    an LLM emphasis word or a planned punch-in zoom inside them. ``max_total_s``
    caps ``total_s + teaser`` (the teaser is shortened, or skipped under 1.0 s).
    """
    if total_s < MIN_CLIP_S:
        return None
    longest = TEASER_MAX_S
    if max_total_s is not None:
        longest = min(longest, max_total_s - total_s)
        if longest < TEASER_MIN_S:
            return None

    lo, hi = total_s * SEARCH_FROM, total_s - TEASER_MIN_S
    starts = [w.start for w in words if lo <= w.start <= hi]
    anchors = [t for t in (*emphasis, *zooms) if lo <= t <= hi + TEASER_MIN_S]
    if not starts:
        # No words in range: use an anchor, else a fixed point 70% in.
        starts = [max(lo, min(hi, t - 0.2)) for t in anchors] or [total_s * 0.7]

    best: tuple[float, float, float] | None = None  # score, start, end
    for s in starts:
        start = max(0.0, s - 0.05)
        end = _window_end(words, start, longest, total_s)
        if end - start < TEASER_MIN_S - 1e-6:
            continue
        inside = [w for w in words if w.start >= start and w.end <= end + 0.05]
        score = float(len(inside))
        score += 4.0 * any(start <= t <= end for t in emphasis)
        score += 3.0 * any(start <= t <= end for t in zooms)
        if best is None or score >= best[0]:  # ties go to the later moment
            best = (score, start, end)
    if best is None:
        return None
    return Teaser(round(best[1], 3), round(best[2], 3))


def _window_end(words: Sequence[Word], start: float, longest: float, total_s: float) -> float:
    """End on a word boundary inside [1.0, longest] s when one exists, else start + 1.2."""
    limit = min(total_s, start + longest)
    ends = [w.end for w in words if start + TEASER_MIN_S <= w.end <= limit and w.start >= start]
    if ends:
        return max(ends)
    return min(limit, start + max(TEASER_MIN_S, min(1.2, longest)))


# --------------------------------------------------------------------------
# Timeline shifting
# --------------------------------------------------------------------------


def shift_words(
    words: Sequence[Word], keywords: set[int], teaser: Teaser
) -> tuple[list[Word], set[int]]:
    """Words for the final timeline: the teaser's own words, then every word + shift."""
    out: list[Word] = []
    flags: set[int] = set()
    for i, w in enumerate(words):
        mid = (w.start + w.end) / 2
        if teaser.start <= mid <= teaser.end:
            out.append(
                replace(
                    w,
                    start=max(0.0, w.start - teaser.start),
                    end=min(teaser.duration_s, w.end - teaser.start),
                )
            )
            if i in keywords:
                flags.add(len(out) - 1)
    for i, w in enumerate(words):
        out.append(replace(w, start=w.start + teaser.shift_s, end=w.end + teaser.shift_s))
        if i in keywords:
            flags.add(len(out) - 1)
    return out, flags


def shift_intervals(
    intervals: Sequence[tuple[float, float]], teaser: Teaser
) -> list[tuple[float, float]]:
    """Spans on the edited timeline -> final timeline (teaser overlap copied to the front)."""
    out: list[tuple[float, float]] = []
    for a, b in intervals:
        lo, hi = max(a, teaser.start), min(b, teaser.end)
        if hi > lo:
            out.append((round(lo - teaser.start, 3), round(hi - teaser.start, 3)))
    out += [(round(a + teaser.shift_s, 3), round(b + teaser.shift_s, 3)) for a, b in intervals]
    return out


def shift_zones(zones: Sequence[ZoneSegment], teaser: Teaser) -> list[ZoneSegment]:
    """Face/picture zones for the final timeline, including the teaser's own copy."""
    out: list[ZoneSegment] = []
    for z in zones:
        lo, hi = max(z.start, teaser.start), min(z.end, teaser.end)
        if hi > lo:
            out.append(
                replace(
                    z,
                    start=lo - teaser.start,
                    end=hi - teaser.start,
                    faces=[(t - teaser.start, *box) for t, *box in z.faces if lo <= t <= hi],
                )
            )
    for z in zones:
        out.append(
            replace(
                z,
                start=z.start + teaser.shift_s,
                end=z.end + teaser.shift_s,
                faces=[(t + teaser.shift_s, *box) for t, *box in z.faces],
            )
        )
    return out
