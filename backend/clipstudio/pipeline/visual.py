"""Visual timeline: give the clip-selection LLM "eyes".

The highlight model only reads a transcript, so it misses moments with no speech
(runway walks, reactions, outfits). This stage:

1. finds shots (reusing the reframe scene detector) and picks one frame per shot
   (several for very long shots),
2. tiles the frames 3x3 into contact sheets with a timestamp burned on each tile,
3. asks an NVIDIA-hosted vision model (OpenAI-compatible) to describe every tile,
4. caches the resulting ``[{t, desc, brand_visible}]`` timeline per source.

Every failure degrades to "no visual info" — this stage never fails a job.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import activity, config
from . import ffmpeg
from .reframe.scenes import Shot, detect_shots

log = logging.getLogger(__name__)

DEFAULT_VISION_MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

#: Shots longer than this get extra samples, roughly one per LONG_SHOT_STEP_S.
LONG_SHOT_S = 8.0
LONG_SHOT_STEP_S = 6.0
#: Detection runs on a shrunken frame; this is all scene cuts need.
ANALYSIS_WIDTH = 320
#: Width of each extracted frame / contact-sheet tile.
FRAME_WIDTH = 384
GRID_COLS = 3
GRID_ROWS = 3
TILES_PER_SHEET = GRID_COLS * GRID_ROWS
#: Hard cap on frames per source, so a 3-hour video cannot mean 2,000 requests.
MAX_FRAMES = 360
CONCURRENCY = 4
REQUEST_TIMEOUT_S = 90.0
RETRIES = 1

CACHE_VERSION = 2


@dataclass
class VisualMoment:
    t: float
    desc: str
    brand_visible: bool = False


ProgressFn = Callable[[float, str], None]


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def sample_times(
    shots: list[Shot],
    duration_s: float,
    *,
    long_shot_s: float = LONG_SHOT_S,
    step_s: float = LONG_SHOT_STEP_S,
    max_frames: int = MAX_FRAMES,
) -> list[float]:
    """One timestamp per shot (its middle); long shots get one per ~``step_s``."""
    times: list[float] = []
    for shot in shots:
        length = shot.end_s - shot.start_s
        if length <= 0:
            continue
        if length > long_shot_s:
            n = max(2, round(length / step_s))
            times.extend(shot.start_s + (i + 0.5) * length / n for i in range(n))
        else:
            times.append(shot.start_s + length / 2)

    top = max(0.0, duration_s - 0.1) if duration_s > 0 else None
    cleaned: list[float] = []
    for t in sorted(times):
        t = max(0.0, min(t, top)) if top is not None else max(0.0, t)
        if not cleaned or t - cleaned[-1] >= 0.5:
            cleaned.append(round(t, 2))

    if len(cleaned) > max_frames:
        stride = len(cleaned) / max_frames
        cleaned = [cleaned[int(i * stride)] for i in range(max_frames)]
    return cleaned


def plan_sheets(times: list[float], per_sheet: int = TILES_PER_SHEET) -> list[list[float]]:
    """Group sample times into contact sheets of ``per_sheet`` tiles."""
    return [times[i : i + per_sheet] for i in range(0, len(times), per_sheet)]


def fmt_time(t: float) -> str:
    total = max(0, int(round(t)))
    return f"{total // 60:02d}:{total % 60:02d}"


# --------------------------------------------------------------------------
# Frames and sheets
# --------------------------------------------------------------------------


def extract_frame(video: Path, t: float, out: Path, *, width: int = FRAME_WIDTH) -> bool:
    cmd = [
        ffmpeg.ffmpeg_path(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-ss", f"{t:.3f}", "-i", str(video),
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out),
    ]  # fmt: skip
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        log.debug("Frame at %.1fs failed: %s", t, exc)
        return False
    return out.exists() and out.stat().st_size > 0


def build_sheet(frames: list[tuple[float, Path]]) -> bytes:
    """Tile up to nine frames 3x3 with a "#n MM:SS" label on each; return JPEG bytes."""
    from PIL import Image, ImageDraw, ImageFont

    images = [(t, Image.open(p).convert("RGB")) for t, p in frames]
    cell_w = max(img.width for _, img in images)
    cell_h = max(img.height for _, img in images)
    rows = (len(images) + GRID_COLS - 1) // GRID_COLS
    sheet = Image.new("RGB", (cell_w * GRID_COLS, cell_h * rows), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:  # very old Pillow
        font = ImageFont.load_default()

    for i, (t, img) in enumerate(images):
        x, y = (i % GRID_COLS) * cell_w, (i // GRID_COLS) * cell_h
        sheet.paste(img, (x + (cell_w - img.width) // 2, y + (cell_h - img.height) // 2))
        label = f"#{i + 1}  {fmt_time(t)}"
        box = draw.textbbox((x + 6, y + 6), label, font=font)
        draw.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill=(0, 0, 0))
        draw.text((x + 6, y + 6), label, fill=(255, 255, 0), font=font)

    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Vision model
# --------------------------------------------------------------------------


def build_prompt(times: list[float]) -> str:
    tiles = "\n".join(f"#{i + 1} = {fmt_time(t)} ({t:.1f}s)" for i, t in enumerate(times))
    return (
        "This image is a contact sheet of video frames, 3 columns, read left to right then "
        "top to bottom. Each tile has a yellow label with its number and timestamp.\n"
        f"Tiles:\n{tiles}\n\n"
        "For each tile give ONE short line: who/what is visible, whether clothing or a brand "
        "is visible (yes/no), and the action or emotion. If a name caption, title or any other "
        "on-screen text is visible, quote it exactly in the line. "
        'Reply with ONLY a JSON list: [{"n": tile number, "t": seconds, '
        '"desc": "MM:SS — who/what, clothing/brand visible yes/no, action/emotion", '
        '"brand_visible": true|false}]. No prose, no markdown fences.'
    )


async def _chat(model: str, prompt: str, jpeg: bytes, api_key: str) -> str:
    """One vision request. Isolated so tests can replace it."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.environ.get("NVIDIA_VISION_BASE_URL") or NVIDIA_BASE_URL,
        timeout=REQUEST_TIMEOUT_S,
        max_retries=0,
    )
    data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    response = await client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_tokens=3000,
        # Reasoning models otherwise burn the whole budget on thinking and return nothing.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
    )
    choices = response.choices or []
    return (choices[0].message.content or "") if choices else ""


