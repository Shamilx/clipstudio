"""Job lifecycle and the SSE progress stream."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from ..config import load as load_settings
from ..db import store
from ..db.models import Job, new_id
from ..jobs.events import broker
from ..jobs.queue import queue
from .schemas import JobCreateIn, JobOut, JobSettingsIn, MoreClipsIn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _apply_overrides(settings, overrides: JobSettingsIn):
    """Layer per-job overrides on top of saved settings."""
    merged = settings.model_copy(deep=True)

    if overrides.provider:
        merged.active_provider = overrides.provider
    if overrides.whisper_model:
        merged.whisper.model = overrides.whisper_model
    if overrides.language is not None:
        merged.whisper.language = overrides.language
    if overrides.diarization is not None:
        merged.whisper.diarization = overrides.diarization
    if overrides.min_duration_s is not None:
        merged.clips.min_duration_s = overrides.min_duration_s
    if overrides.max_duration_s is not None:
        merged.clips.max_duration_s = overrides.max_duration_s
    if overrides.max_clips is not None:
        merged.clips.max_clips = overrides.max_clips
    if overrides.caption_style:
        merged.export.caption_style = overrides.caption_style
    if overrides.ratio:
        merged.export.ratio = overrides.ratio
    for name in (
        "trim_silence", "remove_fillers", "punch_zoom", "keyword_emphasis",
        "progress_bar", "normalize_audio", "background_music", "burn_captions", "teaser",
    ):  # fmt: skip
        value = getattr(overrides, name)
        if value is not None:
            setattr(merged.edit, name, value)

    return merged


@router.post("", response_model=JobOut, status_code=201)
async def create_job(payload: JobCreateIn) -> JobOut:
    source = await asyncio.to_thread(store.get_source, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")

    settings = _apply_overrides(load_settings(), payload.settings)
    from ..providers import PROVIDERS

    if settings.active_provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {settings.active_provider}")
    from ..pipeline import captions as captions_module

    try:
        captions_module.get_style(settings.export.caption_style)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if settings.clips.min_duration_s >= settings.clips.max_duration_s:
        raise HTTPException(
            status_code=400, detail="Minimum clip length must be below the maximum."
        )

    from .. import templates as tpl

    template = (payload.template or "auto").strip().lower()
    if template != "auto" and not any(t.name == template for t in tpl.list_templates()):
        raise HTTPException(status_code=400, detail=f"Unknown template: {template}")
    job_settings = settings.model_dump(mode="json")
    job_settings["template"] = template
    from .. import profiles

    profile = (payload.profile or "").strip().lower()
    if profile and profile != "auto":
        if not profiles.is_known(profile):
            raise HTTPException(status_code=400, detail=f"Unknown profile: {profile}")
        job_settings["profile"] = profile
    # Settings the user set explicitly for this job: the content profile must not override them.
    explicit = [
        name for name in (
            "trim_silence", "remove_fillers", "punch_zoom", "keyword_emphasis",
            "progress_bar", "normalize_audio", "background_music", "burn_captions", "teaser", "caption_style",
        )
        if getattr(payload.settings, name) not in (None, "")
    ]  # fmt: skip
    if explicit:
        job_settings["explicit"] = explicit
    from ..providers.base import clean_brief

    brief = clean_brief(payload.brief)
    if brief:
        job_settings["brief"] = brief

    job = Job(
        id=new_id(),
        source_id=source.id,
        provider=settings.active_provider,
        settings=job_settings,
    )
    await asyncio.to_thread(store.create_job, job)
    queue.notify()

    return JobOut.of(job, source)


@router.get("", response_model=list[JobOut])
async def list_jobs(limit: int = 25) -> list[JobOut]:
    jobs = await asyncio.to_thread(store.list_jobs, limit)
    out: list[JobOut] = []
    for job in jobs:
        source = await asyncio.to_thread(store.get_source, job.source_id)
        out.append(JobOut.of(job, source))
    return out


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: str) -> JobOut:
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    source = await asyncio.to_thread(store.get_source, job.source_id)
    return JobOut.of(job, source)


@router.get("/{job_id}/events")
async def job_events(job_id: str, request: Request) -> EventSourceResponse:
    """Stream progress for a job as Server-Sent Events."""
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def stream():
        # Send the current state immediately: a client connecting mid-job (or
        # reconnecting) must not wait for the next progress tick to render.
        async def snapshot_of(jid: str):
            cur = await asyncio.to_thread(store.get_job, jid)
            if cur is None:
                return None, None
            # Include the source: the client replaces its job with the
            # snapshot, so omitting it blanks the title and duration.
            src = await asyncio.to_thread(store.get_source, cur.source_id)
            return cur, {
                "event": "snapshot",
                "data": JobOut.of(cur, src).model_dump_json(),
            }

        current, snap = await snapshot_of(job_id)
        if current is not None:
            yield snap
            if current.status in ("done", "failed", "cancelled"):
                return

        async for event in broker.subscribe(job_id):
            if await request.is_disconnected():
                break
            if event.type == "heartbeat":
                # A terminal event published between the snapshot above and the
                # subscription taking effect is never delivered; re-check so
                # the client is not left on a dead stream forever.
                current, snap = await snapshot_of(job_id)
                if current is not None and current.status in ("done", "failed", "cancelled"):
                    yield snap
                    return
            yield {"event": event.type, "data": event.to_sse().split("data: ", 1)[-1].strip()}

    return EventSourceResponse(stream())


@router.post("/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: str) -> JobOut:
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    if not queue.cancel(job_id):
        raise HTTPException(
            status_code=409, detail=f"Job is already {job.status}; nothing to cancel."
        )

    updated = await asyncio.to_thread(store.get_job, job_id)
    return JobOut.of(updated or job)


@router.post("/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str) -> JobOut:
    """Requeue a failed or cancelled job.

    Completed stages left their artifacts in ``work/{job_id}/``, so the retry
    resumes at the stage that failed rather than starting over.
    """
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Only failed or cancelled jobs can be retried (this one is {job.status}).",
        )

    # Cancelling a *queued* job leaves its id in the queue's cancel set (only a
    # finished run clears it), so without this the retry would be cancelled
    # again the moment the worker starts it.
    queue._cancel_requested.discard(job_id)
    await asyncio.to_thread(
        store.update_job,
        job_id,
        status="queued",
        error=None,
        progress=0.0,
        finished_at=None,
    )
    queue.notify()

    updated = await asyncio.to_thread(store.get_job, job_id)
    return JobOut.of(updated or job)


@router.post("/{job_id}/more", response_model=JobOut)
async def generate_more(job_id: str, payload: MoreClipsIn | None = None) -> JobOut:
    """Queue a "generate more clips" run on a finished job.

    Reuses the job's cached audio and transcript; only new moments are
    selected, reframed, edited and exported, then appended to the job.
    """
    payload = payload or MoreClipsIn()
    job = await asyncio.to_thread(store.get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Only finished jobs can generate more clips (this one is {job.status}).",
        )

    settings = dict(job.settings)
    settings["more"] = {"count": payload.count}
    await asyncio.to_thread(store.update_job_settings, job_id, settings)
    queue._cancel_requested.discard(job_id)
    await asyncio.to_thread(
        store.update_job,
        job_id,
        status="queued",
        error=None,
        progress=0.0,
        finished_at=None,
    )
    queue.notify()

    updated = await asyncio.to_thread(store.get_job, job_id)
    source = await asyncio.to_thread(store.get_source, job.source_id)
    return JobOut.of(updated or job, source)


@router.get("/{job_id}/clips")
async def job_clips(job_id: str):
    from .clips import list_clips_for_job

    return await list_clips_for_job(job_id)
