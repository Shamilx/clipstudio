"""Clip quality checks. Each check is cheap: low-res decodes and 4 sampled frames."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..pipeline.ffmpeg import ffmpeg_path, probe

log = logging.getLogger(__name__)

MIN_DURATION_S = 10.0
MAX_DURATION_S = 60.0
LUFS_RANGE = (-18.0, -10.0)
MAX_TRUE_PEAK = -1.0
MIN_FACE_HEIGHT = 0.06  # fraction of frame height
HOOK_MIN = 5
_NO_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class QualityReport:
    status: str = "ready"
    score: int = 100
    issues: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def add(self, code: str, message: str, severity: str) -> None:
        self.issues.append({"code": code, "message": message, "severity": severity})

    def to_dict(self) -> dict:
        return asdict(self)

    def finalize(self) -> "QualityReport":
        sev = [i["severity"] for i in self.issues]
        self.score = max(0, 100 - 45 * sev.count("error") - 12 * sev.count("warn"))
        self.status = "rejected" if "error" in sev else "review" if "warn" in sev else "ready"
        return self


def loudness_filter(target: float = -14) -> str:
    """ffmpeg audio filter that normalises to ``target`` LUFS with a -1.5 dBTP ceiling."""
    return f"loudnorm=I={target:g}:TP=-1.5:LRA=11"


def _ffmpeg(args: list[str], timeout: float = 60) -> str:
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostats", *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=_NO_FLAGS,
    )
    return proc.stderr


# -- individual checks -----------------------------------------------------


def _check_video_pass(video: Path, duration: float) -> dict:
    """Black/frozen detection on a 240px-wide decode."""
    vf = "scale=240:-2,blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-60dB:d=2"
    err = _ffmpeg(["-i", str(video), "-an", "-vf", vf, "-f", "null", "-"])
    black = sum(float(m) for m in re.findall(r"black_duration:([\d.]+)", err))
    frozen = sum(float(m) for m in re.findall(r"freeze_duration:\s*([\d.]+)", err))
    # A freeze that runs to the end of the clip never prints a duration.
    starts = re.findall(r"freeze_start:\s*([\d.]+)", err)
    ends = re.findall(r"freeze_end:\s*([\d.]+)", err)
    if len(starts) > len(ends) and duration:
        frozen += max(0.0, duration - float(starts[-1]))
    return {"black_s": round(black, 2), "frozen_s": round(frozen, 2)}


def _check_audio_pass(video: Path) -> dict:
    err = _ffmpeg(["-i", str(video), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    summary = err[err.rfind("Summary:") :] if "Summary:" in err else ""
    lufs = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
    peak = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)

    def num(m):
        if not m:
            return None
        return -120.0 if "inf" in m.group(1) else float(m.group(1))

    return {"lufs": num(lufs), "true_peak": num(peak)}


def _sample_frames(video: Path, n: int = 4, width: int = 480):
    import cv2

    from .. import resources

    resources.limit_cv2()
    cap = cv2.VideoCapture(str(video))
    frames = []
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            return frames
        for i in range(n):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (0.1 + 0.8 * i / max(1, n - 1))))
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            if w > width:
                frame = cv2.resize(frame, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
            frames.append(frame)
    finally:
        cap.release()
    return frames


def _faces_in_frames(frames) -> int | None:
    """Count frames with a large-enough face; None if detection unavailable."""
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python.vision import (
            FaceLandmarker,
            FaceLandmarkerOptions,
            RunningMode,
        )

        from .. import models

        model_file = models.ensure("face_landmarker")
    except Exception as exc:  # noqa: BLE001
        log.warning("Face check unavailable: %s", exc)
        return None
    opts = FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model_file)),
        running_mode=RunningMode.IMAGE,
        num_faces=2,
        min_face_detection_confidence=0.4,
    )
    hits = 0
    with FaceLandmarker.create_from_options(opts) as lm:
        for frame in frames:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            for face in res.face_landmarks or []:
                ys = [p.y for p in face]
                if max(ys) - min(ys) >= MIN_FACE_HEIGHT:
                    hits += 1
                    break
    return hits


def _caption_zone_hits(frames, safe_bottom: float) -> int:
    """Frames where bright text-like pixels sit inside the bottom ``safe_bottom`` strip."""
    import cv2
    import numpy as np

    hits = 0
    for frame in frames:
        h = frame.shape[0]
        strip = frame[int(h * (1 - safe_bottom)) :]
        hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
        white = (hsv[..., 2] > 235) & (hsv[..., 1] < 40)
        yellow = (hsv[..., 2] > 200) & (hsv[..., 1] > 150) & (abs(hsv[..., 0].astype(int) - 27) < 8)
        mask = (white | yellow).astype("uint8")
        frac = float(np.mean(mask))
        if frac > 0.004:
            # Text is blobby: many small components, not a bright wall.
            _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
            blobs = [s for s in stats[1:] if 6 <= s[cv2.CC_STAT_AREA] <= 0.02 * mask.size]
            if len(blobs) >= 8 and frac < 0.35:
                hits += 1
    return hits


# -- hook scoring ----------------------------------------------------------

_HOOK_SYSTEM = (
    "You rate the opening of a short-form vertical video. Given the first seconds of "
    "transcript and the hook line, reply with ONLY an integer 0-10: how strongly it "
    "grabs attention (curiosity, tension, clear promise)."
)


def score_hook(text: str, hook: str, timeout: float = 30.0) -> int | None:
    """One cheap LLM call. Returns 0-10, or None on any failure (never raises)."""
    if not (text or "").strip() and not (hook or "").strip():
        return None
    try:
        from ..providers import build_provider
        from ..providers.base import DetectionConfig

        provider = build_provider()
        user = f"Opening transcript: {(text or '').strip()}\nHook line: {(hook or '').strip()}\nScore 0-10:"
        cfg = DetectionConfig(temperature=0.0)

        async def go() -> str:
            return await asyncio.wait_for(provider._complete(_HOOK_SYSTEM, user, cfg), timeout)

        box: dict = {}

        def runner() -> None:
            try:
                box["r"] = asyncio.run(go())
            except Exception as exc:  # noqa: BLE001
                box["e"] = exc

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout + 2)
        if "r" not in box:
            log.info("Hook scoring skipped: %s", box.get("e", "timeout"))
            return None
        m = re.search(r"\d+", box["r"])
        return min(10, int(m.group())) if m else None
    except Exception as exc:  # noqa: BLE001
        log.info("Hook scoring skipped: %s", exc)
        return None


def _opening_text(words, seconds: float = 3.0) -> str:
    out = []
    for w in words or []:
        get = (lambda k, w=w: w.get(k)) if isinstance(w, dict) else (lambda k, w=w: getattr(w, k, None))
        if (get("start") or 0) <= seconds:
            out.append(str(get("text") or ""))
    return " ".join(out)


# -- main entry ------------------------------------------------------------


def check_clip(
    video_path: Path,
    *,
    words=None,
    hook_text: str | None = None,
    safe_bottom: float = 0.18,
) -> QualityReport:
    video_path = Path(video_path)
    rep = QualityReport()
    if not video_path.exists():
        rep.add("missing_file", "Exported file not found.", "error")
        return rep.finalize()
    try:
        info = probe(video_path)
        duration = float(info.duration_s or 0)
    except Exception as exc:  # noqa: BLE001
        rep.add("unreadable", f"Could not read the video: {exc}", "error")
        return rep.finalize()
    rep.metrics["duration_s"] = round(duration, 2)
    has_audio = bool(getattr(info, "has_audio", True))

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_video = pool.submit(_check_video_pass, video_path, duration)
        f_audio = pool.submit(_check_audio_pass, video_path) if has_audio else None
        f_hook = None
        if words or hook_text:
            f_hook = pool.submit(score_hook, _opening_text(words), hook_text or "")
        try:
            frames = _sample_frames(video_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Frame sampling failed: %s", exc)
            frames = []
        face_hits = _faces_in_frames(frames) if frames else None
        cap_hits = _caption_zone_hits(frames, safe_bottom) if frames else 0
        vid = f_video.result()
        aud = f_audio.result() if f_audio else None
        hook = f_hook.result() if f_hook else None

    if not MIN_DURATION_S <= duration <= MAX_DURATION_S:
        rep.add(
            "duration",
            f"Length {duration:.0f}s is outside the {MIN_DURATION_S:.0f}-{MAX_DURATION_S:.0f}s Shorts range.",
            "warn",
        )

    rep.metrics.update(vid)
    if duration and vid["black_s"] / duration > 0.05:
        rep.add("black_frames", f"{vid['black_s']:.1f}s of black frames.", "error")
    if duration and vid["frozen_s"] / duration > 0.15:
        rep.add("frozen_frames", f"{vid['frozen_s']:.1f}s of frozen video.", "error")

    rep.metrics["frames_sampled"] = len(frames)
    if face_hits is not None and frames:
        rep.metrics["face_frames"] = face_hits
        need = 3 if len(frames) >= 4 else max(1, len(frames) - 1)
        if face_hits < need:
            rep.add(
                "face",
                f"Face clearly visible in only {face_hits}/{len(frames)} sampled frames.",
                "warn",
            )
    if frames:
        rep.metrics["caption_zone_frames"] = cap_hits
        if cap_hits >= 2:
            rep.add(
                "caption_safe_zone",
                f"Bright text in the bottom {int(safe_bottom * 100)}% (covered by app UI).",
                "warn",
            )

    if not has_audio:
        rep.add("no_audio", "Clip has no audio track.", "error")
    elif aud:
        rep.metrics.update(aud)
        lufs, tp = aud["lufs"], aud["true_peak"]
        if lufs is not None and lufs <= -60:
            rep.add("silent", "Audio is silent.", "error")
        else:
            if lufs is not None and not LUFS_RANGE[0] <= lufs <= LUFS_RANGE[1]:
                rep.add(
                    "loudness",
                    f"Loudness {lufs:.1f} LUFS is outside {LUFS_RANGE[0]:.0f}..{LUFS_RANGE[1]:.0f}.",
                    "warn",
                )
            if tp is not None and tp > MAX_TRUE_PEAK:
                rep.add("true_peak", f"True peak {tp:.1f} dBTP exceeds {MAX_TRUE_PEAK:.0f}.", "warn")

    if hook is not None:
        rep.metrics["hook_score"] = hook
        if hook < HOOK_MIN:
            rep.add("weak_hook", f"Hook scored {hook}/10 - opening may not grab attention.", "warn")
    return rep.finalize()


# -- job-level -------------------------------------------------------------


def _transcript_words(clip):
    from ..api.clips import _load_transcript

    t = _load_transcript(clip.job_id)
    return [
        {"text": w.text, "start": w.start - clip.start_s, "end": w.end - clip.start_s}
        for w in t.slice(clip.start_word, clip.end_word)
    ]


def check_clip_by_id(clip_id: str) -> dict | None:
    """Check a clip's latest export and persist the result. None if clip unknown."""
    from ..db import store as dbstore
    from . import store

    clip = dbstore.get_clip(clip_id)
    if clip is None:
        return None
    exports = dbstore.list_exports(clip_id)
    if not exports:
        raise LookupError("Clip has no export yet.")
    exp = exports[0]
    words = None
    edit = dbstore.get_clip_edit(clip_id)
    if edit is not None and edit.edited_words:
        words = edit.edited_words
    else:
        try:
            words = _transcript_words(clip)
        except Exception as exc:  # noqa: BLE001
            log.info("No transcript words for hook check: %s", exc)
    rep = check_clip(Path(exp.path), words=words, hook_text=clip.hook).to_dict()
    previous = store.get(clip_id)
    if previous and previous.get("qa"):
        rep["qa"] = previous["qa"]  # keep the self-QA outcome from the pipeline
    store.save(clip_id, clip.job_id, exp.id, rep)
    return store.get(clip_id)


def run_for_job(job_id: str) -> list[dict]:
    """Check every exported clip of a job; returns the stored reports."""
    from ..db import store as dbstore

    results = []
    for clip in dbstore.list_clips(job_id):
        if not dbstore.list_exports(clip.id):
            continue
        try:
            r = check_clip_by_id(clip.id)
            if r:
                results.append(r)
        except Exception as exc:  # noqa: BLE001
            log.warning("Quality check failed for %s: %s", clip.id, exc)
    return results
