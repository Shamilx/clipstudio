"""Reframe stage — 16:9 source to a 9:16 crop path that follows the speaker.

Pipeline for one clip:

1. Split into shots. Crop paths never interpolate across a cut.
2. Sample face landmarks across the clip.
3. Link observations into per-person tracks.
4. Map diarized speakers onto tracks once, then let turn boundaries drive framing.
5. Per shot, pick a strategy — TRACK, WIDE, or GENERAL.
6. Build a raw crop path, smooth it, and emit segments.

The quality bar is "never jarring": no visible jitter, no cut-off faces, the
speaker on screen for essentially all of their speaking time. Every default here
is biased toward stillness — a locked frame that is slightly off-centre beats a
frame that is always correct and always moving.
"""

from __future__ import annotations

import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from .. import ffmpeg
from ..transcript import Transcript
from .croppath import (
    CropKeyframe,
    CropPath,
    CropSegment,
    Strategy,
    centre_crop,
    target_crop_size,
)
from .faces import FaceDetectionUnavailable, FaceObservation, sample_faces
from .scenes import Shot, detect_shots
from .smoothing import SmoothingConfig, smooth_series
from .speaker import assign_speakers, map_tracks_to_speakers
from .tracker import FaceTrack, build_tracks

log = logging.getLogger(__name__)

__all__ = [
    "CropPath",
    "CropSegment",
    "ReframeConfig",
    "Strategy",
    "build_crop_path",
]

#: Where the eye line sits within the crop, as a fraction of crop height.
#: Portrait convention puts the eyes near the upper third; 0.38 reads as
#: composed without leaving the chin tight to the bottom edge.
EYE_LINE_RATIO = 0.38

#: A face wider than this fraction of the crop is already a close-up; don't
#: track it, lock the frame.
LOCK_IF_FACE_WIDER_THAN = 0.55

#: Each face is padded by this multiple of its own width on both sides (shoulders).
#: If any padded face would be cut by the crop window edge, fit the whole frame.
FACE_SIDE_PAD = 0.6

#: With no usable face, show the whole frame over a blurred background instead of
#: a centre crop (which tends to show an empty set and cut off signs/people).
FIT_WHEN_NO_FACE = True

#: Slow push-in applied to shots with no face at all, to stop a static wide from
#: feeling like a still image. Off by default — see the note in export._zoom_filter.
GENERAL_ZOOM = 0.0


@dataclass
class ReframeConfig:
    aspect_w: int = 9
    aspect_h: int = 16
    sample_fps: float = 2.5
    #: Frames are shrunk to this width before face detection / shot detection.
    analysis_width: int = 480
    smoothing: SmoothingConfig | None = None
    #: Skip face detection entirely and centre-crop everything.
    centre_only: bool = False
    #: Active picture area ``(x, y, w, h)`` inside the real frame (baked-in black
    #: bars removed). All geometry below is computed relative to it.
    active: tuple[int, int, int, int] | None = None


def _fit_segment(start_s: float, end_s: float, source_w: int, source_h: int, strategy: Strategy):
    return CropSegment(
        start_s=start_s,
        end_s=end_s,
        width=source_w,
        height=source_h,
        keyframes=[CropKeyframe(t=start_s, x=0.0, y=0.0)],
        strategy=strategy,
        fit=True,
    )


def _no_face_path(source_w: int, source_h: int, duration: float, config: ReframeConfig) -> CropPath:
    """Whole-clip path for when no face is available."""
    if FIT_WHEN_NO_FACE:
        return CropPath(
            source_width=source_w,
            source_height=source_h,
            segments=[_fit_segment(0.0, duration, source_w, source_h, Strategy.GENERAL)],
        )
    return centre_crop(
        source_w,
        source_h,
        duration,
        aspect_w=config.aspect_w,
        aspect_h=config.aspect_h,
        zoom=GENERAL_ZOOM,
    )


