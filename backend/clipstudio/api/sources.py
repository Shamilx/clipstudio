"""Source ingestion endpoints."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..config import load as load_settings
from ..db import store
from ..pipeline import ingest
from .schemas import SourceOut, YouTubeIngestIn

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources", tags=["sources"])

#: Uploads stream to disk in chunks rather than being buffered whole — source
#: videos routinely exceed available RAM.
UPLOAD_CHUNK = 4 * 1024 * 1024


@router.get("", response_model=list[SourceOut])
async def list_sources(limit: int = 50) -> list[SourceOut]:
    sources = await asyncio.to_thread(store.list_sources, limit)
    return [SourceOut.of(s) for s in sources]


#: Download progress by client-chosen id (in memory; entries are short-lived).
_progress: dict[str, dict] = {}


@router.get("/progress/{progress_id}")
async def ingest_progress(progress_id: str) -> dict:
    return _progress.get(progress_id, {"fraction": None, "message": ""})


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: str) -> SourceOut:
    source = await asyncio.to_thread(store.get_source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")
    return SourceOut.of(source)


@router.post("/youtube", response_model=SourceOut, status_code=201)
async def ingest_youtube(payload: YouTubeIngestIn) -> SourceOut:
    """Download a YouTube video and register it as a source."""
    if not ingest.is_youtube_url(payload.url):
        raise HTTPException(status_code=400, detail="That is not a YouTube URL.")

    settings = load_settings().ingest
    if payload.cookies_from_browser is not None:
        settings = settings.model_copy(
            update={"cookies_from_browser": payload.cookies_from_browser}
        )

    pid = payload.progress_id
    if pid and len(_progress) > 50:
        _progress.clear()

    def on_progress(fraction: float) -> None:
        if pid:
            _progress.setdefault(pid, {})["fraction"] = fraction

    def on_status(message: str) -> None:
        if pid:
            _progress.setdefault(pid, {})["message"] = message

    try:
        source = await asyncio.to_thread(
            ingest.ingest_youtube,
            payload.url,
            settings,
            on_progress=on_progress,
            on_status=on_status,
        )
    except ingest.IngestError as exc:
        # 422 rather than 500: the request was well-formed, but the content
        # can't be fetched, and the hint tells the user what to change.
        raise HTTPException(
            status_code=422, detail={"message": str(exc), "hint": exc.hint}
        ) from exc
    finally:
        # Also on failure: a stale entry would show a previous attempt's
        # progress if the client reuses the id for a retry.
        if pid:
            _progress.pop(pid, None)

    await asyncio.to_thread(store.create_source, source)
    return SourceOut.of(source)


@router.post("/upload", response_model=SourceOut, status_code=201)
async def upload_source(file: UploadFile = File(...)) -> SourceOut:
    """Accept a media upload and register it as a source."""
    filename = Path(file.filename or "upload")
    suffix = filename.suffix.lower()
    if suffix not in ingest.ACCEPTED_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail={
                "message": f"{suffix or 'This file'} is not a supported format.",
                "hint": f"Accepted: {', '.join(sorted(ingest.ACCEPTED_SUFFIXES))}",
            },
        )

    # Stage to a temp file first so a failed or abandoned upload never leaves a
    # half-written file in the media store.
    upload_error: Exception | None = None
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as staging:
        staging_path = Path(staging.name)
        try:
            while chunk := await file.read(UPLOAD_CHUNK):
                staging.write(chunk)
        except Exception as exc:
            upload_error = exc
    if upload_error is not None:
        # Unlink after the handle is closed: Windows refuses to delete an open file.
        staging_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Upload failed.") from upload_error

    try:
        source = await asyncio.to_thread(
            ingest.ingest_file, staging_path, move=True, title=filename.stem
        )
    except ingest.IngestError as exc:
        staging_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422, detail={"message": str(exc), "hint": exc.hint}
        ) from exc
    finally:
        shutil.rmtree(staging_path.parent / staging_path.name, ignore_errors=True)

    await asyncio.to_thread(store.create_source, source)
    return SourceOut.of(source)
