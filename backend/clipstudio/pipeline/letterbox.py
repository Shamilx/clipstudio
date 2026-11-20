"""Letterbox / pillarbox detection.

Films are often delivered as a 2.39:1 picture inside a 16:9 frame, with black bars
baked in. Reframing that frame as-is puts the bars into the vertical video. This
module finds the *active picture area* once per source:

* ffmpeg ``cropdetect`` on ~20 frames sampled across the video (fast seek, small
  scale, so it costs a few seconds),
* a per-field median over the samples (robust against dark scenes / fades),
* ignored when the bars are under 2% of the frame on every side.

The result is cached in the job's work dir and applies to every content type.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import subprocess
from pathlib import Path

from . import ffmpeg

log = logging.getLogger(__name__)

SAMPLES = 20
ANALYSIS_WIDTH = 640
#: Bars thinner than this fraction of the frame (on every side) are ignored.
MIN_BAR_FRACTION = 0.02
#: cropdetect black threshold (0-255) and rounding.
CROP_LIMIT = 20
#: Source pixels shaved off every detected bar edge so no sliver of bar survives.
INSET_PX = 2
MIN_VALID_SAMPLES = 5
CACHE_VERSION = 1

_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")

#: (x, y, w, h) in source pixels.
Area = tuple[int, int, int, int]


def parse_cropdetect(stderr: str) -> Area | None:
    """Last ``crop=w:h:x:y`` in cropdetect output, as ``(x, y, w, h)``."""
    found = _CROP_RE.findall(stderr or "")
    if not found:
        return None
    w, h, x, y = (int(v) for v in found[-1])
    return x, y, w, h


def median_area(
    samples: list[Area],
    src_w: int,
    src_h: int,
    *,
    inset: int = INSET_PX,
    min_samples: int = MIN_VALID_SAMPLES,
) -> Area | None:
    """Robust (median) active area from per-frame detections, or None when no real bars.

    ``samples`` are ``(x, y, w, h)`` in source pixels. Returns None if there are too
    few samples or the bars are under 2% of the frame on every side.
    """
    if len(samples) < min_samples or src_w <= 0 or src_h <= 0:
        return None
    left = statistics.median(s[0] for s in samples)
    top = statistics.median(s[1] for s in samples)
    right = statistics.median(src_w - (s[0] + s[2]) for s in samples)
    bottom = statistics.median(src_h - (s[1] + s[3]) for s in samples)
    if (
        left < MIN_BAR_FRACTION * src_w
        and right < MIN_BAR_FRACTION * src_w
        and top < MIN_BAR_FRACTION * src_h
        and bottom < MIN_BAR_FRACTION * src_h
    ):
        return None
    # Only edges that really have a bar are shaved by the inset; real edges stay put.
    x0 = round(left) + (inset if left >= MIN_BAR_FRACTION * src_w else 0)
    y0 = round(top) + (inset if top >= MIN_BAR_FRACTION * src_h else 0)
    x1 = src_w - round(right) - (inset if right >= MIN_BAR_FRACTION * src_w else 0)
    y1 = src_h - round(bottom) - (inset if bottom >= MIN_BAR_FRACTION * src_h else 0)
    x0, y0 = x0 + x0 % 2, y0 + y0 % 2
    w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    if w < src_w * 0.3 or h < src_h * 0.3:  # implausible: mostly black footage
        return None
    return x0, y0, w, h


def sample_times(duration_s: float, n: int = SAMPLES) -> list[float]:
    if duration_s <= 0:
        return [0.0]
    return [duration_s * (0.04 + 0.92 * i / max(1, n - 1)) for i in range(n)]


def _detect_at(video: Path, t: float, scale: float) -> Area | None:
    cmd = [
        ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-ss", f"{t:.2f}", "-i", str(video),
        "-frames:v", "3", "-an",
        "-vf", f"scale={ANALYSIS_WIDTH}:-2,cropdetect=limit={CROP_LIMIT}:round=2:reset=0",
        "-f", "null", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.debug("cropdetect at %.1fs failed: %s", t, exc)
        return None
    area = parse_cropdetect(proc.stderr)
    if area is None:
        return None
    x, y, w, h = area
    return round(x * scale), round(y * scale), round(w * scale), round(h * scale)


def detect_active_area(video: Path, src_w: int, src_h: int, duration_s: float) -> Area | None:
    """Run cropdetect over sampled frames and return the median active area (or None)."""
    if not src_w or not src_h:
        return None
    scale = src_w / ANALYSIS_WIDTH if src_w > ANALYSIS_WIDTH else 1.0
    samples = [a for t in sample_times(duration_s) if (a := _detect_at(video, t, scale))]
    area = median_area(samples, src_w, src_h)
    log.info("Letterbox detection on %s: %d samples -> %s", video.name, len(samples), area)
    return area


def _key(video: Path) -> str:
    try:
        st = video.stat()
        return f"v{CACHE_VERSION}|{video.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return f"v{CACHE_VERSION}|{video}"


def load_or_detect(
    video: Path, src_w: int, src_h: int, duration_s: float, cache: Path | None
) -> Area | None:
    """Cached detection (one cropdetect pass per source). Never raises."""
    key = _key(video)
    if cache is not None and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("key") == key:
                a = data.get("active")
                return tuple(a) if a else None  # type: ignore[return-value]
        except Exception:  # noqa: BLE001 - corrupt cache = recompute
            pass
    try:
        area = detect_active_area(video, src_w, src_h, duration_s)
    except Exception as exc:  # noqa: BLE001
        log.warning("Letterbox detection failed (%s); using the full frame.", exc)
        area = None
    if cache is not None:
        try:
            cache.write_text(json.dumps({"key": key, "active": list(area) if area else None}), encoding="utf-8")
        except OSError:
            pass
    return area


def stricter(area: Area, src_w: int, src_h: int, extra: int = 6) -> Area:
    """Shrink the active area by ``extra`` px on every side (QA re-render after a leftover band)."""
    x, y, w, h = area
    x, y = x + extra, y + extra
    return x, y, max(2, (w - 2 * extra) // 2 * 2), max(2, (h - 2 * extra) // 2 * 2)