def build_crop_path(
    video: Path,
    *,
    start_s: float,
    end_s: float,
    transcript: Transcript | None = None,
    config: ReframeConfig | None = None,
    on_frame=None,
) -> CropPath:
    """Compute the crop path for one clip.

    Falls back to a centred crop whenever face detection is unavailable or finds
    nothing — a centre crop is an acceptable result, an exception is not.

    Preconditions:
        video exists; 0 <= start_s < end_s <= source duration.
    """
    config = config or ReframeConfig()
    duration = end_s - start_s

    info = ffmpeg.probe(video)
    if not info.has_video or not info.width or not info.height:
        raise ValueError(f"{video.name} has no video stream to reframe.")

    source_w, source_h = info.width, info.height
    active = config.active
    off_x = off_y = 0
    if active is not None:
        off_x, off_y, source_w, source_h = active
    crop_w, crop_h = target_crop_size(source_w, source_h, config.aspect_w, config.aspect_h)

    if config.centre_only:
        return _with_active(
            centre_crop(
                source_w, source_h, duration, aspect_w=config.aspect_w, aspect_h=config.aspect_h
            ),
            active,
        )

    # Face sampling and shot detection are independent decodes of the same range;
    # both release the GIL inside OpenCV, so run them side by side (this stage is
    # decode-bound and the target CPU has idle cores).
    shots_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shots")
    shots_future = shots_pool.submit(
        detect_shots, video, start_s=start_s, end_s=end_s, analysis_width=config.analysis_width
    )
    try:
        observations = sample_faces(
            video,
            start_s=start_s,
            end_s=end_s,
            sample_fps=config.sample_fps,
            analysis_width=config.analysis_width,
            on_frame=on_frame,
        )
    except FaceDetectionUnavailable as exc:
        shots_pool.shutdown(wait=False, cancel_futures=True)
        log.warning("Face detection unavailable (%s); using a centre crop.", exc)
        return _with_active(_no_face_path(source_w, source_h, duration, config), active)

    if not observations:
        shots_pool.shutdown(wait=False, cancel_futures=True)
        log.info("No faces detected in %s; using a centre crop.", video.name)
        return _with_active(_no_face_path(source_w, source_h, duration, config), active)

    if active is not None:
        observations = _to_active_space(observations, off_x, off_y, source_w, source_h)
        if not observations:
            shots_pool.shutdown(wait=False, cancel_futures=True)
            return _with_active(_no_face_path(source_w, source_h, duration, config), active)

    tracks = build_tracks(observations)
    shots = shots_future.result()
    shots_pool.shutdown()

    turns = _clip_relative_turns(transcript, start_s, end_s)
    speaker_map = map_tracks_to_speakers(tracks, turns) if turns else {}

    segments: list[CropSegment] = []
    for shot in shots:
        segments.extend(
            _segments_for_shot(
                shot,
                tracks=tracks,
                turns=turns,
                speaker_map=speaker_map,
                source_w=source_w,
                source_h=source_h,
                crop_w=crop_w,
                crop_h=crop_h,
                config=config,
            )
        )

    if not segments:
        return _with_active(_no_face_path(source_w, source_h, duration, config), active)

    # Guarantee the segments tile the clip exactly; a gap or overlap would
    # desync the concatenated render from the audio.
    segments[0].start_s = 0.0
    segments[-1].end_s = duration
    for index in range(len(segments) - 1):
        segments[index].end_s = segments[index + 1].start_s

    segments = [s for s in segments if s.duration_s > 0.01]

    log.info(
        "Reframed %s into %d segment(s): %s",
        video.name,
        len(segments),
        ", ".join(s.strategy.value for s in segments),
    )
    return CropPath(
        source_width=source_w,
        source_height=source_h,
        segments=segments,
        speaker=_dominant_speaker(tracks),
        active=active,
        faces=[
            {
                "t": round(o.t, 2),
                "x0": round(o.left, 1),
                "y0": round(o.top, 1),
                "x1": round(o.right, 1),
                "y1": round(o.bottom, 1),
            }
            for o in observations
        ],
    )


def _with_active(path: CropPath, active: tuple[int, int, int, int] | None) -> CropPath:
    path.active = active
    return path


