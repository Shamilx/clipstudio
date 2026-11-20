"""Bulk intake queue endpoints."""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import templates as tpl
from ..jobs import batch as bq
from ..pipeline import ingest

router = APIRouter(prefix="/api/queue", tags=["queue"])


class QueueAddIn(BaseModel):
    urls: list[str] = []
    text: str = ""  # alternative: raw textarea contents, one link per line
    template: str = "auto"


def _out() -> dict:
    items = bq.list_items()
    return {
        "paused": bq.is_paused(),
        "current": next((i.id for i in items if i.status == "running"), None),
        "items": [i.to_dict() for i in items],
    }


@router.get("")
async def get_queue() -> dict:
    return await asyncio.to_thread(_out)


@router.post("", status_code=201)
async def add(payload: QueueAddIn) -> dict:
    raw = [*payload.urls, *re.split(r"[\s,]+", payload.text)]
    urls = [u.strip() for u in raw if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="Paste at least one YouTube link.")
    bad = [u for u in urls if not ingest.is_youtube_url(u)]
    if bad:
        raise HTTPException(status_code=400, detail=f"Not a YouTube URL: {bad[0]}")
    template = (payload.template or "auto").strip().lower()
    if template != "auto" and not any(t.name == template for t in tpl.list_templates()):
        raise HTTPException(status_code=400, detail=f"Unknown template: {template}")
    added = await asyncio.to_thread(bq.add_items, urls, template)
    out = await asyncio.to_thread(_out)
    out["added"] = len(added)
    out["skipped"] = len(set(urls)) - len(added)
    return out


@router.post("/pause")
async def pause() -> dict:
    await asyncio.to_thread(bq.set_paused, True)
    return await asyncio.to_thread(_out)


@router.post("/resume")
async def resume() -> dict:
    await asyncio.to_thread(bq.set_paused, False)
    return await asyncio.to_thread(_out)


@router.post("/clear-finished")
async def clear_finished() -> dict:
    await asyncio.to_thread(bq.clear_finished)
    return await asyncio.to_thread(_out)


@router.delete("/{item_id}")
async def remove(item_id: str) -> dict:
    if not await bq.batch.remove(item_id):
        raise HTTPException(status_code=404, detail="Queue item not found.")
    return await asyncio.to_thread(_out)


class MoveIn(BaseModel):
    direction: str


@router.post("/{item_id}/move")
async def move(item_id: str, payload: MoveIn) -> dict:
    if payload.direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="direction must be up or down.")
    await asyncio.to_thread(bq.move_item, item_id, payload.direction)
    return await asyncio.to_thread(_out)


@router.post("/{item_id}/retry")
async def retry(item_id: str) -> dict:
    item = await asyncio.to_thread(bq.get_item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Queue item not found.")
    if item.status not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail="Only failed or cancelled items can retry.")
    await asyncio.to_thread(
        bq.update_item,
        item_id,
        status="pending",
        error=None,
        progress=0,
        job_id=None,
        source_id=None,
    )
    return await asyncio.to_thread(_out)
