"""Bulk intake queue: ordering, persistence, one-render-at-a-time."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from clipstudio.db.models import Source, new_id
from clipstudio.jobs import batch as bq


class Fake:
    """Fake downloader + job runner that tracks concurrency."""

    def __init__(self):
        self.jobs: dict[str, str] = {}
        self.created: list[str] = []
        self.max_running = 0

    def download(self, url):
        return Source(id=new_id(), type="youtube", path="x", title=f"T {url}", url=url)

    def create_job(self, source_id, template):
        jid = new_id()
        self.jobs[jid] = "running"
        self.created.append(jid)
        running = sum(1 for s in self.jobs.values() if s == "running")
        self.max_running = max(self.max_running, running)
        return jid

    def get_job(self, jid):
        return SimpleNamespace(
            status=self.jobs[jid], progress=0.5, current_stage="render", error=None
        )

    def finish(self, jid):
        self.jobs[jid] = "done"

    def make(self):
        return bq.BatchQueue(self.download, self.create_job, self.get_job, lambda j: None)


@pytest.fixture
def db(initialised_db):
    return initialised_db


def statuses():
    return [i.status for i in bq.list_items()]


def test_add_order_dedup_and_move(db):
    items = bq.add_items(["u1", "u2", "u3", "u2"], "talk")
    assert [i.url for i in items] == ["u1", "u2", "u3"]
    assert [i.url for i in bq.list_items()] == ["u1", "u2", "u3"]
    assert bq.move_item(items[2].id, "up")
    assert [i.url for i in bq.list_items()] == ["u1", "u3", "u2"]
    assert not bq.move_item(items[0].id, "up")
    assert bq.list_items()[0].template == "talk"


def test_one_render_at_a_time_and_prefetch(db):
    fake = Fake()
    q = fake.make()
    bq.add_items(["a", "b", "c"])

    async def run():
        await q.tick()  # starts download of a
        await q.wait_idle()
        await q.tick()  # a -> running, starts download of b
        await q.wait_idle()
        assert statuses() == ["running", "ready", "pending"]
        await q.tick()  # b waits: a still running; no extra download (b ready)
        assert statuses() == ["running", "ready", "pending"]
        assert len(fake.created) == 1
        fake.finish(fake.created[0])
        await q.tick()  # a done, b starts
        assert statuses()[:2] == ["done", "running"]  # c may already be prefetching
        await q.wait_idle()
        await q.tick()
        fake.finish(fake.created[1])
        for _ in range(4):
            await q.tick()
            await q.wait_idle()
            if len(fake.created) == 3:
                fake.finish(fake.created[2])
        assert statuses() == ["done", "done", "done"]

    asyncio.run(run())
    assert fake.max_running == 1


def test_pause_and_resume(db):
    fake = Fake()
    q = fake.make()
    bq.add_items(["a"])
    bq.set_paused(True)

    async def run():
        await q.tick()
        await q.wait_idle()
        assert statuses() == ["pending"]
        bq.set_paused(False)
        await q.tick()
        await q.wait_idle()
        assert statuses() == ["downloading"] or statuses() == ["ready"]

    asyncio.run(run())


def test_persistence_and_recovery(db):
    items = bq.add_items(["a", "b"])
    bq.update_item(items[0].id, status="downloading")
    bq.update_item(items[1].id, status="running", job_id="job1")
    bq.set_paused(True)
    q = Fake().make()
    asyncio.run(q.start())
    asyncio.run(q.stop())
    got = {i.url: i for i in bq.list_items()}
    assert got["a"].status == "pending"  # interrupted download restarts
    assert got["b"].status == "running" and got["b"].job_id == "job1"  # job tracked
    assert bq.is_paused()


def test_failed_download_and_remove(db):
    fake = Fake()

    def boom(url):
        raise RuntimeError("nope")

    q = bq.BatchQueue(boom, fake.create_job, fake.get_job, lambda j: None)
    items = bq.add_items(["a", "b"])

    async def run():
        await q.tick()
        await q.wait_idle()
        assert statuses()[0] == "failed"
        assert await q.remove(items[1].id)
        assert not await q.remove("missing")

    asyncio.run(run())
    assert [i.url for i in bq.list_items()] == ["a"]
    assert bq.clear_finished() == 1
