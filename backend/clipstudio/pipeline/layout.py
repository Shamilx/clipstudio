"""Layout resolution — single face crop vs. slide-over-speaker split frame.

The reframer decides *what is in shot* (face track, wide two-shot, no face / fit
because the subjects are too spread out). This module decides how that becomes a
vertical frame:

* ``speaker`` - one 9:16 crop around the speaker (the normal talking-head look).
* ``split``   - top ~40% the whole source frame (slide / screen, letterboxed over
  a blurred copy of itself), bottom ~60% a face crop of the speaker. A slide shot
  as one wide crop makes the speaker a few pixels tall; the split keeps both.

``layout_mode`` comes from the template: ``auto`` splits where the reframer saw
slide-like content (fit / no-face segments), ``speaker`` never splits, ``split``
always does.
"""

from __future__ import annotations

from dataclasses import replace

from .reframe.croppath import CropKeyframe, CropPath, CropSegment, Strategy, target_crop_size

#: Share of the output height given to the slide panel in the split layout.
SPLIT_TOP_RATIO = 0.40
#: Where the eye line sits inside the face panel (fraction of panel height).
FACE_EYE_RATIO = 0.40
#: Target face width as a fraction of the face-panel width.
FACE_WIDTH_FRACTION = 0.30


def panel_heights(out_h: int) -> tuple[int, int]:
    top = round(out_h * SPLIT_TOP_RATIO / 2) * 2
    return top, out_h - top


def face_crop(path: CropPath, panel_w: int, panel_h: int) -> tuple[int, int, float, float]:
    """Crop window ``(w, h, x, y)`` in source pixels for the face panel."""
    src_w, src_h = path.source_width, path.source_height
    aspect = panel_w / panel_h
    sp = path.speaker or {"cx": src_w / 2, "eye_y": src_h * 0.38, "w": src_h * 0.22}

    max_w = min(float(src_w), src_h * aspect)
    min_w = min(max_w, 0.42 * src_h * aspect)
    cw = max(min_w, min(max_w, sp["w"] / FACE_WIDTH_FRACTION))
    ch = cw / aspect
    cw_i, ch_i = int(cw) // 2 * 2, int(ch) // 2 * 2
    x = max(0.0, min(sp["cx"] - cw_i / 2, src_w - cw_i))
    y = max(0.0, min(sp["eye_y"] - FACE_EYE_RATIO * ch_i, src_h - ch_i))
    return cw_i, ch_i, round(x, 1), round(y, 1)


def _wants_split(seg: CropSegment, path: CropPath, mode: str) -> bool:
    if mode == "split":
        return True
    if mode == "speaker":
        return False
    # auto: split only a face-bearing shot the reframer had to fit (an edge-to-edge
    # two-shot). A no-face shot (runway, wide set) keeps the blurred whole-frame fit:
    # the speaker's face position comes from other shots and would crop empty set.
    return seg.fit and seg.strategy == Strategy.WIDE


def resolve_layout(path: CropPath, mode: str, out_w: int, out_h: int, ratio: str) -> CropPath:
    """Return a copy of ``path`` whose segments carry their render layout."""
    if ratio != "9:16":
        return path
    top_h, bot_h = panel_heights(out_h)
    segments: list[CropSegment] = []
    for seg in path.segments:
        if _wants_split(seg, path, mode):
            w, h, x, y = face_crop(path, out_w, bot_h)
            segments.append(
                replace(
                    seg,
                    width=w,
                    height=h,
                    keyframes=[CropKeyframe(seg.start_s, x, y)],
                    fit=False,
                    layout="split",
                )
            )
        elif mode == "speaker" and seg.fit and seg.strategy == Strategy.WIDE:
            cw, ch = target_crop_size(path.source_width, path.source_height, 9, 16)
            sp = path.speaker or {"cx": path.source_width / 2, "eye_y": path.source_height * 0.38}
            x = max(0.0, min(sp["cx"] - cw / 2, path.source_width - cw))
            y = max(0.0, min(sp["eye_y"] - 0.38 * ch, path.source_height - ch))
            segments.append(
                replace(
                    seg,
                    width=cw,
                    height=ch,
                    keyframes=[CropKeyframe(seg.start_s, x, y)],
                    fit=False,
                    layout="speaker",
                    strategy=Strategy.TRACK,
                )
            )
        else:
            segments.append(replace(seg, layout="speaker" if not seg.fit else ""))
    return CropPath(
        path.source_width, path.source_height, segments, path.speaker, path.active, path.faces
    )
