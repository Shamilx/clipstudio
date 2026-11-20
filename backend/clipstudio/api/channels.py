"""Channel watch endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import channels as ch

router = APIRouter(prefix="/api/channels", tags=["channels"])


class ChannelIn(BaseModel):
    url: str
    template: str = "auto"
    interval_min: int = ch.DEFAULT_INTERVAL_MIN
    min_duration_s: float | None = None
    max_duration_s: float | None = None
    include_keywords: str = ""
    exclude_keywords: str = ""


class ChannelPatch(BaseModel):
    template: str | None = None
    interval_min: int | None = None
    min_duration_s: float | None = None
    max_duration_s: float | None = None
    include_keywords: str | None = None
    exclude_keywords: str | None = None
    enabled: bool | None = None


@router.get("")
async def list_channels() -> list[dict]:
    return await asyncio.to_thread(ch.list_channels)


@router.post("", status_code=201)
async def add_channel(payload: ChannelIn) -> dict:
    try:
        return await asyncio.to_thread(ch.add_channel, **payload.model_dump())
    except ch.ChannelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{channel_id}")
async def patch_channel(channel_id: str, payload: ChannelPatch) -> dict:
    updated = await asyncio.to_thread(
        ch.update_channel, channel_id, **payload.model_dump(exclude_unset=True)
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Channel not found.")
    return updated


@router.delete("/{channel_id}", status_code=204)
async def delete_channel(channel_id: str) -> None:
    if not await asyncio.to_thread(ch.delete_channel, channel_id):
        raise HTTPException(status_code=404, detail="Channel not found.")


@router.post("/{channel_id}/check")
async def check_now(channel_id: str) -> dict:
    if await asyncio.to_thread(ch.get_channel, channel_id) is None:
        raise HTTPException(status_code=404, detail="Channel not found.")
    fresh = await asyncio.to_thread(ch.check_channel, channel_id)
    return {
        "enqueued": [{"id": c.id, "title": c.title, "url": c.url} for c in fresh],
        "channel": await asyncio.to_thread(ch.get_channel, channel_id),
    }