def _to_active_space(
    observations: list[FaceObservation], off_x: float, off_y: float, w: int, h: int
) -> list[FaceObservation]:
    """Shift face observations into active-area coordinates; drop faces outside it."""
    out: list[FaceObservation] = []
    for o in observations:
        cx, cy = o.cx - off_x, o.cy - off_y
        if not (0 <= cx <= w and 0 <= cy <= h):
            continue
        out.append(replace(o, cx=cx, cy=cy, eye_y=o.eye_y - off_y))
    return out


def _dominant_speaker(tracks: list[FaceTrack]) -> dict[str, float] | None:
    """Median face position/size of the most-seen track (the main speaker)."""
    tracks = [t for t in tracks if t.observations]
    if not tracks:
        return None
    best = max(tracks, key=lambda t: len(t.observations))
    obs = best.observations
    return {
        "cx": float(statistics.median(o.cx for o in obs)),
        "eye_y": float(statistics.median(o.eye_y for o in obs)),
        "w": float(statistics.median(o.width for o in obs)),
    }


def _clip_relative_turns(
    transcript: Transcript | None, start_s: float, end_s: float
) -> list[tuple[str, float, float]]:
    """Rebase diarization turns onto the clip's timeline, dropping non-overlapping ones."""
    if transcript is None or not transcript.has_diarization:
        return []

    turns: list[tuple[str, float, float]] = []
    for speaker, turn_start, turn_end in transcript.speaker_turns():
        overlap_start = max(turn_start, start_s)
        overlap_end = min(turn_end, end_s)
        if overlap_end > overlap_start:
            turns.append((speaker, overlap_start - start_s, overlap_end - start_s))
    return turns


def _segments_for_shot(
    shot: Shot,
    *,
    tracks: list[FaceTrack],
    turns: list[tuple[str, float, float]],
    speaker_map: dict[str, FaceTrack],
    source_w: int,
    source_h: int,
    crop_w: int,
    crop_h: int,
    config: ReframeConfig,
) -> list[CropSegment]:
    assignments = assign_speakers(
        tracks,
        shot_start_s=shot.start_s,
        shot_end_s=shot.end_s,
        turns=turns,
        speaker_map=speaker_map,
    )

    segments: list[CropSegment] = []
    for assignment in assignments:
        visible = [
            t for t in tracks if t.observations_between(assignment.start_s, assignment.end_s)
        ]

        if assignment.track is not None:
            segments.append(
                _track_segment(
                    assignment.track,
                    start_s=assignment.start_s,
                    end_s=assignment.end_s,
                    source_w=source_w,
                    source_h=source_h,
                    crop_w=crop_w,
                    crop_h=crop_h,
                    config=config,
                )
            )
        elif visible:
            segments.append(
                _wide_segment(
                    visible,
                    start_s=assignment.start_s,
                    end_s=assignment.end_s,
                    source_w=source_w,
                    source_h=source_h,
                    crop_w=crop_w,
                    crop_h=crop_h,
                )
            )
        else:
            segments.append(
                _general_segment(
                    start_s=assignment.start_s,
                    end_s=assignment.end_s,
                    source_w=source_w,
                    source_h=source_h,
                    crop_w=crop_w,
                    crop_h=crop_h,
                )
            )

    return segments


