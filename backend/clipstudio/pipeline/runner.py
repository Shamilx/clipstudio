"""Pipeline orchestration.

Runs a job stage by stage, writing each stage's artifacts into
``work/{job_id}/``. Because every stage's output is a file on disk, a retry
skips everything already done and resumes at the stage that failed — which
matters when stage two took eleven minutes and stage four hit a rate limit.

Progress is reported through a callback rather than written directly, so the
same runner serves the CLI (a progress bar) and the web API (an SSE stream).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from .. import activity, campaign, paths, profiles
from ..config import Settings
from ..config import load as load_settings
from ..db import store
from ..db.models import Clip, Export, Job, Source, new_id, utcnow
from ..db.models import Transcript as TranscriptRow
from ..providers import build_provider, detection_config
from ..providers.base import clean_brief
from ..quality import store as qstore
from . import (
    Stage,
    captions,
    editplan,
    export,
    ffmpeg,
    highlights,
    hooks,
    letterbox,
    prepare,
    qa,
    transcribe,
    visual,
    vocabulary,
)
from .prepare import Silence
from .reframe import ReframeConfig, build_crop_path
from .reframe.croppath import CropPath
from .transcript import Transcript

log = logging.getLogger(__name__)

#: Royalty-free background tracks live here (project root / music).
MUSIC_DIR = Path(__file__).resolve().parents[3] / "music"

#: Weight of each stage in the overall progress bar. Rough proportions of wall
#: time on a mid-range machine — transcription and export dominate.
STAGE_WEIGHTS: dict[Stage, float] = {
    Stage.PREPARE: 0.05,
    Stage.TRANSCRIBE: 0.35,
    Stage.HIGHLIGHTS: 0.15,
    Stage.REFRAME: 0.20,
    Stage.CAPTIONS: 0.02,
    Stage.EXPORT: 0.20,
    Stage.QA: 0.03,
}


class JobCancelled(RuntimeError):
    """The job was cancelled by the user."""


class PipelineError(RuntimeError):
    """A stage failed in a way the user should see."""

    def __init__(self, message: str, *, stage: Stage) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass
class ProgressEvent:
    stage: Stage
    #: Progress within the stage, 0..1.
    stage_progress: float
    #: Progress across the whole job, 0..1.
    overall: float
    message: str = ""


ProgressHandler = Callable[[ProgressEvent], None]


class JobWorkspace:
    """Paths for one job's intermediate artifacts."""

    def __init__(self, job_id: str) -> None:
        self.root = paths.job_work_dir(job_id)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def audio(self) -> Path:
        # A job prepared by an older version keeps its PCM audio.
        legacy = self.root / "audio.wav"
        if legacy.exists() and legacy.stat().st_size > 0:
            return legacy
        return self.root / "audio.ogg"

    @property
    def transcript(self) -> Path:
        return self.root / "transcript.json"

    @property
    def silences(self) -> Path:
        return self.root / "silences.json"

    @property
    def visual(self) -> Path:
        return self.root / "visual.json"

    @property
    def letterbox(self) -> Path:
        return self.root / "letterbox.json"

    @property
    def thumbnails(self) -> Path:
        return self.root / "thumbnails"

    def crop_path(self, clip_id: str) -> Path:
        return self.root / "crops" / f"{clip_id}.json"

    def edit_plan(self, clip_id: str) -> Path:
        return self.root / "plans" / f"{clip_id}.json"

    @property
    def captions_dir(self) -> Path:
        return self.root / "captions"


