"""Quality gate endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ..db import store as dbstore
from ..quality import checks
from ..quality import store as qstore

router = APIRouter(prefix="/api", tags=["quality"])


@router.post("/clips/{clip_id}/quality")
async def run_quality(clip_id: str) -> dict:
    try:
        result = await asyncio.to_thread(checks.check_clip_by_id, clip_id)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Clip not found.")
    return result


@router.get("/clips/{clip_id}/quality")
async def get_quality(clip_id: str) -> dict:
    result = await asyncio.to_thread(qstore.get, clip_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No quality report yet.")
    return result


@router.get("/jobs/{job_id}/quality")
async def job_quality(job_id: str) -> list[dict]:
    if await asyncio.to_thread(dbstore.get_job, job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return await asyncio.to_thread(qstore.list_for_job, job_id)
