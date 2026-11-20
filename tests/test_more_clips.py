"""Reject reasons, campaign brief injection, and "generate more clips"."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from clipstudio import app as app_module
from clipstudio.app import create_app
from clipstudio.db import store
from clipstudio.db.models import Clip, Job, Source, new_id
from clipstudio.pipeline import highlights
from clipstudio.providers.base import (
    BRIEF_MAX_CHARS,
    DetectionConfig,
    TranscriptWindow,
    render_window_prompt,
)
from fastapi.testclient import TestClient


def _clip(start: float, end: float, **kw) -> Clip:
    return Clip(id=new_id(), job_id="j", start_s=start, end_s=end, **kw)


class TestOverlapFilter:
    def test_fraction_uses_union_of_existing(self) -> None:
        new = _clip(0, 100)
        existing = [_clip(0, 20), _clip(10, 30)]  # union 0-30 -> 30%
        assert highlights.overlap_fraction(new, existing) == pytest.approx(0.30)

    def test_drops_over_threshold_keeps_at_or_below(self) -> None:
        existing = [_clip(0, 100)]
        over = _clip(69, 169)  # 31% overlap
        edge = _clip(70, 170)  # 30% overlap
        clear = _clip(200, 260)
        kept = highlights.drop_overlapping([over, edge, clear], existing)
        assert kept == [edge, clear]

    def test_no_existing_keeps_everything(self) -> None:
        clips = [_clip(0, 10)]
        assert highlights.drop_overlapping(clips, []) == clips


class TestPromptInjection:
    WINDOW = TranscriptWindow(text="[0]hi [1]there", first_word=0, last_word=500)

    def test_brief_is_delimited_and_truncated(self) -> None:
        cfg = DetectionConfig(brief="Show the sneakers. " + "x" * 5000)
        prompt = render_window_prompt(self.WINDOW, cfg)
        assert "Campaign rules from the client" in prompt
        assert "Show the sneakers." in prompt
        assert "x" * BRIEF_MAX_CHARS not in prompt  # cut off before 3000 x's
        assert prompt.count("x") < BRIEF_MAX_CHARS

    def test_no_brief_no_section(self) -> None:
        prompt = render_window_prompt(self.WINDOW, DetectionConfig())
        assert "Campaign rules" not in prompt
        assert "Already used" not in prompt

    def test_exclusions_listed(self) -> None:
        existing = [
            _clip(0, 30, start_word=10, end_word=90, title="Good one"),
            _clip(
                40,
                70,
                start_word=120,
                end_word=200,
                title="Promo clip",
                status="discarded",
                reject_reason="ad_or_announcement",
            ),  # fmt: skip
            _clip(900, 950, start_word=9000, end_word=9100),  # outside window
        ]
        cfg = DetectionConfig()
        highlights.apply_exclusions(cfg, existing)
        prompt = render_window_prompt(self.WINDOW, cfg)

        assert "10-90" in prompt and "120-200" in prompt
        assert "9000-9100" not in prompt
        assert "avoid similar moments" in prompt
        assert '"Promo clip"' in prompt and "ad" in prompt
        assert '"Good one"' not in prompt  # only rejected clips are "disliked"

    def test_always_on_rules_in_every_prompt_variant(self) -> None:
        from clipstudio.providers.base import load_prompt

        for version in ("highlight_v1", "highlight_v2", "highlight_v3"):
            text = load_prompt(version)
            assert "mini story or payoff" in text
            assert "giveaway" in text


@pytest.fixture
def client(clipstudio_home, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv(app_module.ENV_NO_WORKER, "1")
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def done_job(initialised_db) -> Job:
    source = store.create_source(
        Source(id=new_id(), type="upload", path="C:/m.mp4", title="S", duration_s=600.0)
    )
    job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
    store.replace_clips(
        job.id,
        [
            Clip(id=new_id(), job_id=job.id, rank=r, start_s=r * 60.0, end_s=r * 60.0 + 40,
                 start_word=r * 100, end_word=r * 100 + 80, title=f"Clip {r}")
            for r in (1, 2)
        ],
    )  # fmt: skip
    return job


class TestRejectReason:
    def test_reason_persists_and_clears_on_restore(self, client, done_job) -> None:
        clip_id = client.get(f"/api/jobs/{done_job.id}/clips").json()[0]["id"]

        # One click rejects with no reason...
        r = client.patch(f"/api/clips/{clip_id}", json={"status": "discarded"})
        assert r.json()["reject_reason"] == ""

        # ...then a quick button adds one, with optional text.
        r = client.patch(
            f"/api/clips/{clip_id}",
            json={"reject_reason": "boring_no_story", "reject_note": "nothing happens"},
        )
        body = r.json()
        assert body["status"] == "discarded"
        assert body["reject_reason"] == "boring_no_story"
        assert store.get_clip(clip_id).reject_note == "nothing happens"

        # Undoing the reject forgets the reason.
        r = client.patch(f"/api/clips/{clip_id}", json={"status": "candidate"})
        assert r.json()["reject_reason"] == ""

    def test_unknown_reason_rejected(self, client, done_job) -> None:
        clip_id = client.get(f"/api/jobs/{done_job.id}/clips").json()[0]["id"]
        r = client.patch(f"/api/clips/{clip_id}", json={"reject_reason": "meh"})
        assert r.status_code == 422


class TestBriefAndMoreEndpoint:
    def test_brief_stored_with_job_and_truncated(self, client, done_job) -> None:
        r = client.post(
            "/api/jobs",
            json={"source_id": done_job.source_id, "brief": "b" * (BRIEF_MAX_CHARS + 1000)},
        )
        assert r.status_code == 201
        assert len(r.json()["brief"]) == BRIEF_MAX_CHARS
        assert len(store.get_job(r.json()["id"]).settings["brief"]) == BRIEF_MAX_CHARS

    def test_more_requeues_done_job(self, client, done_job) -> None:
        r = client.post(f"/api/jobs/{done_job.id}/more", json={"count": 4})
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["more_count"] == 4

    def test_more_requires_finished_job(self, client, done_job) -> None:
        store.update_job(done_job.id, status="running")
        r = client.post(f"/api/jobs/{done_job.id}/more", json={"count": 4})
        assert r.status_code == 409

    def test_append_clips_continues_ranking(self, done_job) -> None:
        new = store.append_clips(done_job.id, [_clip(500, 540), _clip(600, 640)])
        assert [c.rank for c in new] == [3, 4]
        assert len(store.list_clips(done_job.id)) == 4
