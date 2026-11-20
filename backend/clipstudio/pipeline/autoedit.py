"""Automatic editing plan: dead-air trimming, filler removal, zoom schedule.

Everything is computed from word timestamps and applied by ffmpeg in the single
export pass (trim + concat), so it is cheap on a weak CPU.

All times in an :class:`EditPlan` are *clip-relative* source seconds, because
that is the timeline the crop path uses. ``map_time`` converts a source time to
the edited timeline (the one captions and effects live on).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from ..config import EditSettings
from .reframe.croppath import CropPath, CropSegment
from .transcript import Word

MIN_KEEP_S = 0.2
#: Zoom depth of a punch-in on an emphasis moment, and of a jump-cut nudge.
PUNCH_ZOOM = 0.12
JUMP_ZOOM = 0.05
PUNCH_LEAD_S = 0.15
PUNCH_LEN_S = 1.0
MAX_PUNCHES = 6
MIN_PUNCH_GAP_S = 3.5

_STRIP = re.compile(r"[^\w]", re.UNICODE)


@dataclass
class EditPlan:
    """Kept source intervals (clip-relative) and where they land after concat."""

    duration_s: float
    keep: list[tuple[float, float]] = field(default_factory=list)

    @property
    def edited_duration_s(self) -> float:
        return sum(b - a for a, b in self.keep)

    @property
    def removed_s(self) -> float:
        return self.duration_s - self.edited_duration_s

    @property
    def is_noop(self) -> bool:
        return len(self.keep) == 1 and self.keep[0][0] <= 0.001 and (
            self.keep[0][1] >= self.duration_s - 0.001
        )

    def offsets(self) -> list[float]:
        """Start of each kept interval on the edited timeline."""
        out, total = [], 0.0
        for a, b in self.keep:
            out.append(total)
            total += b - a
        return out

    def map_time(self, t: float) -> float:
        """Source time -> edited time (snaps removed time to the next cut)."""
        for (a, b), offset in zip(self.keep, self.offsets(), strict=True):
            if t < a:
                return offset
            if t <= b:
                return offset + (t - a)
        return self.edited_duration_s

    def jump_cut_times(self) -> list[float]:
        return self.offsets()[1:]


def is_filler(text: str, fillers: set[str]) -> bool:
    return _STRIP.sub("", text.lower()) in fillers


def plan_cuts(
    words: list[Word], duration_s: float, settings: EditSettings
) -> tuple[EditPlan, list[Word]]:
    """Plan cuts for a clip.

    ``words`` have clip-relative times. Returns the plan and the surviving words
    (fillers removed), still in source time.
    """
    if not (settings.trim_silence or settings.remove_fillers) or not words:
        return EditPlan(duration_s, [(0.0, duration_s)]), list(words)

    fillers = {f.lower() for f in settings.filler_words} if settings.remove_fillers else set()
    kept_words = [w for w in words if not is_filler(w.text, fillers)] if fillers else list(words)
    if not kept_words:
        return EditPlan(duration_s, [(0.0, duration_s)]), list(words)

    removed: list[tuple[float, float]] = []
    pad = settings.pause_pad_s

    # Filler words (and a hair around them).
    for w in words:
        if fillers and is_filler(w.text, fillers):
            removed.append((max(0.0, w.start - 0.02), min(duration_s, w.end + 0.02)))

    if settings.trim_silence:
        # Gaps between consecutive surviving words; also leading/trailing air.
        prev_end = 0.0
        first = True
        for w in kept_words:
            gap = w.start - prev_end
            limit = 0.4 if first else settings.max_pause_s
            if gap > limit:
                lo = prev_end + (0.0 if first else pad)
                hi = w.start - pad
                if first:
                    lo = 0.0
                if hi - lo > 0.1:
                    removed.append((lo, hi))
            prev_end = max(prev_end, w.end)
            first = False
        tail = duration_s - prev_end
        if tail > 0.5:
            removed.append((prev_end + pad, duration_s))

    # Complement of the merged removed intervals.
    removed = sorted(r for r in removed if r[1] > r[0])
    merged: list[list[float]] = []
    for a, b in removed:
        if merged and a <= merged[-1][1] + 0.02:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in merged:
        if a - cursor >= MIN_KEEP_S:
            keep.append((cursor, a))
        cursor = max(cursor, b)
    if duration_s - cursor >= MIN_KEEP_S:
        keep.append((cursor, duration_s))
    if not keep:
        return EditPlan(duration_s, [(0.0, duration_s)]), list(words)

    # Never gut a clip: if almost everything would go, leave it alone.
    plan = EditPlan(duration_s, keep)
    if plan.edited_duration_s < 3.0 or plan.edited_duration_s < 0.4 * duration_s:
        return EditPlan(duration_s, [(0.0, duration_s)]), list(words)
    return plan, kept_words


def apply_cuts(crop_path: CropPath, keep: list[tuple[float, float]]) -> CropPath:
    """Split crop segments at the kept intervals (clip-relative source time).

    Each piece keeps its keyframes and strategy, with ``start_s``/``end_s`` as the
    source range to trim. Pieces are concatenated back to back by the exporter.
    """
    pieces: list[CropSegment] = []
    for ka, kb in keep:
        for seg in crop_path.segments:
            a, b = max(ka, seg.start_s), min(kb, seg.end_s)
            if b - a > 0.02:
                pieces.append(replace(seg, start_s=a, end_s=b))
    return CropPath(
        crop_path.source_width, crop_path.source_height, pieces, crop_path.speaker,
        crop_path.active, crop_path.faces,
    )


def zoom_intervals(
    plan: EditPlan,
    emphasis_times: list[float],
    *,
    punch: bool,
    jump: bool,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float]]]:
    """Zoom schedule on the edited timeline.

    Returns ``(bumps, steps)``: ``bumps`` are ``(t0, t1, depth)`` smooth punch-ins,
    ``steps`` are ``(t0, t1)`` constant nudges on alternate jump-cut pieces.
    """
    total = plan.edited_duration_s
    bumps: list[tuple[float, float, float]] = []
    if punch:
        last = -99.0
        for t in sorted(plan.map_time(t) for t in emphasis_times):
            if t - last < MIN_PUNCH_GAP_S or len(bumps) >= MAX_PUNCHES:
                continue
            t0 = max(0.0, t - PUNCH_LEAD_S)
            t1 = min(total, t0 + PUNCH_LEN_S)
            if t1 - t0 > 0.4:
                bumps.append((round(t0, 3), round(t1, 3), PUNCH_ZOOM))
                last = t
    steps: list[tuple[float, float]] = []
    if jump and len(plan.keep) > 1:
        offsets = plan.offsets()
        for i, (a, b) in enumerate(plan.keep):
            if i % 2 == 1:
                steps.append((round(offsets[i], 3), round(offsets[i] + (b - a), 3)))
    return bumps, steps


def zoom_expression(
    bumps: list[tuple[float, float, float]],
    steps: list[tuple[float, float]],
    no_zoom: list[tuple[float, float]] | None = None,
) -> str | None:
    """ffmpeg expression for the zoom factor over time ``t`` (None = no zoom)."""
    terms: list[str] = []
    for t0, t1, depth in bumps:
        # Eased punch-in: smoothstep up, hold, smoothstep down (not a sine blip).
        ramp = round(min(0.28, (t1 - t0) / 3), 3)
        x = f"min(clip((t-{t0})/{ramp},0,1),clip(({t1}-t)/{ramp},0,1))"
        terms.append(f"between(t,{t0},{t1})*{depth}*{x}*{x}*(3-2*{x})")
    for t0, t1 in steps:
        terms.append(f"between(t,{t0},{t1})*{JUMP_ZOOM}")
    if not terms:
        return None
    expr = "(" + "+".join(terms) + ")"
    for a, b in no_zoom or []:
        expr = f"{expr}*not(between(t,{round(a, 3)},{round(b, 3)}))"
    return f"(1+{expr})"