def parse_descriptions(raw: str, times: list[float]) -> list[VisualMoment]:
    """Parse the model's JSON list; snap each entry to a real tile time."""
    from ..providers.base import _json_by_brace_matching, strip_think

    text = strip_think(raw)
    if m := re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        text = m.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _json_by_brace_matching(text)
    if isinstance(parsed, dict):
        parsed = next((v for v in parsed.values() if isinstance(v, list)), [])
    if not isinstance(parsed, list):
        raise ValueError("expected a JSON list")

    out: list[VisualMoment] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        desc = " ".join(str(item.get("desc") or "").split())
        desc = re.sub(r"^\d{1,2}:\d{2}\s*[-–—−�?:]+\s*", "", desc)
        if not desc:
            continue
        t: float | None = None
        try:
            n = int(item.get("n"))
            if 1 <= n <= len(times):
                t = times[n - 1]
        except (TypeError, ValueError):
            pass
        if t is None:
            try:
                raw_t = float(item.get("t"))
            except (TypeError, ValueError):
                continue
            t = min(times, key=lambda x: abs(x - raw_t))
        brand = item.get("brand_visible")
        if isinstance(brand, str):
            brand = brand.strip().lower() in ("true", "yes", "y")
        out.append(VisualMoment(t=float(t), desc=desc, brand_visible=bool(brand)))
    return out


