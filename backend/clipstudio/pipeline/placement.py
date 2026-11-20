"""Caption placement: keep captions off faces and off the picture.

For every caption group the exporter knows what is on screen (which layout, where
the picture sits in a fit/blur frame, where the tracked faces are in output
pixels). ``choose_bottom_y`` returns the y (bottom edge of the text block) to use,
or ``None`` when the style's normal position is already free.

Rules, in order:

* fit/blur layout: captions go in the blurred band *below* the picture (centred in
  the part clear of the platform UI), else the band above it, never over it;
* otherwise the default position is used unless a face box would overlap the
  caption block, in which case the nearest free vertical zone is taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .reframe.croppath import CropPath, CropSegment

#: Platform UI covers this share of the bottom of the frame.
SAFE_BOTTOM_RATIO = 0.20
#: Hook card lives here at the top; captions stay below it.
TOP_LIMIT_RATIO = 0.30
STEP_RATIO = 0.02

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass
class ZoneSegment:
    """What one stretch of the edited timeline looks like in output pixels."""

    start: float
    end: float
    #: "speaker" (face crop), "fit" (whole frame over blur) or "split".
    kind: str = "speaker"
    #: Vertical extent ``(y0, y1)`` of the picture in a fit layout.
    picture: tuple[float, float] | None = None
    #: ``(t, x0, y0, x1, y1)`` face boxes in output pixels, edited-timeline time.
    faces: list[tuple[float, float, float, float, float]] = field(default_factory=list)

    def contains(self, t: float) -> bool:
        return self.start <= t < self.end

    def faces_between(self, t0: float, t1: float, pad_s: float = 0.5) -> list[Rect]:
        return [(x0, y0, x1, y1) for t, x0, y0, x1, y1 in self.faces if t0 - pad_s <= t <= t1 + pad_s]


def _kf_at(seg: CropSegment, t: float) -> tuple[float, float]:
    """Crop window origin at source time ``t`` (linear between keyframes)."""
    kfs = seg.keyframes
    if not kfs:
        return 0.0, 0.0
    if t <= kfs[0].t or len(kfs) == 1:
        return kfs[0].x, kfs[0].y
    for a, b in zip(kfs, kfs[1:], strict=False):
        if a.t <= t <= b.t and b.t > a.t:
            r = (t - a.t) / (b.t - a.t)
            return a.x + (b.x - a.x) * r, a.y + (b.y - a.y) * r
    return kfs[-1].x, kfs[-1].y


def build_zones(crop: CropPath, out_w: int, out_h: int) -> list[ZoneSegment]:
    """Zone segments (edited timeline) from a resolved, cut crop path.

    Pieces after ``autoedit.apply_cuts`` keep *source* clip-relative times in
    ``start_s/end_s`` and are concatenated back to back, so a running cursor gives
    the edited time. Face boxes are mapped through each piece's crop geometry.
    """
    zones: list[ZoneSegment] = []
    cursor = 0.0
    for seg in crop.segments:
        a, b = seg.start_s, seg.end_s
        zone = ZoneSegment(cursor, cursor + (b - a))
        if seg.layout == "split":
            zone.kind = "split"
        elif seg.fit:
            zone.kind = "fit"
            sw, sh = crop.source_width, crop.source_height
            s = min(out_w / sw, out_h / sh)
            pw, ph = sw * s, sh * s
            ox, oy = (out_w - pw) / 2, (out_h - ph) / 2
            zone.picture = (oy, oy + ph)
            for f in crop.faces:
                if a <= f["t"] < b:
                    zone.faces.append(
                        (cursor + f["t"] - a, ox + f["x0"] * s, oy + f["y0"] * s,
                         ox + f["x1"] * s, oy + f["y1"] * s)
                    )  # fmt: skip
        else:
            sx, sy = out_w / seg.width, out_h / seg.height
            for f in crop.faces:
                if a <= f["t"] < b:
                    cx, cy = _kf_at(seg, f["t"])
                    box = ((f["x0"] - cx) * sx, (f["y0"] - cy) * sy,
                           (f["x1"] - cx) * sx, (f["y1"] - cy) * sy)  # fmt: skip
                    if box[2] > 0 and box[3] > 0 and box[0] < out_w and box[1] < out_h:
                        zone.faces.append((cursor + f["t"] - a, *box))
        zones.append(zone)
        cursor += b - a
    return zones


def zone_at(zones: list[ZoneSegment], t: float) -> ZoneSegment | None:
    for z in zones:
        if z.contains(t):
            return z
    return zones[-1] if zones and t >= zones[-1].end else None


def _overlaps(box: Rect, others: list[Rect]) -> float:
    total = 0.0
    for x0, y0, x1, y1 in others:
        w = min(box[2], x1) - max(box[0], x0)
        h = min(box[3], y1) - max(box[1], y0)
        if w > 0 and h > 0:
            total += w * h
    return total


def choose_bottom_y(
    zone: ZoneSegment | None,
    *,
    t0: float,
    t1: float,
    out_w: int,
    out_h: int,
    default_bottom: float,
    box_w: float,
    box_h: float,
    face_margin: float = 0.0,
    force_band: bool = True,
) -> float | None:
    """Bottom y for the caption block, or None to keep the style's default position."""
    if zone is None or zone.kind == "split":
        return None
    safe_bottom = out_h * (1 - SAFE_BOTTOM_RATIO)
    top_limit = out_h * TOP_LIMIT_RATIO
    x0 = (out_w - box_w) / 2
    x1 = x0 + box_w

    avoid: list[Rect] = []
    for fx0, fy0, fx1, fy1 in zone.faces_between(t0, t1):
        mx, my = (fx1 - fx0) * (0.1 + face_margin), (fy1 - fy0) * (0.1 + face_margin)
        avoid.append((fx0 - mx, fy0 - my, fx1 + mx, fy1 + my))
    if zone.kind == "fit" and zone.picture:
        avoid.append((0.0, zone.picture[0], float(out_w), zone.picture[1]))

    def blocked(bottom: float) -> float:
        return _overlaps((x0, bottom - box_h, x1, bottom), avoid)

    candidates: list[float] = []
    if zone.kind == "fit" and zone.picture and force_band:
        py0, py1 = zone.picture
        lo, hi = py1, safe_bottom
        if hi - lo >= box_h + 8:
            candidates.append(lo + (hi - lo + box_h) / 2)  # centred in the lower band
        lo, hi = top_limit, py0
        if hi - lo >= box_h + 8:
            candidates.append(lo + (hi - lo + box_h) / 2)  # centred in the upper band
    candidates.append(default_bottom)
    step = out_h * STEP_RATIO
    lowest, highest = safe_bottom, top_limit + box_h
    k = 1
    while default_bottom - k * step >= highest or default_bottom + k * step <= lowest:
        if default_bottom + k * step <= lowest:
            candidates.append(default_bottom + k * step)
        if default_bottom - k * step >= highest:
            candidates.append(default_bottom - k * step)
        k += 1

    best, best_overlap = default_bottom, None
    for cand in candidates:
        overlap = blocked(cand)
        if overlap <= 0:
            return None if abs(cand - default_bottom) < 0.5 else cand
        if best_overlap is None or overlap < best_overlap:
            best, best_overlap = cand, overlap
    return None if abs(best - default_bottom) < 0.5 else best