def _track_segment(
    track: FaceTrack,
    *,
    start_s: float,
    end_s: float,
    source_w: int,
    source_h: int,
    crop_w: int,
    crop_h: int,
    config: ReframeConfig,
) -> CropSegment:
    """Frame a single subject, tracking them or locking on if they barely move."""
    observations = track.observations_between(start_s, end_s)
    if not observations:
        return _general_segment(
            start_s=start_s,
            end_s=end_s,
            source_w=source_w,
            source_h=source_h,
            crop_w=crop_w,
            crop_h=crop_h,
        )

    raw = [
        (
            o.t,
            _clamp(o.cx - crop_w / 2, 0, source_w - crop_w),
            _clamp(o.eye_y - EYE_LINE_RATIO * crop_h, 0, source_h - crop_h),
        )
        for o in observations
    ]

    mean_face_width = sum(o.width for o in observations) / len(observations)
    spread_x = max(x for _, x, _ in raw) - min(x for _, x, _ in raw)

    # A close-up, or a subject who barely moves, is better locked than tracked.
    should_lock = (
        mean_face_width > crop_w * LOCK_IF_FACE_WIDER_THAN
        or spread_x < (config.smoothing or SmoothingConfig()).dead_zone_px
    )

    if should_lock or len(raw) < 3:
        x = sum(item[1] for item in raw) / len(raw)
        y = sum(item[2] for item in raw) / len(raw)
        keyframes = [CropKeyframe(t=start_s, x=x, y=y)]
    else:
        smoothing = config.smoothing or SmoothingConfig()
        xs = smooth_series([(t, x) for t, x, _ in raw], smoothing)
        ys = smooth_series([(t, y) for t, _, y in raw], smoothing)
        keyframes = [CropKeyframe(t=t, x=x, y=y) for (t, x), (_, y) in zip(xs, ys, strict=True)]
        # Anchor the ends so the expression covers the whole segment.
        if keyframes[0].t > start_s:
            keyframes.insert(0, CropKeyframe(start_s, keyframes[0].x, keyframes[0].y))
        if keyframes[-1].t < end_s:
            keyframes.append(CropKeyframe(end_s, keyframes[-1].x, keyframes[-1].y))

    return CropSegment(
        start_s=start_s,
        end_s=end_s,
        width=crop_w,
        height=crop_h,
        keyframes=keyframes,
        strategy=Strategy.TRACK,
    )


def _wide_segment(
    visible: list[FaceTrack],
    *,
    start_s: float,
    end_s: float,
    source_w: int,
    source_h: int,
    crop_w: int,
    crop_h: int,
) -> CropSegment:
    """Hold several faces at once — locked, never panning.

    Panning between two people who are both on screen is the most obviously
    robotic thing an auto-reframer does. If they fit, lock on their centroid; if
    they don't, fit the whole frame rather than cutting someone out.
    """
    observations: list[FaceObservation] = [
        o for track in visible for o in track.observations_between(start_s, end_s)
    ]
    if not observations:
        return _general_segment(
            start_s=start_s,
            end_s=end_s,
            source_w=source_w,
            source_h=source_h,
            crop_w=crop_w,
            crop_h=crop_h,
        )

    left = min(o.left for o in observations)
    right = max(o.right for o in observations)
    centre_x = (left + right) / 2
    crop_x = _clamp(centre_x - crop_w / 2, 0, source_w - crop_w)
    # Every face plus its shoulders must sit fully inside the crop window.
    for o in observations:
        pad = o.width * FACE_SIDE_PAD
        if o.left - pad < crop_x or o.right + pad > crop_x + crop_w:
            return _fit_segment(start_s, end_s, source_w, source_h, Strategy.WIDE)

    eye_y = sum(o.eye_y for o in observations) / len(observations)

    return CropSegment(
        start_s=start_s,
        end_s=end_s,
        width=crop_w,
        height=crop_h,
        keyframes=[
            CropKeyframe(
                t=start_s,
                x=crop_x,
                y=_clamp(eye_y - EYE_LINE_RATIO * crop_h, 0, source_h - crop_h),
            )
        ],
        strategy=Strategy.WIDE,
    )


def _general_segment(
    *,
    start_s: float,
    end_s: float,
    source_w: int,
    source_h: int,
    crop_w: int,
    crop_h: int,
) -> CropSegment:
    """No usable face — fit the whole frame (or centre crop if FIT_WHEN_NO_FACE is off)."""
    if FIT_WHEN_NO_FACE:
        return _fit_segment(start_s, end_s, source_w, source_h, Strategy.GENERAL)
    return CropSegment(
        start_s=start_s,
        end_s=end_s,
        width=crop_w,
        height=crop_h,
        keyframes=[CropKeyframe(t=start_s, x=(source_w - crop_w) / 2, y=(source_h - crop_h) / 2)],
        strategy=Strategy.GENERAL,
        zoom=GENERAL_ZOOM,
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high)) if high > low else max(0.0, low)
