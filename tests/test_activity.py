"""Live activity: migration, throttling, DB writes and the waiting ticker."""

from __future__ import annotations

import asyncio
import json

from clipstudio import activity, db
from clipstudio.db import store
from clipstudio.db.models import Job, Source


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _tracker(min_interval_s: float = 0.5):
    clock = Clock()
    writes: list[tuple] = []
    tracker = activity.ActivityTracker(
        "j1",
        writer=lambda *a: writes.append(a),
        clock=clock,
        min_interval_s=min_interval_s,
    )
    return tracker, clock, writes


def test_migration_adds_activity_columns(initialised_db):
    with db.connection() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"activity", "heartbeat_at", "activity_log_json"} <= cols


def test_changed_log_message_always_written():
    tracker, _clock, writes = _tracker()
    assert tracker.set("one")
    assert tracker.set("two")  # same instant, but a new message
    assert [w[1] for w in writes] == ["one", "two"]
    assert [e["msg"] for e in json.loads(writes[-1][3])] == ["one", "two"]


def test_transient_and_repeat_updates_are_throttled():
    tracker, clock, writes = _tracker()
    tracker.set("Rendering: 1%")
    assert not tracker.set("Rendering: 2%", log=False)
    assert not tracker.set("Rendering: 1%")
    clock.t += 0.6
    assert tracker.set("Rendering: 3%", log=False)
    assert len(writes) == 2
    # Transient updates never add log lines.
    assert len(json.loads(writes[-1][3])) == 1


def test_log_is_capped():
    tracker, _c, writes = _tracker()
    for i in range(50):
        tracker.set(f"m{i}")
    assert len(json.loads(writes[-1][3])) == activity.LOG_LIMIT


def test_activity_written_to_db_on_emit(initialised_db):
    from clipstudio.pipeline import Stage
    from clipstudio.pipeline.runner import PipelineRunner

    store.create_source(Source(id="s1", type="upload", path="x.mp4", title="t"))
    job = store.create_job(Job(id="j1", source_id="s1"))
    runner = PipelineRunner(job, store.get_source("s1"))
    runner._emit(Stage.TRANSCRIBE, 0.1, "Transcribing with Groq Whisper (chunk 2/3)")
    got = store.get_job("j1")
    assert got.activity == "Transcribing with Groq Whisper (chunk 2/3)"
    assert got.heartbeat_at
    assert got.activity_log[-1]["stage"] == "transcribe"
    from clipstudio.api.schemas import JobOut

    assert JobOut.of(got).activity == got.activity


def test_waiting_ticker_updates_counter():
    tracker, clock, writes = _tracker(min_interval_s=0.0)
    activity.bind(tracker)
    try:

        async def slow():
            async with activity.waiting("Asking Groq", interval_s=0.02):
                await asyncio.sleep(0.15)

        asyncio.run(slow())
    finally:
        activity.bind(None)
    msgs = [w[1] for w in writes]
    assert msgs[0] == "Asking Groq"
    assert any("waiting" in m for m in msgs[1:])
    # Ticks are transient: only the opening line is in the log.
    assert len(json.loads(writes[-1][3])) == 1


def test_relabel_changes_waiting_text():
    tracker, _c, writes = _tracker(min_interval_s=0.0)
    activity.bind(tracker)
    try:

        async def go():
            async with activity.waiting("Asking Groq", interval_s=0.02):
                activity.relabel("Groq failed (rate limit) -> falling back to NVIDIA")
                await asyncio.sleep(0.08)

        asyncio.run(go())
    finally:
        activity.bind(None)
    assert any(m.startswith("Groq failed") and "waiting" in m for m in (w[1] for w in writes))


def test_note_without_tracker_is_noop():
    activity.bind(None)
    activity.note("nothing")
    with activity.waiting_sync("x", interval_s=0.01):
        pass


def test_service_names():
    assert activity.service_name("https://api.groq.com/openai/v1") == "Groq"
    assert activity.service_name("https://integrate.api.nvidia.com/v1") == "NVIDIA"
