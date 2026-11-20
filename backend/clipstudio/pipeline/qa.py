"""Self-QA of a finished clip.

Per clip, six frames are sampled from the *final mp4* and checked:

1. numerically: black bands (a run of near-black rows / columns longer than 4% of the
   frame), and a caption box sitting on a detected face (caption position comes from
   the ASS file that was burned in);
2. with one vision call on a contact sheet of the six frames plus the hook, title,
   caption and the campaign's forbidden list: subject cut off, captions covering
   faces, hook not matching the scene, campaign-rule violations.

This module only judges. The runner applies the single automatic fix per issue and
re-checks once; nothing here (or in the runner's QA stage) may fail a job.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

N_FRAMES = 6
FRAME_WIDTH = 360
#: A near-black row/column run longer than this share of the frame is a band.
BAND_FRACTION = 0.04
ROW_MEAN_MAX = 20.0
ROW_P99_MAX = 40.0
#: Share of a face covered by the caption box that counts as "caption over a face".
FACE_COVER = 0.2

# Issue codes (stable: the runner keys its fixes on them).
BLACK_BANDS = "black_bands"
CAPTION_ON_FACE = "caption_on_face"
SUBJECT_CUT = "subject_cut"
HOOK_MISMATCH = "hook_mismatch"
RULE_VIOLATION = "rule_violation"
OTHER = "other"


@dataclass
class QAResult:
    issues: list[dict[str, str]] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    vision_ran: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.issues

    def codes(self) -> set[str]:
        return {i["code"] for i in self.issues}

    def add(self, code: str, message: str) -> None:
        if not any(i["code"] == code and i["message"] == message for i in self.issues):
            self.issues.append({"code": code, "message": message})

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "pass" if self.ok else "warn",
            "issues": self.issues,
            "fixes": self.fixes,
            "vision": self.vision_ran,
            "metrics": self.metrics,
        }


# --------------------------------------------------------------------------
# Black bands
# --------------------------------------------------------------------------


def _longest_run(mask: np.ndarray) -> int:
    best = run = 0
    for v in mask:
        run = run + 1 if v else 0
        best = max(best, run)
    return best


def band_fractions(gray: np.ndarray) -> tuple[float, float]:
    """Longest near-black run of rows and of columns, as ``(row_fraction, col_fraction)``.

    ``gray`` is a 2-D luma array. A frame that is black everywhere (fade, title card)
    returns (0, 0): that is content, not a band.
    """
    h, w = gray.shape[:2]
    g = gray.astype(np.float32)
    rows = (g.mean(axis=1) <= ROW_MEAN_MAX) & (np.percentile(g, 99, axis=1) <= ROW_P99_MAX)
    cols = (g.mean(axis=0) <= ROW_MEAN_MAX) & (np.percentile(g, 99, axis=0) <= ROW_P99_MAX)
    if rows.all() or cols.all():
        return 0.0, 0.0
    return _longest_run(rows) / h, _longest_run(cols) / w


def detect_black_bands(frames_gray: list[np.ndarray]) -> tuple[float, float]:
    """Median (over frames) longest band fractions ``(rows, cols)``."""
    if not frames_gray:
        return 0.0, 0.0
    fr = [band_fractions(g) for g in frames_gray]
    return float(np.median([f[0] for f in fr])), float(np.median([f[1] for f in fr]))


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------


def sample_frames(video: Path, n: int = N_FRAMES, width: int = FRAME_WIDTH) -> list[tuple[float, Any]]:
    """``[(t, BGR frame)]`` evenly spread over 8%..92% of the video."""
    import cv2

    from .. import resources

    resources.limit_cv2()
    cap = cv2.VideoCapture(str(video))
    out: list[tuple[float, Any]] = []
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        if total <= 0:
            return out
        for i in range(n):
            idx = int(total * (0.08 + 0.84 * i / max(1, n - 1)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            if w > width:
                frame = cv2.resize(frame, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
            out.append((idx / fps, frame))
    finally:
        cap.release()
    return out


# --------------------------------------------------------------------------
# Captions vs faces
# --------------------------------------------------------------------------

_POS = re.compile(r"\\pos\((-?[\d.]+),(-?[\d.]+)\)")
_TAGS = re.compile(r"\{[^}]*\}")


def caption_box_at(ass_path: Path, t: float, width: int, height: int) -> tuple[float, float, float, float] | None:
    """Caption block rectangle (px, output space) on screen at ``t``; None when none is shown.

    Position comes from the burned-in ASS file: ``\\pos`` when the placement logic
    moved the caption, else the style's bottom margin.
    """
    import pysubs2

    try:
        subs = pysubs2.load(str(ass_path), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.debug("QA could not read %s: %s", ass_path, exc)
        return None
    ms = t * 1000
    for event in subs.events:
        if event.style not in ("Cap", "CapSeam") or not (event.start <= ms < event.end):
            continue
        style = subs.styles.get(event.style)
        px = float(style.fontsize) if style else height * 0.045
        plain = _TAGS.sub("", event.text).replace("\\n", "\\N")
        lines = plain.split("\\N")
        box_w = min(width * 0.95, max(len(line) for line in lines) * px * 0.55)
        box_h = len(lines) * px * 1.2
        m = _POS.search(event.text)
        if event.style == "CapSeam":
            cy = float(m.group(2)) if m else height * 0.4
            bottom = cy + box_h / 2
        elif m:
            bottom = float(m.group(2))
        else:
            bottom = height - (style.marginv if style else height * 0.24)
        cx = float(m.group(1)) if m else width / 2
        return cx - box_w / 2, bottom - box_h, cx + box_w / 2, bottom
    return None


def face_boxes(frames_bgr: list[Any]) -> list[list[tuple[float, float, float, float]]] | None:
    """Per frame, normalised ``(x0, y0, x1, y1)`` face boxes; None when detection is unavailable."""
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions, RunningMode

        from .. import models

        model_file = models.ensure("face_landmarker")
    except Exception as exc:  # noqa: BLE001
        log.info("QA face check unavailable: %s", exc)
        return None
    options = FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model_file)),
        running_mode=RunningMode.IMAGE,
        num_faces=3,
        min_face_detection_confidence=0.4,
    )
    out: list[list[tuple[float, float, float, float]]] = []
    with FaceLandmarker.create_from_options(options) as landmarker:
        for frame in frames_bgr:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            boxes = []
            for face in result.face_landmarks or []:
                xs, ys = [p.x for p in face], [p.y for p in face]
                boxes.append((min(xs), min(ys), max(xs), max(ys)))
            out.append(boxes)
    return out


def box_covers_face(
    caption: tuple[float, float, float, float], face: tuple[float, float, float, float]
) -> float:
    """Share of the face rectangle covered by the caption rectangle (same units)."""
    w = min(caption[2], face[2]) - max(caption[0], face[0])
    h = min(caption[3], face[3]) - max(caption[1], face[1])
    area = (face[2] - face[0]) * (face[3] - face[1])
    return (w * h / area) if w > 0 and h > 0 and area > 0 else 0.0


# --------------------------------------------------------------------------
# Numeric checks
# --------------------------------------------------------------------------


def numeric_checks(video: Path, ass_path: Path | None, result: QAResult) -> list[tuple[float, Any]]:
    """Black bands + caption-over-face. Returns the sampled frames for the vision step."""
    import cv2

    frames = sample_frames(video)
    if not frames:
        return []
    h, w = frames[0][1].shape[:2]
    rows, cols = detect_black_bands([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for _, f in frames])
    result.metrics.update(band_rows=round(rows, 3), band_cols=round(cols, 3))
    if rows > BAND_FRACTION:
        result.add(BLACK_BANDS, f"Black bands cover {rows * 100:.0f}% of the frame height.")
    if cols > BAND_FRACTION:
        result.add(BLACK_BANDS, f"Black bands cover {cols * 100:.0f}% of the frame width.")

    if ass_path is not None and ass_path.exists():
        faces = face_boxes([f for _, f in frames])
        if faces is not None:
            hits = 0
            for (t, _), boxes in zip(frames, faces, strict=True):
                cap = caption_box_at(ass_path, t, w, h)
                if cap is None:
                    continue
                for x0, y0, x1, y1 in boxes:
                    if box_covers_face(cap, (x0 * w, y0 * h, x1 * w, y1 * h)) >= FACE_COVER:
                        hits += 1
                        break
            result.metrics["caption_face_frames"] = hits
            if hits:
                result.add(CAPTION_ON_FACE, f"Caption covers a face in {hits} of {len(frames)} sampled frames.")
    return frames


# --------------------------------------------------------------------------
# Vision review
# --------------------------------------------------------------------------

VISION_PROMPT = """This image is a contact sheet of {n} frames from one finished vertical short video
(3 columns, read left to right then top to bottom, each tile labelled #n).
Hook shown on screen: "{hook}"
Title: "{title}"
Post caption: "{caption}"
Campaign rules the clip must respect (forbidden): {forbidden}
All footage in this video IS the campaign's approved source material (it comes from the
campaign's own title / brief). Do NOT judge copyright, where the footage comes from, or whether
it is original; only check visual / format problems and brief rules that are visually checkable.

Check ONLY for clear problems:
- subject_cut: a person's face/head or the main subject is cut off by the frame edge
- caption_on_face: burned-in caption text covers a face
- hook_mismatch: the hook does not match what the scenes show
- rule_violation: the clip breaks one of the forbidden rules above
Reply with ONLY JSON: {{"ok": true|false, "issues": [{{"type": "subject_cut|caption_on_face|hook_mismatch|rule_violation|other", "detail": "short"}}]}}.
ok must be true when there is no clear problem."""


_SOURCE_JUDGEMENT = re.compile(
    r"copyright|another film|other film|existing film|original footage|different (?:film|movie|show)|"
    r"from (?:a|an|the) (?:\w+ )?(?:film|movie|show|franchise)|licen[cs]",
    re.IGNORECASE,
)


def parse_vision(raw: str) -> list[dict[str, str]] | None:
    """Issues from the vision reply; [] when ok; None when unparseable."""
    from ..providers.base import extract_json_object

    try:
        data = extract_json_object(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    issues = []
    for item in data.get("issues") or []:
        if isinstance(item, dict):
            code = str(item.get("type") or OTHER).strip().lower()
            detail = str(item.get("detail") or item.get("message") or "").strip()
        else:
            code, detail = OTHER, str(item).strip()
        if code not in (SUBJECT_CUT, CAPTION_ON_FACE, HOOK_MISMATCH, RULE_VIOLATION):
            code = OTHER
        if detail and not _SOURCE_JUDGEMENT.search(detail):
            issues.append({"code": code, "message": detail[:200]})
    if data.get("ok") is True and not issues:
        return []
    return issues if issues or data.get("ok") is False else []


async def vision_review(
    frames: list[tuple[float, Any]],
    *,
    hook: str,
    title: str,
    caption: str,
    forbidden: list[str],
    chat=None,
) -> list[dict[str, str]] | None:
    """One vision call over the contact sheet. None when unavailable or failed."""
    from .. import config
    from . import visual

    api_key = config.nvidia_api_key()
    if chat is None and not api_key:
        return None
    if not frames:
        return None
    import cv2

    with tempfile.TemporaryDirectory(prefix="qa_") as tmp:
        tiles: list[tuple[float, Path]] = []
        for i, (t, frame) in enumerate(frames):
            p = Path(tmp) / f"f{i}.jpg"
            cv2.imwrite(str(p), frame)
            tiles.append((t, p))
        sheet = visual.build_sheet(tiles)
    prompt = VISION_PROMPT.format(
        n=len(frames),
        hook=hook or "(none)",
        title=title or "(none)",
        caption=" ".join((caption or "").split())[:300] or "(none)",
        forbidden="; ".join(forbidden) if forbidden else "(none)",
    )
    try:
        raw = await (chat or visual._chat)(visual.vision_model(), prompt, sheet, api_key)
    except Exception as exc:  # noqa: BLE001 - QA never fails a job
        log.warning("QA vision call failed: %s", exc)
        return None
    return parse_vision(raw)