class PipelineRunner:
    """Executes one job."""

    def __init__(
        self,
        job: Job,
        source: Source,
        *,
        settings: Settings | None = None,
        on_progress: ProgressHandler | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.job = job
        self.source = source
        self.settings = settings or load_settings()
        self.on_progress = on_progress
        self._is_cancelled = is_cancelled or (lambda: False)
        self.workspace = JobWorkspace(job.id)
        self._completed_weight = 0.0
        self._stage_clock = time.monotonic()
        self.activity = activity.ActivityTracker(job.id)
        #: Active picture area (x, y, w, h) when baked-in black bars were found.
        self.active: tuple[int, int, int, int] | None = None
        self.profile: profiles.Profile | None = None
        self._moments: list[visual.VisualMoment] = []
        #: Per clip: the ExportRequest used, so QA can re-render once with a fix.
        self._export_ctx: dict[str, dict] = {}

    # -- campaign ------------------------------------------------------------

    async def _prepare_campaign(self) -> None:
        """Extract the brief's rules once (never fails) and apply job-level overrides."""
        try:
            await campaign.ensure_campaign(self.job, self.settings)
        except Exception as exc:  # noqa: BLE001 - campaign rules are best-effort
            log.warning("Campaign extraction failed: %s", exc)
        rules = campaign.rules_from_job(self.job)
        if rules is not None:
            for warning in rules.warnings:
                log.warning("Campaign brief warning: %s", warning)

    def _rules(self) -> campaign.CampaignRules | None:
        return campaign.rules_from_job(self.job)

    def _brand_safe(self) -> bool:
        rules = self._rules()
        return bool(rules and rules.brand_safe)

    def _explicit(self) -> set[str]:
        return set(self.job.settings.get("explicit") or [])

    def _template_is_explicit(self) -> bool:
        return str(self.job.settings.get("template") or "auto") not in ("", "auto")

    def _max_topical(self) -> int:
        return min(4, self.profile.hashtag_count) if self.profile else 4

    def _finalize_metadata(self, clips: list[Clip]) -> None:
        """Required hashtags/text first, 2-4 clean topical tags; works without a brief too."""
        rules = self._rules()
        for clip in clips:
            if rules is not None:
                campaign.enforce_metadata(rules, clip, max_topical=self._max_topical())
            else:
                clip.hashtags = campaign.clean_hashtags([], clip.hashtags, self._max_topical())
                clip.description = campaign.with_hashtags(clip.description, clip.hashtags)

    # -- content profile ---------------------------------------------------

    def _cut_rate(self) -> float | None:
        """Shot cuts per minute from a keyframe-only scan of (up to) the first 15 minutes."""
        duration = self.source.duration_s or 0.0
        if duration <= 0 or not self.source.has_video:
            return None
        span = min(duration, 900.0)
        try:
            shots = visual.keyframe_shots(Path(self.source.path), span)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cut-rate detection failed: %s", exc)
            return None
        if not shots:
            return None
        return max(0, len(shots) - 1) / (span / 60.0)

    def _resolve_profile(self, transcript: Transcript) -> None:
        """Pick (once) and apply the content profile; explicit settings still win."""
        name = str(self.job.settings.get("profile") or "").strip().lower()
        rules = self._rules()
        if not profiles.is_known(name):
            footage = rules.footage_type if rules else None
            named = profiles.profile_from_footage(footage) if rules else None
            wpm = cuts = None
            if named:
                why = f"from the brief: {footage} footage"
            else:
                self._emit(Stage.HIGHLIGHTS, 0.0, "Choosing content profile: measuring speech and shot cuts")
                minutes = max((self.source.duration_s or transcript.duration or 0.0) / 60.0, 1 / 60)
                wpm = len(transcript.words) / minutes if transcript.words else 0.0
                cuts = self._cut_rate()
                why = (
                    f"auto-detected: {wpm:.0f} words/min, "
                    + (f"{cuts:.0f} cuts/min" if cuts is not None else "cut rate unknown")
                    + (", black bars" if self.active else "")
                )
            name = profiles.choose_profile(
                footage_type=footage, has_brief=rules is not None, words_per_min=wpm,
                letterbox=self.active is not None, cuts_per_min=cuts,
            )  # fmt: skip
            self.job.settings = {**self.job.settings, "profile": name}
            try:
                store.update_job_settings(self.job.id, self.job.settings)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not store the content profile: %s", exc)
        else:
            why = "set for this job"
        self.profile = profiles.load_profile(name)
        profiles.apply_to_edit(self.profile, self.settings.edit, self._explicit())
        self._emit(Stage.HIGHLIGHTS, 0.0, f"Content profile: {self.profile.label} ({why})")
        log.info("Content profile %s (%s).", self.profile.name, why)

    def _template_with_profile(self, template):
        """Profile look on top of an auto-picked template; an explicit template wins."""
        if self.profile is not None and not self._template_is_explicit():
            return profiles.apply_to_template(self.profile, template)
        return template

    # -- names -------------------------------------------------------------

    def _apply_vocabulary(self, transcript: Transcript) -> Transcript:
        """Fix misheard proper nouns using the brief's vocabulary and on-screen names."""
        rules = self._rules()
        vocab = vocabulary.merge(
            rules.vocabulary if rules else [],
            vocabulary.names_from_descriptions([m.desc for m in self._moments]),
        )
        if not vocab:
            return transcript
        self._emit(Stage.HIGHLIGHTS, 0.0, f"Checking names against {len(vocab)} known name(s)")
        changed = vocabulary.correct_words(transcript.words, vocab)
        if changed:
            self.activity.set(f"Corrected {changed} misheard name(s) in the transcript")
            try:
                transcript.save(self.workspace.transcript)
            except OSError as exc:
                log.warning("Could not save the corrected transcript: %s", exc)
        return transcript

    def _scene_text(self, clip: Clip) -> str:
        lines = [
            f"- {m.desc}"
            for m in self._moments
            if clip.start_s - 1.0 <= m.t <= clip.end_s + 1.0
        ]
        return "\n".join(lines[:8])

    # -- progress ----------------------------------------------------------

    def _check_cancelled(self) -> None:
        if self._is_cancelled():
            raise JobCancelled("Job cancelled.")

    def _emit(
        self,
        stage: Stage,
        stage_progress: float,
        message: str = "",
        *,
        overall: float | None = None,
        transient: bool = False,
    ) -> None:
        if overall is None:
            overall = self._completed_weight + STAGE_WEIGHTS[stage] * stage_progress
        overall = min(1.0, overall)
        store.update_job(self.job.id, current_stage=stage.value, progress=round(overall, 4))
        self.activity.stage = stage.value
        if message:
            # Progress ticks repeat the same text with a new percentage; only a
            # genuinely new message opens a log line (see ActivityTracker.set).
            self.activity.set(message, log=transient is False)
        if self.on_progress:
            self.on_progress(
                ProgressEvent(
                    stage=stage,
                    stage_progress=stage_progress,
                    overall=overall,
                    message=message or stage.label,
                )
            )

    def _finish_stage(self, stage: Stage) -> None:
        """Mark a stage complete.

        ``overall`` is passed explicitly rather than derived. Deriving it after
        incrementing the accumulated weight counts the stage twice, so the bar
        jumped past the true total and then snapped backwards when the next
        stage reported 0 — visibly, at every stage boundary.
        """
        self._completed_weight += STAGE_WEIGHTS[stage]
        now = time.monotonic()
        log.info("Stage %s finished in %.1fs.", stage.value, now - self._stage_clock)
        self._stage_clock = now
        self._emit(stage, 1.0, overall=self._completed_weight)

    def _stage_progress(self, stage: Stage, message: str = "") -> Callable[[float], None]:
        def report(fraction: float) -> None:
            self._emit(stage, max(0.0, min(1.0, fraction)), message, transient=True)

        return report

    # -- entry point -------------------------------------------------------

    async def run(self) -> list[Clip]:
        """Run the full pipeline and return the exported clips."""
        store.update_job(self.job.id, status="running", started_at=utcnow(), error=None)
        activity.bind(self.activity)
        self.activity.set("Starting job")

        try:
            await self._prepare_campaign()
            if self.job.settings.get("more"):
                return await self._run_more()
            audio = self._stage_prepare()
            # Silence detection only needs the audio file, so it runs while the
            # (network-bound) transcription is in flight.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="prep") as pool:
                silence_future = pool.submit(self._load_or_detect_silences, audio)
                visual_future = self._start_visual(pool)
                transcript = self._stage_transcribe(audio)
                silences = silence_future.result()
                moments = self._await_visual(visual_future)
            self._moments = moments
            transcript = self._apply_vocabulary(transcript)
            self._resolve_profile(transcript)
            clips = await self._stage_highlights(transcript, silences, moments)
            crop_paths = self._stage_reframe(clips, transcript)
            await self._stage_captions(clips, transcript)
            self._stage_export(clips, transcript, crop_paths)
            await self._stage_qa(clips, transcript)
        except JobCancelled:
            self.activity.set("Cancelled")
            store.update_job(self.job.id, status="cancelled", finished_at=utcnow(), progress=0.0)
            raise
        except Exception as exc:
            log.exception("Job %s failed.", self.job.id)
            self.activity.set(f"Failed: {str(exc)[:160]}")
            store.update_job(self.job.id, status="failed", error=str(exc), finished_at=utcnow())
            raise

        self.activity.set(f"Done: {len(clips)} clip(s) exported")
        store.update_job(self.job.id, status="done", progress=1.0, finished_at=utcnow())
        return clips

    async def _run_more(self) -> list[Clip]:
        """Generate extra clips for a finished job.

        Reuses the cached audio, transcript and silences, asks the model for new
        moments that avoid the existing clips, then runs only the downstream
        stages on the new clips and appends them to the job.
        """
        request = self.job.settings.get("more") or {}
        count = max(1, min(int(request.get("count") or 5), 50))

        audio = self._stage_prepare()
        transcript = self._stage_transcribe(audio)
        silences = self._load_or_detect_silences(audio)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="visual") as pool:
            moments = self._await_visual(self._start_visual(pool))

        self._moments = moments
        transcript = self._apply_vocabulary(transcript)
        self._resolve_profile(transcript)
        new_clips = await self._stage_more_highlights(transcript, silences, count, moments)
        if new_clips:
            crop_paths = self._stage_reframe(new_clips, transcript)
            await self._stage_captions(new_clips, transcript)
            self._stage_export(new_clips, transcript, crop_paths)
            await self._stage_qa(new_clips, transcript)
            error = None
        else:
            error = "No new clips found: the remaining moments overlap existing clips."

        self._clear_more_request()
        self.activity.set(f"Done: {len(new_clips)} new clip(s)")
        store.update_job(
            self.job.id, status="done", progress=1.0, finished_at=utcnow(), error=error
        )
        return new_clips

    def _clear_more_request(self) -> None:
        settings = dict(self.job.settings)
        settings.pop("more", None)
        self.job.settings = settings
        store.update_job_settings(self.job.id, settings)

    # -- visual timeline ---------------------------------------------------

    def _start_visual(self, pool: ThreadPoolExecutor):
        """Start the frame analysis in a worker thread (None when disabled)."""
        if not (self.settings.visual_enabled and self.source.has_video):
            return None
        self._visual_state = (0.0, "Analysing video frames")

        def on_progress(fraction: float, message: str) -> None:
            self._visual_state = (fraction, message)
            # Called from the worker thread; the main thread only polls every 0.5 s.
            activity.note(message, log=False)

        return pool.submit(
            visual.build_timeline,
            Path(self.source.path),
            self.source.duration_s or 0.0,
            cache=self.workspace.visual,
            on_progress=on_progress,
            cancelled=self._is_cancelled,
        )

    def _await_visual(self, future) -> list[visual.VisualMoment]:
        """Wait for the timeline, reporting progress from this (the main) thread."""
        if future is None:
            return []
        while not future.done():
            fraction, message = self._visual_state
            # No message: the worker thread writes its own, more specific activity.
            self._emit(Stage.HIGHLIGHTS, 0.0)
            self._check_cancelled()
            time.sleep(0.5)
        try:
            moments = future.result()
        except Exception as exc:  # noqa: BLE001 - visuals are optional
            log.warning("Visual timeline failed (%s); continuing without it.", exc)
            moments = []
        log.info("Visual timeline: %d moment(s).", len(moments))
        return moments

    async def _stage_more_highlights(
        self,
        transcript: Transcript,
        silences: list[Silence],
        count: int,
        moments: list[visual.VisualMoment] | None = None,
    ) -> list[Clip]:
        stage = Stage.HIGHLIGHTS
        self._check_cancelled()

        existing = store.list_clips(self.job.id)
        provider = build_provider(self.job.provider or self.settings.active_provider, self.settings)
        config = detection_config(self.settings)
        config.brief = clean_brief(self.job.settings.get("brief"))
        config.campaign_note = campaign.prompt_section(self._rules())
        config.max_clips = count

        self._emit(stage, 0.0, f"Finding {count} more highlights with {activity.describe(provider)}")
        try:
            clips = await highlights.detect(
                transcript,
                provider,
                config,
                job_id=self.job.id,
                silences=silences,
                on_progress=self._stage_progress(stage),
                existing=existing,
                visual=[(m.t, m.desc, m.brand_visible) for m in moments or []],
            )
        except highlights.HighlightError as exc:
            log.info("No new clips for job %s: %s", self.job.id, exc)
            clips = []

        clips = clips[:count]
        self._finalize_metadata(clips)
        clips = store.append_clips(self.job.id, clips)
        # Stages report progress across the whole bar; give the cached early
        # stages their weight so the bar starts where a normal job would.
        self._finish_stage(stage)
        return clips

    # -- stages ------------------------------------------------------------

    def _stage_prepare(self) -> Path:
        stage = Stage.PREPARE
        self._check_cancelled()
        source_path = Path(self.source.path)

        if self.workspace.audio.exists() and self.workspace.audio.stat().st_size > 0:
            log.info("Reusing existing audio for job %s.", self.job.id)
            self._emit(stage, 0.0, "Using cached audio")
        else:
            self._emit(stage, 0.0, "Preparing audio (ffmpeg)")
            prepare.extract_audio(
                source_path,
                self.workspace.audio,
                duration_s=self.source.duration_s,
                on_progress=self._stage_progress(stage),
            )

        self._detect_letterbox(stage)
        self._finish_stage(stage)
        return self.workspace.audio

    def _detect_letterbox(self, stage: Stage) -> None:
        """Find baked-in black bars once per source (cached in the work dir)."""
        if not self.source.has_video:
            return
        width, height = self.source.width, self.source.height
        if not width or not height:
            info = ffmpeg.probe(Path(self.source.path))
            width, height = info.width, info.height
        if not width or not height:
            return
        self._emit(stage, 0.9, "Checking for black bars (letterbox)")
        self.active = letterbox.load_or_detect(
            Path(self.source.path), width, height, self.source.duration_s or 0.0,
            self.workspace.letterbox,
        )
        if self.active:
            ax, ay, aw, ah = self.active
            self.activity.set(f"Black bars found: using the {aw}x{ah} picture (of {width}x{height})")
        else:
            self.activity.set("No black bars found")

    def _stage_transcribe(self, audio: Path) -> Transcript:
        stage = Stage.TRANSCRIBE
        self._check_cancelled()

        if self.workspace.transcript.exists():
            log.info("Reusing existing transcript for job %s.", self.job.id)
            self._emit(stage, 0.0, "Using cached transcript")
            transcript = Transcript.load(self.workspace.transcript)
            self._finish_stage(stage)
            return transcript

        rules = self._rules()
        hint = vocabulary.whisper_prompt(rules.vocabulary) if rules else ""
        label = (
            "Transcribing with Groq Whisper"
            if self.settings.whisper.backend == "groq"
            else "Transcribing locally (Whisper)"
        )
        self._emit(stage, 0.0, label + (" (with name hints from the brief)" if hint else ""))
        transcript = transcribe.transcribe(
            audio,
            self.settings.whisper,
            duration_s=self.source.duration_s,
            on_progress=self._stage_progress(stage),
            cancelled=self._is_cancelled,
            prompt=hint,
        )

        if self.settings.whisper.diarization:
            from ..config import HF_TOKEN_KEY, get_secret

            self._emit(stage, 0.95, "Identifying speakers (pyannote)")
            transcribe.diarize(audio, transcript, hf_token=get_secret(HF_TOKEN_KEY, self.settings))

        transcript.save(self.workspace.transcript)
        store.upsert_transcript(
            TranscriptRow(
                job_id=self.job.id,
                json_path=str(self.workspace.transcript),
                language=transcript.language,
                model=transcript.model,
                has_diarization=transcript.has_diarization,
                word_count=len(transcript.words),
                source=transcript.source,
            )
        )

        self._finish_stage(stage)
        return transcript

    def _load_or_detect_silences(self, audio: Path) -> list[Silence]:
        if self.workspace.silences.exists():
            raw = json.loads(self.workspace.silences.read_text(encoding="utf-8"))
            return [Silence(**item) for item in raw]

        silences = prepare.detect_silences(audio)
        self.workspace.silences.write_text(
            json.dumps([{"start": s.start, "end": s.end} for s in silences]),
            encoding="utf-8",
        )
        return silences

    async def _stage_highlights(
        self,
        transcript: Transcript,
        silences: list[Silence],
        moments: list[visual.VisualMoment] | None = None,
    ) -> list[Clip]:
        stage = Stage.HIGHLIGHTS
        self._check_cancelled()

        existing = store.list_clips(self.job.id)
        if existing:
            log.info("Reusing %d existing clips for job %s.", len(existing), self.job.id)
            self._finish_stage(stage)
            return existing

        provider_name = self.job.provider or self.settings.active_provider
        provider = build_provider(provider_name, self.settings)
        config = detection_config(self.settings)
        config.brief = clean_brief(self.job.settings.get("brief"))
        config.campaign_note = campaign.prompt_section(self._rules())

        self._emit(stage, 0.0, f"Finding highlights with {activity.describe(provider)}")
        clips = await highlights.detect(
            transcript,
            provider,
            config,
            job_id=self.job.id,
            silences=silences,
            on_progress=self._stage_progress(stage),
            visual=[(m.t, m.desc, m.brand_visible) for m in moments or []],
        )

        self._finalize_metadata(clips)
        store.replace_clips(self.job.id, clips)
        self._finish_stage(stage)
        return clips

    def _stage_reframe(self, clips: list[Clip], transcript: Transcript) -> dict[str, CropPath]:
        stage = Stage.REFRAME
        self._check_cancelled()
        source_path = Path(self.source.path)

        crop_paths: dict[str, CropPath] = {}

        if not self.source.has_video:
            # Audio-only sources render as captions on a solid background, so
            # there is nothing to reframe.
            self._finish_stage(stage)
            return crop_paths

        aspect = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}[self.settings.export.ratio]
        config = ReframeConfig(
            aspect_w=aspect[0],
            aspect_h=aspect[1],
            sample_fps=self.settings.light.face_sample_fps,
            analysis_width=self.settings.light.analysis_width,
            active=self.active,
        )

        for index, clip in enumerate(clips):
            self._check_cancelled()
            self._emit(
                stage, index / len(clips),
                f"Framing clip {index + 1}/{len(clips)}: detecting faces",
            )
            cached = self.workspace.crop_path(clip.id)
            clip_started = time.monotonic()
            reuse = CropPath.load(cached) if cached.exists() else None
            if reuse is not None and reuse.active != self.active:
                reuse = None  # framed against a different picture area
            if reuse is not None:
                crop_paths[clip.id] = reuse
            else:
                crop_paths[clip.id] = build_crop_path(
                    source_path,
                    start_s=clip.start_s,
                    end_s=clip.end_s,
                    transcript=transcript,
                    config=config,
                    on_frame=lambda seen, total, i=index: activity.note(
                        f"Framing clip {i + 1}/{len(clips)}: detecting faces "
                        f"(frame {seen}/{total})",
                        log=False,
                    ),
                )
                crop_paths[clip.id].save(cached)

            log.info(
                "Reframe clip %d/%d in %.1fs (%d segments, %d fit).",
                index + 1, len(clips), time.monotonic() - clip_started,
                len(crop_paths[clip.id].segments),
                sum(1 for s in crop_paths[clip.id].segments if s.fit),
            )  # fmt: skip
            self._emit(
                stage, (index + 1) / len(clips),
                f"Framing clip {index + 1}/{len(clips)} done ({len(crop_paths[clip.id].segments)} segments)",
            )

        self._finish_stage(stage)
        return crop_paths

    async def _stage_captions(self, clips: list[Clip], transcript: Transcript) -> None:
        """Caption style check + one edit plan (zooms/emoji/sfx/hook) per clip.

        Caption files themselves are written during export, where the output
        dimensions are known.
        """
        stage = Stage.CAPTIONS
        self._check_cancelled()
        captions.get_style(self.settings.export.caption_style)

        template_name = str(self.job.settings.get("template") or "auto")
        provider = None
        try:
            provider = build_provider(self.job.provider or self.settings.active_provider, self.settings)
        except Exception as exc:  # noqa: BLE001 - plans fall back to heuristics
            log.warning("No LLM for edit plans (%s); using heuristic plans.", exc)

        semaphore = asyncio.Semaphore(4)
        done = 0

        async def plan_one(clip: Clip) -> None:
            nonlocal done
            cached = self.workspace.edit_plan(clip.id)
            if cached.exists():
                done += 1
                return
            started = time.monotonic()
            async with semaphore:
                template, words = self.pick_template(template_name, clip, transcript)
                template = self._template_with_profile(template)
                rel = [
                    editplan.Word(w.text, w.start - clip.start_s, w.end - clip.start_s)
                    for w in words
                ]
                plan = await editplan.plan_clip(
                    rel,
                    clip.end_s - clip.start_s,
                    editplan.TemplateLimits.of(template, brand_safe=self._brand_safe()),
                    provider=provider,
                    title=clip.title,
                    hook=clip.hook,
                    emphasis_times=self.emphasis_rel(clip, transcript),
                    template_name=template.name,
                    label=f"clip {clip.rank}/{len(clips)}",
                )
            await self._ensure_hook(clip, plan, words, provider, len(clips))
            plan.save(cached)
            done += 1
            self._emit(stage, done / len(clips), f"Edit plan ready for clip {done}/{len(clips)}")
            log.info(
                "Edit plan for clip %s (%s, template %s) in %.1fs: %d zooms, %d emoji, %d sfx.",
                clip.rank, plan.source, template.name, time.monotonic() - started,
                len(plan.zooms), len(plan.emoji), len(plan.sfx),
            )  # fmt: skip

        await asyncio.gather(*(plan_one(c) for c in clips))
        self._finish_stage(stage)

    async def _ensure_hook(self, clip: Clip, plan, words, provider, total: int) -> None:
        """Replace a generic hook with a clip-specific one (one LLM retry, else the title)."""
        rules = self._rules()
        hook, how = await hooks.ensure_specific_hook(
            plan.hook_text or clip.hook,
            provider=provider,
            title=clip.title,
            transcript=" ".join(w.text for w in words),
            scene=self._scene_text(clip),
            tone=rules.caption_style if rules else "",
        )
        if how != "kept":
            self.activity.set(
                f"Hook for clip {clip.rank}/{total} was generic: "
                + ("wrote a specific one" if how == "regenerated" else "using the clip title")
            )
        plan.hook_text = hook
        if hook and hook != clip.hook:
            clip.hook = hook
            store.update_clip(clip.id, hook=hook)

    @staticmethod
    def pick_template(name: str, clip: Clip, transcript: Transcript):
        """Resolve the job's template for one clip ("auto" asks ``auto_pick``)."""
        from ..templates import auto_pick, load_template

        words = highlights.clip_words(
            transcript, clip.start_s, clip.end_s, clip.start_word, clip.end_word
        )
        if not name or name == "auto":
            try:
                name = auto_pick(" ".join(w.text for w in words), clip.title)
            except Exception as exc:  # noqa: BLE001
                log.warning("auto_pick failed (%s); using the default template.", exc)
                name = "talk"
        return load_template(name), words

    @staticmethod
    def emphasis_rel(clip: Clip, transcript: Transcript) -> list[float]:
        return [
            round(transcript.words[i].start - clip.start_s, 3)
            for i in clip.emphasis
            if 0 <= i < len(transcript.words)
        ]

    def _stage_export(
        self,
        clips: list[Clip],
        transcript: Transcript,
        crop_paths: dict[str, CropPath],
    ) -> None:
        stage = Stage.EXPORT
        self._check_cancelled()

        style = captions.get_style(self.settings.export.caption_style)
        template_name = str(self.job.settings.get("template") or "auto")
        ratio = self.settings.export.ratio
        source_path = Path(self.source.path)
        destination_dir = paths.exports_dir() / self.job.id
        destination_dir.mkdir(parents=True, exist_ok=True)

        for index, clip in enumerate(clips):
            self._check_cancelled()
            # Announce the stage before the (slow) render starts: with no event
            # until the first progress line, the UI sat on "Captions" for minutes.
            self._emit(
                stage, index / len(clips),
                f"Rendering clip {index + 1}/{len(clips)} with ffmpeg: 0%",
            )

            crop_path = crop_paths.get(clip.id) or self._fallback_crop_path(clip, ratio)
            words = highlights.clip_words(
                transcript, clip.start_s, clip.end_s, clip.start_word, clip.end_word
            )
            destination = destination_dir / export.output_filename(
                clip.title or f"clip-{clip.rank}", ratio
            )

            plan = editplan.EditPlanData.load(self.workspace.edit_plan(clip.id))
            if plan is not None and plan.template:
                from ..templates import load_template

                template = load_template(plan.template)
            else:
                template, _ = self.pick_template(template_name, clip, transcript)
            template = self._template_with_profile(template)
            if plan is not None:
                plan = editplan.constrain(
                    plan,
                    editplan.TemplateLimits.of(template, brand_safe=self._brand_safe()),
                    clip.end_s - clip.start_s,
                )
            if "caption_style" in self._explicit() and not self._template_is_explicit():
                clip_style = style  # the job explicitly asked for this caption style
            elif template.caption_preset in captions.PRESETS:
                clip_style = captions.get_style(template.caption_preset)
            else:
                clip_style = style

            request = export.ExportRequest(
                template=template,
                edit_plan=plan,
                source=source_path,
                destination=destination,
                start_s=clip.start_s,
                end_s=clip.end_s,
                crop_path=crop_path,
                words=words,
                style=clip_style,
                ratio=ratio,
                has_audio=self.source.has_audio,
                language=transcript.language,
                emphasis_times=[
                    transcript.words[i].start
                    for i in clip.emphasis
                    if 0 <= i < len(transcript.words)
                ],
                burn_captions=self.settings.edit.burn_captions,
                music_file=export.find_music(MUSIC_DIR, clip.id)
                if self.settings.edit.background_music
                else None,
            )

            def clip_progress(fraction: float, i: int = index) -> None:
                self._emit(
                    stage,
                    (i + fraction) / len(clips),
                    f"Rendering clip {i + 1}/{len(clips)} with ffmpeg: {int(fraction * 100)}%",
                    transient=True,
                )

            export.export_clip(
                request,
                work_dir=self.workspace.captions_dir,
                settings=self.settings.export,
                edit=self.settings.edit,
                on_progress=clip_progress,
                cancelled=self._is_cancelled,
            )

            self._export_ctx[clip.id] = {
                "request": request, "plan": plan, "destination": destination, "index": index,
            }
            self._write_post_text(clip, destination)
            store.create_export(
                Export(
                    id=new_id(),
                    clip_id=clip.id,
                    path=str(destination),
                    ratio=ratio,
                    style=style.key,
                    size_bytes=destination.stat().st_size,
                )
            )
            store.update_clip(clip.id, status="exported")

        self._finish_stage(stage)

    def _write_post_text(self, clip: Clip, destination: Path, qa_notes: list[str] | None = None) -> None:
        """Write the .txt, after checking it carries every campaign requirement."""
        rules = self._rules()
        try:
            if rules is not None:
                problems = campaign.enforce_metadata(rules, clip, max_topical=self._max_topical())
                if problems:
                    log.warning("Clip %s metadata was missing: %s (fixed)", clip.id, "; ".join(problems))
                store.update_clip(clip.id, description=clip.description, hashtags=clip.hashtags)
            else:
                clip.description = campaign.with_hashtags(clip.description, clip.hashtags)
                store.update_clip(clip.id, description=clip.description)
            write_post_text(clip, destination, qa_notes=qa_notes)
        except OSError as exc:
            log.warning("Could not write post text for clip %s: %s", clip.id, exc)

    # -- self-QA -------------------------------------------------------------

    async def _stage_qa(self, clips: list[Clip], transcript: Transcript) -> None:
        """Check each exported clip's frames; one automatic fix + re-check. Never fails the job."""
        stage = Stage.QA
        self._check_cancelled()
        provider = None
        try:
            provider = build_provider(self.job.provider or self.settings.active_provider, self.settings)
        except Exception as exc:  # noqa: BLE001
            log.warning("No LLM for QA hook fixes (%s).", exc)
        total = len(clips)
        for index, clip in enumerate(clips):
            self._check_cancelled()
            ctx = self._export_ctx.get(clip.id)
            if ctx is None:
                continue
            label = f"QA clip {index + 1}/{total}"
            self._emit(stage, index / max(1, total), f"{label}: checking frames")
            try:
                await self._qa_clip(clip, ctx, transcript, provider, label, index, total)
            except JobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - QA never fails the job
                log.warning("QA failed for clip %s: %s", clip.id, exc)
                self.activity.set(f"{label}: skipped ({str(exc)[:80]})")
        self._finish_stage(stage)

    async def _qa_check(self, clip: Clip, ctx: dict, label: str, stage_frac: float) -> qa.QAResult:
        stage = Stage.QA
        request: export.ExportRequest = ctx["request"]
        result = qa.QAResult()
        ass = self.workspace.captions_dir / request.destination.stem / "captions.ass"
        self._emit(stage, stage_frac, f"{label}: checking frames (black bands, captions vs faces)")
        frames = qa.numeric_checks(request.destination, ass, result)
        if frames and self.settings.visual_enabled:
            self._emit(stage, stage_frac, f"{label}: vision review of the frames")
            rules = self._rules()
            plan = ctx.get("plan")
            issues = await qa.vision_review(
                frames,
                hook=(plan.hook_text if plan is not None and plan.hook_text else clip.hook),
                title=clip.title,
                caption=clip.description,
                forbidden=rules.forbidden if rules else [],
            )
            if issues is not None:
                result.vision_ran = True
                for issue in issues:
                    result.add(issue["code"], issue["message"])
        return result

    async def _qa_clip(self, clip, ctx, transcript, provider, label, index, total) -> None:
        stage = Stage.QA
        frac = index / max(1, total)
        result = await self._qa_check(clip, ctx, label, frac)
        if not result.ok:
            fixes = await self._qa_fix(clip, ctx, transcript, provider, result)
            if fixes:
                self._emit(stage, frac, f"{label}: fixing ({', '.join(fixes)}), re-rendering")
                request = ctx["request"]
                export.export_clip(
                    request,
                    work_dir=self.workspace.captions_dir,
                    settings=self.settings.export,
                    edit=self.settings.edit,
                    cancelled=self._is_cancelled,
                )
                self._emit(stage, frac, f"{label}: re-checking after the fix")
                result = await self._qa_check(clip, ctx, label, frac)
                result.fixes = fixes
        notes = [i["message"] for i in result.issues]
        exports = store.list_exports(clip.id)
        qstore.save_qa(clip.id, self.job.id, exports[0].id if exports else None, result.to_dict())
        self._write_post_text(clip, ctx["destination"], qa_notes=notes)
        self.activity.set(
            f"{label}: " + ("passed" if result.ok else f"kept with {len(notes)} note(s)")
            + (f" (fixed: {', '.join(result.fixes)})" if result.fixes and result.ok else "")
        )

    async def _qa_fix(self, clip, ctx, transcript, provider, result: qa.QAResult) -> list[str]:
        """Apply at most one fix per issue kind to ``ctx["request"]``; returns what was done."""
        from .reframe import _fit_segment
        from .reframe.croppath import Strategy

        request: export.ExportRequest = ctx["request"]
        codes = result.codes()
        fixes: list[str] = []
        crop = request.crop_path

        if qa.BLACK_BANDS in codes:
            base = crop.active or (0, 0, crop.source_width, crop.source_height)
            frac = max(result.metrics.get("band_rows", 0.0), result.metrics.get("band_cols", 0.0))
            extra = max(8, min(int(0.5 * frac * min(base[2], base[3])), min(base[2], base[3]) // 4))
            stricter = letterbox.stricter(base, 0, 0, extra=extra)
            if self.source.has_video:
                aspect = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}[self.settings.export.ratio]
                config = ReframeConfig(
                    aspect_w=aspect[0], aspect_h=aspect[1],
                    sample_fps=self.settings.light.face_sample_fps,
                    analysis_width=self.settings.light.analysis_width,
                    active=stricter,
                )  # fmt: skip
                crop = build_crop_path(
                    Path(self.source.path), start_s=clip.start_s, end_s=clip.end_s,
                    transcript=transcript, config=config,
                )  # fmt: skip
                crop.save(self.workspace.crop_path(clip.id))
                fixes.append("cropped black bands tighter")
        if qa.SUBJECT_CUT in codes:
            sw, sh = crop.source_width, crop.source_height
            fit = [
                replace(
                    _fit_segment(seg.start_s, seg.end_s, sw, sh, Strategy.GENERAL),
                    layout="",
                )
                for seg in crop.segments
            ]
            crop = replace(crop, segments=fit)
            fixes.append("showing the whole frame")
        if qa.CAPTION_ON_FACE in codes:
            request = replace(request, caption_face_margin=request.caption_face_margin + 0.8)
            fixes.append("moved captions off faces")
        if qa.HOOK_MISMATCH in codes and request.edit_plan is not None:
            plan = request.edit_plan
            words = highlights.clip_words(
                transcript, clip.start_s, clip.end_s, clip.start_word, clip.end_word
            )
            reason = "; ".join(i["message"] for i in result.issues if i["code"] == qa.HOOK_MISMATCH)
            better = await hooks.specific_hook(
                provider,
                title=clip.title,
                transcript=" ".join(w.text for w in words),
                scene=self._scene_text(clip),
                rejected=f"{plan.hook_text} (QA: {reason})",
            )
            if better and better != plan.hook_text:
                plan = replace(plan, hook_text=better)
                plan.save(self.workspace.edit_plan(clip.id))
                store.update_clip(clip.id, hook=better)
                clip.hook = better
                ctx["plan"] = plan
                request = replace(request, edit_plan=plan)
                fixes.append("rewrote the hook")
        if fixes:
            ctx["request"] = replace(request, crop_path=crop)
        return fixes

    def _fallback_crop_path(self, clip: Clip, ratio: str) -> CropPath:
        """Centre crop for sources with no reframe data (audio-only, or a failure)."""
        from .reframe.croppath import centre_crop

        width, height = export.ratio_dimensions(ratio)
        if self.active:
            src_w, src_h = self.active[2], self.active[3]
        else:
            info = ffmpeg.probe(Path(self.source.path))
            src_w, src_h = info.width or width, info.height or height
        path = centre_crop(
            src_w,
            src_h,
            clip.end_s - clip.start_s,
            aspect_w=9 if ratio == "9:16" else (1 if ratio == "1:1" else 16),
            aspect_h=16 if ratio == "9:16" else (1 if ratio == "1:1" else 9),
        )
        path.active = self.active
        return path


def write_post_text(clip: Clip, video: Path, qa_notes: list[str] | None = None) -> Path:
    """Write ``<video>.txt``: title, caption, hashtags: what the user pastes when posting.

    Self-QA problems that could not be fixed follow as ``QA notes:`` lines.
    """
    nl = chr(10)
    lines = [
        (clip.title or "").strip(), "", (clip.description or "").strip(), "",
        " ".join(clip.hashtags), "",
    ]  # fmt: skip
    if qa_notes:
        lines += ["QA notes:", *[f"- {n}" for n in qa_notes], ""]
    body = nl.join(lines)
    out = video.with_suffix(".txt")
    out.write_text(body, encoding="utf-8")
    return out


async def run_job(
    job_id: str,
    *,
    settings: Settings | None = None,
    on_progress: ProgressHandler | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> list[Clip]:
    """Load a job and run it to completion."""
    job = store.get_job(job_id)
    if job is None:
        raise PipelineError(f"Job {job_id} not found.", stage=Stage.PREPARE)

    source = store.get_source(job.source_id)
    if source is None:
        raise PipelineError(f"Source {job.source_id} not found.", stage=Stage.PREPARE)

    runner = PipelineRunner(
        job, source, settings=settings, on_progress=on_progress, is_cancelled=is_cancelled
    )
    # The pipeline is mostly blocking work (ffmpeg, Whisper, MediaPipe) with one
    # async stage. Running it in a worker thread keeps the web server's event
    # loop responsive while a job is going.
    return await asyncio.get_running_loop().run_in_executor(None, _run_sync, runner)


def _run_sync(runner: PipelineRunner) -> list[Clip]:
    return asyncio.run(runner.run())
