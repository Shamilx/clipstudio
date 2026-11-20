"""Export packs: MP4 + title/description/thumbnail/meta in one zip."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import templates as tpl
from ..db import store
from ..templates.rules import apply_to_metadata

router = APIRouter(prefix="/api", tags=["pack"])


def _slug(text: str, fallback: str = "clip") -> str:
    s = re.sub(r"[^\w\-]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-")
    return s[:50] or fallback


def make_thumbnail(video: Path, out: Path, at_s: float = 1.0) -> bool:
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    cmd = [
        exe,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{at_s}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(out),
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(cmd, check=True, timeout=60, creationflags=flags)
    except Exception:
        return False
    return out.is_file() and out.stat().st_size > 0


def _template_for(job) -> tpl.Template | None:
    name = str((job.settings or {}).get("template") or "auto")
    if name == "auto":
        return None
    return tpl.load_template(name)


def add_clip_to_zip(zf: zipfile.ZipFile, prefix: str, clip, export, template, tmp: Path) -> None:
    video = Path(export.path)
    title, description, tags = apply_to_metadata(
        template, clip.title, clip.description, clip.hashtags
    )
    body = (description + "\n\n" + " ".join(tags)).strip() + "\n"
    zf.write(video, f"{prefix}{_slug(clip.title, 'clip')}.mp4", compress_type=zipfile.ZIP_STORED)
    zf.writestr(f"{prefix}title.txt", title + "\n")
    zf.writestr(f"{prefix}description.txt", body)
    thumb = tmp / f"{clip.id}.jpg"
    # Thumbnail at the hook, ~1 s in.
    if make_thumbnail(video, thumb, 1.0):
        zf.write(thumb, f"{prefix}thumbnail.jpg")
    meta = {
        "clip_id": clip.id,
        "job_id": clip.job_id,
        "title": title,
        "description": description,
        "hashtags": tags,
        "hook": clip.hook,
        "score": clip.score,
        "start_s": clip.start_s,
        "end_s": clip.end_s,
        "duration_s": round(clip.duration_s, 2),
        "ratio": export.ratio,
        "style": export.style,
        "template": template.name if template else None,
        "credit_text": template.credit_text if template else "",
        "video_file": video.name,
    }
    zf.writestr(f"{prefix}meta.json", json.dumps(meta, indent=2, ensure_ascii=False))


def _latest_export(clip_id: str):
    for e in store.list_exports(clip_id):  # newest first
        if Path(e.path).is_file():
            return e
    return None


def build_clip_pack(clip_id: str, dest: Path) -> str:
    clip = store.get_clip(clip_id)
    if clip is None:
        raise LookupError("Clip not found.")
    export = _latest_export(clip_id)
    if export is None:
        raise FileNotFoundError("This clip has no exported video yet.")
    job = store.get_job(clip.job_id)
    template = _template_for(job) if job else None
    with tempfile.TemporaryDirectory() as t, zipfile.ZipFile(dest, "w") as zf:
        add_clip_to_zip(zf, "", clip, export, template, Path(t))
    return f"{_slug(clip.title, 'clip')}-pack.zip"


def build_job_pack(job_id: str, dest: Path) -> str:
    job = store.get_job(job_id)
    if job is None:
        raise LookupError("Job not found.")
    template = _template_for(job)
    n = 0
    with tempfile.TemporaryDirectory() as t, zipfile.ZipFile(dest, "w") as zf:
        for clip in store.list_clips(job_id):
            export = _latest_export(clip.id)
            if export is None:
                continue
            n += 1
            add_clip_to_zip(
                zf, f"{n:02d}-{_slug(clip.title, 'clip')}/", clip, export, template, Path(t)
            )
    if n == 0:
        raise FileNotFoundError("This job has no exported clips yet.")
    return f"job-{job_id}-pack.zip"


async def _respond(builder, ident: str) -> FileResponse:
    fd, name = tempfile.mkstemp(suffix=".zip", prefix="clipstudio-pack-")
    os.close(fd)
    dest = Path(name)
    try:
        filename = await asyncio.to_thread(builder, ident, dest)
    except LookupError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        dest,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: dest.unlink(missing_ok=True)),
    )


@router.get("/clips/{clip_id}/pack")
async def clip_pack(clip_id: str) -> FileResponse:
    return await _respond(build_clip_pack, clip_id)


@router.get("/jobs/{job_id}/pack")
async def job_pack(job_id: str) -> FileResponse:
    return await _respond(build_job_pack, job_id)