async def _describe_sheet(
    model: str, api_key: str, times: list[float], jpeg: bytes, label: str = ""
) -> list[VisualMoment] | None:
    """Describe one sheet; ``None`` when it failed after the retry."""
    prompt = build_prompt(times)
    for attempt in range(RETRIES + 1):
        try:
            async with activity.waiting(
                f"Sending frame sheet {label} to NVIDIA vision"
                + (f" (retry {attempt})" if attempt else "")
            ):
                raw = await asyncio.wait_for(
                    _chat(model, prompt, jpeg, api_key), timeout=REQUEST_TIMEOUT_S
                )
            return parse_descriptions(raw, times)
        except Exception as exc:  # noqa: BLE001 - never fail the job
            log.warning(
                "Vision sheet at %s failed (attempt %d): %s",
                fmt_time(times[0]), attempt + 1, str(exc)[:160],
            )  # fmt: skip
    return None


async def describe_sheets(
    sheets: list[tuple[list[float], bytes]],
    *,
    model: str,
    api_key: str,
    on_progress: ProgressFn | None = None,
) -> tuple[list[VisualMoment], int]:
    """Run all sheets, up to CONCURRENCY at once. Returns (moments, failed_sheets)."""
    semaphore = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def one(index: int, times: list[float], jpeg: bytes):
        nonlocal done
        label = f"{index + 1}/{len(sheets)}"
        async with semaphore:
            result = await _describe_sheet(model, api_key, times, jpeg, label)
        done += 1
        if result is None:
            activity.note(f"Sheet {index + 1} failed, skipped")
        else:
            activity.note(f"Sheet {label} done ({len(result)} descriptions)")
        if on_progress:
            on_progress(0.4 + 0.6 * done / len(sheets), f"Describing frames {done}/{len(sheets)}")
        return result

    results = await asyncio.gather(*(one(i, t, j) for i, (t, j) in enumerate(sheets)))
    moments = [m for r in results if r for m in r]
    moments.sort(key=lambda m: m.t)
    return moments, sum(1 for r in results if r is None)


# --------------------------------------------------------------------------
# Cache + entry point
# --------------------------------------------------------------------------


def vision_model() -> str:
    return os.environ.get("NVIDIA_VISION_MODEL") or DEFAULT_VISION_MODEL


def cache_key(video: Path, model: str, max_duration_s: float | None = None) -> str:
    try:
        st = video.stat()
        ident = f"{video.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        ident = str(video)
    return f"v{CACHE_VERSION}|{ident}|{model}|{max_duration_s or 0}"


def load_cache(cache: Path, key: str) -> list[VisualMoment] | None:
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("key") != key:
            return None
        return [VisualMoment(**m) for m in data["moments"]]
    except Exception:  # noqa: BLE001 - missing or corrupt cache = recompute
        return None


