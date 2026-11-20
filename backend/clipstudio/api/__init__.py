"""REST API routers."""

from __future__ import annotations

from fastapi import APIRouter

from . import channels, clips, jobs, pack, quality, queue, settings, sources, templates

__all__ = ["api_router"]

api_router = APIRouter()
api_router.include_router(sources.router)
api_router.include_router(jobs.router)
api_router.include_router(clips.router)
api_router.include_router(quality.router)
api_router.include_router(settings.router)
api_router.include_router(templates.router)
api_router.include_router(queue.router)
api_router.include_router(channels.router)
api_router.include_router(pack.router)
