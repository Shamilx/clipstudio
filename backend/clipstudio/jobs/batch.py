"""Bulk intake queue: many YouTube links, processed strictly one at a time.

Each item moves ``pending -> downloading -> ready -> running -> done`` (or
``failed`` / ``cancelled``). The downloader may fetch item N+1 while item N
renders (downloads are light), but a job is only created for the next item once
the previous one has finished, so there is never more than one render. Jobs
themselves still run through :class:`clipstudio.jobs.queue.JobQueue`, which is
single-worker as well.

State lives in SQLite (tables created here with ``IF NOT EXISTS``) so the queue
survives restarts and resumes where it was.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from ..db import connection
from ..db.models import Job, Source, new_id, utcnow

log = logging.getLogger(__name__)

POLL_S = 2.0
FINISHED = ("done", "failed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_items (
    id         TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    template   TEXT NOT NULL DEFAULT 'auto',
    status     TEXT NOT NULL DEFAULT 'pending',
    position   REAL NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    source_id  TEXT,
    job_id     TEXT,
    progress   REAL NOT NULL DEFAULT 0,
    stage      TEXT NOT NULL DEFAULT '',
    error      TEXT,
    origin     TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass
class Item:
    id: str
    url: str
    template: str
    status: str
    position: float
    title: str
    source_id: str | None
    job_id: str | None
    progress: float
    stage: str
    error: str | None
    origin: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure() -> None:
    with connection() as conn:
        conn.executescript(_SCHEMA)


def _item(row) -> Item:
    return Item(**{k: row[k] for k in Item.__dataclass_fields__})


# -- persistence (sync) ------------------------------------------------------


def list_items() -> list[Item]:
    _ensure()
    with connection() as conn:
        rows = conn.execute("SELECT * FROM queue_items ORDER BY position, created_at").fetchall()
    return [_item(r) for r in rows]


def get_item(item_id: str) -> Item | None:
    _ensure()
    with connection() as conn:
        row = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
    return _item(row) if row else None


def add_items(urls: list[str], template: str = "auto", origin: str = "manual") -> list[Item]:
    """Append links to the queue, skipping ones already waiting or in flight."""
    _ensure()
    out: list[Item] = []
    with connection() as conn:
        active = {
            r["url"]
            for r in conn.execute(
                "SELECT url FROM queue_items WHERE status NOT IN ('done','failed','cancelled')"
            )
        }
        top = conn.execute("SELECT COALESCE(MAX(position), 0) FROM queue_items").fetchone()[0]
        for url in urls:
            url = url.strip()
            if not url or url in active:
                continue
            active.add(url)
            top += 1
            now = utcnow()
            item_id = new_id()
            conn.execute(
                "INSERT INTO queue_items (id, url, template, position, origin, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (item_id, url, template, top, origin, now, now),
            )
            row = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
            out.append(_item(row))
    return out


def update_item(item_id: str, **fields: Any) -> None:
    if not fields:
        return
    _ensure()
    fields["updated_at"] = utcnow()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connection() as conn:
        conn.execute(f"UPDATE queue_items SET {cols} WHERE id = ?", (*fields.values(), item_id))


def delete_item(item_id: str) -> None:
    _ensure()
    with connection() as conn:
        conn.execute("DELETE FROM queue_items WHERE id = ?", (item_id,))


def clear_finished() -> int:
    _ensure()
    with connection() as conn:
        cur = conn.execute("DELETE FROM queue_items WHERE status IN ('done','failed','cancelled')")
        return cur.rowcount


def move_item(item_id: str, direction: str) -> bool:
    """Swap an item with its neighbour among not-yet-finished items."""
    items = [i for i in list_items() if i.status not in FINISHED]
    idx = next((n for n, i in enumerate(items) if i.id == item_id), None)
    if idx is None:
        return False
    other = idx - 1 if direction == "up" else idx + 1
    if not 0 <= other < len(items):
        return False
    a, b = items[idx], items[other]
    update_item(a.id, position=b.position)
    update_item(b.id, position=a.position)
    return True


def is_paused() -> bool:
    _ensure()
    with connection() as conn:
        row = conn.execute("SELECT value FROM queue_state WHERE key = 'paused'").fetchone()
    return bool(row and row["value"] == "1")


def set_paused(paused: bool) -> None:
    _ensure()
    with connection() as conn:
        conn.execute(
            "INSERT INTO queue_state (key, value) VALUES ('paused', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if paused else "0",),
        )


# -- default collaborators ---------------------------------------------------


def default_download(url: str) -> Source:
    from ..config import load as load_settings
    from ..db import store
    from ..pipeline import ingest

    source = ingest.ingest_youtube(url, load_settings().ingest)
    store.create_source(source)
    return source


def default_create_job(source_id: str, template: str) -> str:
    from ..config import load as load_settings
    from ..db import store
    from .queue import queue

    settings = load_settings()
    data = settings.model_dump(mode="json")
    data["template"] = template or "auto"
    job = Job(id=new_id(), source_id=source_id, provider=settings.active_provider, settings=data)
    store.create_job(job)
    queue.notify()
    return job.id


def default_get_job(job_id: str):
    from ..db import store

    return store.get_job(job_id)


def default_cancel_job(job_id: str) -> None:
    from .queue import queue

    queue.cancel(job_id)


# -- the worker --------------------------------------------------------------


class BatchQueue:
    def __init__(
        self,
        download: Callable[[str], Source] = default_download,
        create_job: Callable[[str, str], str] = default_create_job,
        get_job: Callable[[str], Any] = default_get_job,
        cancel_job: Callable[[str], None] = default_cancel_job,
    ) -> None:
        self._download = download
        self._create_job = create_job
        self._get_job = get_job
        self._cancel_job = cancel_job
        self._task: asyncio.Task | None = None
        self._dl_task: asyncio.Task | None = None
        self._dl_item: str | None = None
        self._removed: set[str] = set()

    # lifecycle
    async def start(self) -> None:
        await asyncio.to_thread(self._recover)
        self._task = asyncio.create_task(self._loop(), name="clipstudio-batch-queue")

    async def stop(self) -> None:
        for t in (self._task, self._dl_task):
            if t is not None:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self._task = self._dl_task = None

    def _recover(self) -> None:
        for it in list_items():
            if it.status == "downloading":
                update_item(it.id, status="pending", progress=0)
            elif it.status == "running" and not it.job_id:
                update_item(it.id, status="ready" if it.source_id else "pending")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Batch queue tick failed.")
            await asyncio.sleep(POLL_S)

    # control (called from API)
    async def remove(self, item_id: str) -> bool:
        item = await asyncio.to_thread(get_item, item_id)
        if item is None:
            return False
        if item.status == "running" and item.job_id:
            await asyncio.to_thread(self._cancel_job, item.job_id)
        if item.id == self._dl_item:
            self._removed.add(item.id)
        await asyncio.to_thread(delete_item, item_id)
        return True

    # one scheduling step; safe to call repeatedly
    async def tick(self) -> None:
        items = await asyncio.to_thread(list_items)

        # 1. follow the running item's job
        for it in items:
            if it.status == "running" and it.job_id:
                job = await asyncio.to_thread(self._get_job, it.job_id)
                if job is None:
                    await asyncio.to_thread(
                        update_item, it.id, status="failed", error="Job disappeared."
                    )
                elif job.status in FINISHED:
                    await asyncio.to_thread(
                        update_item, it.id, status=job.status, progress=job.progress,
                        stage=job.current_stage, error=job.error,
                    )
                elif it.progress != job.progress or it.stage != job.current_stage:
                    await asyncio.to_thread(
                        update_item, it.id, progress=job.progress, stage=job.current_stage
                    )

        if await asyncio.to_thread(is_paused):
            return
        items = await asyncio.to_thread(list_items)
        running = [i for i in items if i.status == "running"]

        # 2. start the next render (never two)
        if not running:
            ready = next((i for i in items if i.status == "ready"), None)
            if ready is not None and ready.source_id:
                try:
                    job_id = await asyncio.to_thread(
                        self._create_job, ready.source_id, ready.template
                    )
                    await asyncio.to_thread(update_item, ready.id, status="running", job_id=job_id)
                except Exception as exc:
                    log.exception("Could not create job for queue item %s.", ready.id)
                    await asyncio.to_thread(
                        update_item, ready.id, status="failed", error=str(exc)
                    )

        # 3. prefetch: at most one download, and at most one finished download waiting
        items = await asyncio.to_thread(list_items)
        if self._dl_task is not None and self._dl_task.done():
            self._dl_task = None
            self._dl_item = None
        waiting = [i for i in items if i.status == "ready"]
        if self._dl_task is None and not waiting:
            pending = next((i for i in items if i.status == "pending"), None)
            if pending is not None:
                await asyncio.to_thread(
                    update_item, pending.id, status="downloading", stage="download"
                )
                self._dl_item = pending.id
                self._dl_task = asyncio.create_task(self._download_item(pending))

    async def _download_item(self, item: Item) -> None:
        try:
            source = await asyncio.to_thread(self._download, item.url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if item.id not in self._removed:
                await asyncio.to_thread(
                    update_item, item.id, status="failed", error=str(exc)[:500], stage=""
                )
            self._removed.discard(item.id)
            return
        if item.id in self._removed:
            self._removed.discard(item.id)
            return
        await asyncio.to_thread(
            update_item, item.id, status="ready", source_id=source.id,
            title=source.title, stage="",
        )

    async def wait_idle(self) -> None:  # test helper
        if self._dl_task is not None:
            await asyncio.gather(self._dl_task, return_exceptions=True)


batch = BatchQueue()