def build_timeline(
    video: Path,
    duration_s: float,
    *,
    cache: Path | None = None,
    max_duration_s: float | None = None,
    on_progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[VisualMoment]:
    """Compute (or load) the visual timeline for ``video``. Never raises."""
    try:
        return _build_timeline(video, duration_s, cache, max_duration_s, on_progress, cancelled)
    except Exception as exc:  # noqa: BLE001
        log.warning("Visual analysis failed (%s); continuing without it.", exc)
        return []


#: Mean absolute grey-level difference (0-255) between consecutive keyframes
#: that counts as a cut.
KEYFRAME_CUT_DIFF = 22.0
_KF_W, _KF_H = 96, 54


def keyframe_shots(video: Path, span_s: float) -> list[Shot]:
    """Find shots by decoding keyframes only (tens of times faster than a full decode).

    ffprobe lists keyframe times from packet flags without decoding; ffmpeg then
    decodes just those frames at 96x54 grey. A large jump between neighbouring
    keyframes marks a cut. Cuts land on keyframes (usually every 1-2 s), which is
    plenty for picking one frame per scene. Returns [] if anything goes wrong.
    """
    import subprocess

    import numpy as np

    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-skip_frame",
                "nokey",
                "-show_entries",
                "frame=pts_time",
                "-of",
                "csv=p=0",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=True,
        )
        cells = (x.strip().strip(",") for x in probe.stdout.split())
        times = [float(x) for x in cells if x and x != "N/A"]
        raw = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-skip_frame",
                "nokey",
                "-i",
                str(video),
                "-vf",
                f"scale={_KF_W}:{_KF_H},format=gray",
                "-fps_mode",
                "passthrough",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            timeout=600,
            check=True,
        ).stdout
    except Exception as exc:  # noqa: BLE001 - fall back to the full decode
        log.warning("Keyframe shot detection failed (%s); using full decode.", exc)
        return []

    size = _KF_W * _KF_H
    frames = np.frombuffer(raw, dtype=np.uint8)[: len(raw) // size * size].reshape(-1, size)
    n = min(len(frames), len(times))
    if n < 2:
        return []
    times, frames = times[:n], frames[:n].astype(np.int16)
    diffs = np.abs(np.diff(frames, axis=0)).mean(axis=1)

    shots: list[Shot] = []
    start = 0.0
    for i, d in enumerate(diffs, start=1):
        t = times[i]
        if t >= span_s:
            break
        if d >= KEYFRAME_CUT_DIFF and t - start >= 0.8:
            shots.append(Shot(start, t))
            start = t
    if span_s - start > 0.1:
        shots.append(Shot(start, span_s))
    log.info("Keyframe shot detection: %d keyframes -> %d shots.", n, len(shots))
    return shots


def _build_timeline(video, duration_s, cache, max_duration_s, on_progress, cancelled):
    progress = on_progress or (lambda f, m: None)
    model = vision_model()
    key = cache_key(video, model, max_duration_s)
    if cache is not None and (cached := load_cache(cache, key)) is not None:
        log.info("Reusing cached visual timeline (%d moments).", len(cached))
        progress(1.0, "Using cached visual timeline")
        activity.note(f"Using cached visual timeline ({len(cached)} moments)")
        return cached

    api_key = config.nvidia_api_key()
    if not api_key:
        log.info("NVIDIA_API_KEY is not set; skipping visual analysis.")
        return []

    span = min(duration_s, max_duration_s) if max_duration_s else duration_s
    progress(0.0, "Finding shots (keyframes)")
    with activity.waiting_sync("Finding shots (keyframes)"):
        shots = keyframe_shots(video, span or duration_s)
        if not shots:  # odd container / no keyframe info: fall back to a full decode
            activity.relabel("Finding shots (full decode, slower)")
            shots = detect_shots(video, end_s=span or None, analysis_width=ANALYSIS_WIDTH)
    times = sample_times(shots, span)
    if not times:
        return []
    log.info("Visual plan: %d shot(s) -> %d frame(s).", len(shots), len(times))
    activity.note(f"Found {len(shots)} shots, sampling {len(times)} frames")

    sheets: list[tuple[list[float], bytes]] = []
    with tempfile.TemporaryDirectory(prefix="visual_") as tmp:
        tmp_dir = Path(tmp)
        groups = plan_sheets(times)
        for gi, group in enumerate(groups):
            if cancelled and cancelled():
                return []
            frames: list[tuple[float, Path]] = []
            for fi, t in enumerate(group):
                out = tmp_dir / f"f_{gi}_{fi}.jpg"
                if extract_frame(video, t, out):
                    frames.append((t, out))
            progress(
                0.4 * (gi + 1) / len(groups),
                f"Extracting frames {sum(len(g) for g in groups[: gi + 1])}/{len(times)}",
            )
            if frames:
                sheets.append(([t for t, _ in frames], build_sheet(frames)))

    if not sheets:
        return []

    moments, failed = asyncio.run(
        describe_sheets(sheets, model=model, api_key=api_key, on_progress=progress)
    )
    if cache is not None and moments and failed == 0:
        try:
            cache.write_text(
                json.dumps({"key": key, "moments": [asdict(m) for m in moments]}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Could not cache the visual timeline: %s", exc)
    elif failed:
        log.warning("%d sheet(s) failed; timeline not cached so a rerun can fill the gaps.", failed)
    return moments


def timeline_for_prompt(moments: list[VisualMoment]) -> list[dict]:
    return [asdict(m) for m in moments]


def from_dicts(items: list[dict] | None) -> list[VisualMoment]:
    return [VisualMoment(**i) for i in (items or [])]


__all__ = [
    "VisualMoment",
    "build_timeline",
    "sample_times",
    "plan_sheets",
]
