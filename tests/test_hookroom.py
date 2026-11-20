"""Hook writers' room: validation, judging, fallbacks, names, persistence, API, results."""

from __future__ import annotations

import asyncio
import json

import pytest
from clipstudio import app as app_module
from clipstudio.app import create_app
from clipstudio.db import store
from clipstudio.db.models import Clip, Job, Source, new_id
from clipstudio.pipeline import editplan, hookroom, hooks
from clipstudio.pipeline.runner import JobWorkspace
from fastapi.testclient import TestClient

TRANSCRIPT = (
    "so we opened the trunk and my mom just stood there for a while. "
    "Nic could not say a word. The last gift was for her."
)


class Scripted:
    """Fake provider: replies come from a function of (system, user)."""

    def __init__(self, writer=None, judge=None) -> None:
        self.writer, self.judge, self.calls = writer, judge, []

    async def _complete(self, system, user, config):
        is_judge = "tough editor" in system
        self.calls.append("judge" if is_judge else "writer")
        reply = self.judge if is_judge else self.writer
        if isinstance(reply, Exception):
            raise reply
        return reply(user) if callable(reply) else reply


def cands(*pairs):
    return json.dumps({"candidates": [{"formula": f, "hook": h} for f, h in pairs]})


def scores(*rows):
    return json.dumps(
        {
            "scores": [
                {
                    "id": i,
                    "specificity": s,
                    "curiosity": c,
                    "readability": r,
                    "accuracy": a,
                    "brief": b,
                }
                for i, (s, c, r, a, b) in enumerate(rows)
            ]
        }
    )


GOOD = [
    ("curiosity", "Who is the last gift for?"),
    ("claim", "Mom never wraps gifts alone"),
    ("stakes", "One trunk, three years of secrets"),
    ("in_medias_res", "Nic opens the locked trunk"),
    ("contrast", "She expected jewelry, got a letter"),
]


def run(provider, **kw):
    ctx = hookroom.HookContext.build(
        transcript=kw.pop("transcript", TRANSCRIPT), **kw.pop("ctx", {})
    )
    return asyncio.run(hookroom.run_room(provider, ctx, **kw))


class TestValidation:
    @pytest.mark.parametrize(
        "text",
        [
            "Who's that gift for, St?",  # dangling abbreviation
            "He opened the",  # dangling article
            "He just wanted to",  # dangling preposition in a statement
            "The sheriff draws first and",
            "The sheriff draws first,",
            "One two three four five six seven eight nine",  # 9 words
            "A" * 70,
            "He did it \U0001f525 again",  # emoji
            "Look at #this thing now",
            "Ask @nic about the trunk",
            'He said "stop right there',
            "(Nic opens the trunk",
            "Can you believe this?",
            "so we opened the trunk and",  # copies the transcript opening
            "",
        ],
    )
    def test_rejects(self, text: str) -> None:
        assert hooks.validate_hook(text, transcript=TRANSCRIPT) is not None, text

    @pytest.mark.parametrize(
        "text",
        [
            "Who is the last gift for?",
            "This mistake cost me $40,000",
            "Why did he say so?",
            "Plan B saved the whole show",
            "Nobody knows who she is",
        ],
    )
    def test_accepts(self, text: str) -> None:
        assert hooks.validate_hook(text, transcript=TRANSCRIPT) is None, text

    def test_never_truncates(self) -> None:
        assert hooks.clean_hook("one two three four five six seven eight nine ten") == ""

    def test_title_fallback_whole_phrase(self) -> None:
        assert hooks.title_fallback("The Ambush at Dawn") == "The Ambush at Dawn"
        out = hooks.title_fallback("The sheriff draws first and misses the outlaw completely today")
        assert out and len(out.split()) <= 8 and out.split()[-1].lower() not in hooks.DANGLING_WORDS


class TestRoom:
    def test_judge_picks_highest_and_alt_is_other_formula(self) -> None:
        provider = Scripted(
            writer=cands(*GOOD),
            judge=scores(
                (6, 6, 9, 9, 9), (5, 5, 9, 9, 9), (7, 7, 9, 9, 9), (9, 9, 9, 9, 9), (8, 8, 9, 9, 9)
            ),
        )
        room = run(provider)
        assert room.hook == "Nic opens the locked trunk"
        assert room.alt == "She expected jewelry, got a letter"
        assert provider.calls == ["writer", "judge"]
        assert len(room.candidates) == 5 and room.candidates[3]["score"] == 9.0
        assert room.candidates[3]["scores"]["specificity"] == 9.0

    def test_unsafe_candidate_cannot_win(self) -> None:
        provider = Scripted(
            writer=cands(*GOOD),
            judge=scores(
                (9, 9, 9, 2, 9), (5, 5, 9, 9, 9), (5, 5, 9, 9, 9), (6, 6, 9, 9, 9), (5, 5, 9, 9, 9)
            ),
        )
        assert run(provider).hook == "Nic opens the locked trunk"

    def test_invalid_candidates_rejected_not_truncated(self) -> None:
        provider = Scripted(
            writer=cands(
                ("curiosity", "Who is the last gift for, St?"),
                ("claim", "Mom never wraps gifts for her kids and"),
                ("stakes", "One trunk, three years of secrets"),
                ("in_medias_res", "Nic opens the locked trunk \U0001f525"),
                ("contrast", "She expected jewelry, got a letter"),
            ),
            judge=scores((5, 5, 9, 9, 9), (8, 8, 9, 9, 9)),
        )
        room = run(provider)
        rejected = [c for c in room.candidates if c.get("rejected")]
        assert len(rejected) == 3
        assert room.hook == "She expected jewelry, got a letter"
        assert room.alt == "One trunk, three years of secrets"

    def test_judge_failure_still_picks_by_formula_order(self) -> None:
        provider = Scripted(writer=cands(*GOOD), judge=RuntimeError("boom"))
        room = run(provider)
        assert room.hook == "Who is the last gift for?"

    def test_writer_failure_returns_none(self) -> None:
        assert run(Scripted(writer=RuntimeError("down"))) is None
        assert run(Scripted(writer="not json")) is None

    def test_fallback_to_highlight_hook_then_title(self) -> None:
        bad = cands(("curiosity", "Wow"), ("claim", "This is crazy"))
        room = run(Scripted(writer=bad), highlight_hook="Nic opens the trunk", title="Gift Reveal")
        assert (room.hook, room.source, room.alt) == ("Nic opens the trunk", "highlight", "")
        room = run(
            Scripted(writer=bad), highlight_hook="Wait for it", title="The Trunk Opens At Last"
        )
        assert (room.hook, room.source) == ("The Trunk Opens At Last", "title")

    def test_brief_name_correction(self) -> None:
        provider = Scripted(
            writer=cands(*[(f, h.replace("Nic", "Nick")) for f, h in GOOD]),
            judge=scores(
                (9, 9, 9, 9, 9), (1, 1, 9, 9, 9), (1, 1, 9, 9, 9), (1, 1, 9, 9, 9), (1, 1, 9, 9, 9)
            ),
        )
        room = run(provider, ctx={"brief": "Hero is Nic. Keep the gift secret.", "title": "t"})
        assert "Nic" in " ".join(c["text"] for c in room.candidates[3:4])
        assert "Nick" not in " ".join(c["text"] for c in room.candidates)

    def test_fix_names(self) -> None:
        names = hookroom.brief_names("Hero is Nic. We love Parker too.", ["Quanah Parker"])
        assert hookroom.fix_names("Nick opens the trunk", names) == "Nic opens the trunk"
        assert hookroom.fix_names("Nic's trunk", names) == "Nic's trunk"
        assert hookroom.fix_names("Parkr rides in", names) == "Parker rides in"
        assert hookroom.fix_names("Nobody opens the trunk", names) == "Nobody opens the trunk"
        assert hookroom.fix_names("nick opens", names) == "nick opens"  # lowercase untouched

    def test_examples_reach_the_writer_prompt(self) -> None:
        seen = {}

        def writer(user):
            seen["user"] = user
            return cands(*GOOD)

        ctx = hookroom.HookContext.build(transcript=TRANSCRIPT)
        ctx.examples = '## Real results\nExamples that worked:\n- "X"\n'
        asyncio.run(hookroom.run_room(Scripted(writer=writer, judge="{}"), ctx))
        assert "Examples that worked" in seen["user"]


class TestEnsureSpecificHook:
    def test_room_result_persists_alt(self, initialised_db) -> None:
        source = store.create_source(Source(id=new_id(), type="upload", path="x.mp4"))
        job = store.create_job(Job(id=new_id(), source_id=source.id))
        clip = Clip(id=new_id(), job_id=job.id, start_s=0, end_s=40, rank=1, hook="old")
        store.replace_clips(job.id, [clip])
        provider = Scripted(
            writer=cands(*GOOD),
            judge=scores(
                (9, 9, 9, 9, 9), (5, 5, 9, 9, 9), (5, 5, 9, 9, 9), (7, 7, 9, 9, 9), (5, 5, 9, 9, 9)
            ),
        )
        hook, how = asyncio.run(
            hooks.ensure_specific_hook(
                "old", provider=provider, title="Gift", transcript=TRANSCRIPT, clip_id=clip.id
            )
        )
        assert (hook, how) == ("Who is the last gift for?", "regenerated")
        saved = store.get_clip(clip.id)
        assert saved.hook_alt == "Nic opens the locked trunk"
        assert (
            len(saved.hook_candidates) == 5 and saved.hook_candidates[0]["formula"] == "curiosity"
        )

    def test_falls_back_to_old_behaviour(self) -> None:
        provider = Scripted(writer=RuntimeError("down"))
        hook, how = asyncio.run(
            hooks.ensure_specific_hook(
                "Nic opens the trunk", provider=provider, title="t", transcript=TRANSCRIPT
            )
        )
        assert (hook, how) == ("Nic opens the trunk", "kept")
        hook, how = asyncio.run(
            hooks.ensure_specific_hook(
                "Who's that gift for, St?", provider=None, title="The Trunk Opens", transcript=""
            )
        )
        assert (hook, how) == ("The Trunk Opens", "title")


class TestEditplanHook:
    def test_long_hook_is_dropped_not_truncated(self) -> None:
        assert editplan._clean_hook("one two three four five six seven eight nine") == ""
        assert editplan._clean_hook("Nic opens the trunk") == "Nic opens the trunk"


# -- API ---------------------------------------------------------------------


@pytest.fixture
def client(clipstudio_home, monkeypatch):
    monkeypatch.setenv(app_module.ENV_NO_WORKER, "1")
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def clip(client) -> Clip:
    source = store.create_source(Source(id=new_id(), type="upload", path="x.mp4", duration_s=100))
    job = store.create_job(Job(id=new_id(), source_id=source.id))
    c = Clip(
        id=new_id(),
        job_id=job.id,
        start_s=0,
        end_s=40,
        rank=1,
        hook="Who is the last gift for?",
        hook_alt="Nic opens the locked trunk",
        hook_candidates=[
            {"text": "Who is the last gift for?", "formula": "curiosity", "score": 8.0},
            {"text": "Nic opens the locked trunk", "formula": "in_medias_res", "score": 7.5},
        ],
    )
    store.replace_clips(job.id, [c])
    plan = editplan.EditPlanData(hook_text=c.hook)
    plan.save(JobWorkspace(job.id).edit_plan(c.id))
    return c


class TestHookApi:
    def test_clip_out_exposes_variants(self, client, clip) -> None:
        body = client.get(f"/api/clips/{clip.id}").json()
        assert body["hook_alt"] == "Nic opens the locked trunk"
        assert body["hook_candidates"][0]["score"] == 8.0

    def test_patch_hook_updates_clip_and_plan(self, client, clip) -> None:
        r = client.patch(f"/api/clips/{clip.id}", json={"hook": "  Nic   finds the letter "})
        assert r.status_code == 200 and r.json()["hook"] == "Nic finds the letter"
        plan = editplan.EditPlanData.load(JobWorkspace(clip.job_id).edit_plan(clip.id))
        assert plan.hook_text == "Nic finds the letter"

    def test_patch_hook_validation(self, client, clip) -> None:
        assert client.patch(f"/api/clips/{clip.id}", json={"hook": "   "}).status_code == 400
        assert (
            client.patch(f"/api/clips/{clip.id}", json={"hook": "Hot \U0001f525"}).status_code
            == 400
        )

    def test_swap(self, client, clip) -> None:
        r = client.post(f"/api/clips/{clip.id}/hook/swap")
        assert r.status_code == 200
        assert (r.json()["hook"], r.json()["hook_alt"]) == (
            "Nic opens the locked trunk",
            "Who is the last gift for?",
        )
        plan = editplan.EditPlanData.load(JobWorkspace(clip.job_id).edit_plan(clip.id))
        assert plan.hook_text == "Nic opens the locked trunk"
        # swapping twice restores the original
        r = client.post(f"/api/clips/{clip.id}/hook/swap")
        assert r.json()["hook"] == "Who is the last gift for?"

    def test_swap_without_alt(self, client, clip) -> None:
        store.update_clip(clip.id, hook_alt="")
        assert client.post(f"/api/clips/{clip.id}/hook/swap").status_code == 400
        assert client.post("/api/clips/nope/hook/swap").status_code == 404


class TestResults:
    def test_add_list_delete(self, client, clip) -> None:
        r = client.post(
            f"/api/clips/{clip.id}/results",
            json={"platform": "youtube", "views": 1200, "viewed_pct": 71.5, "days_live": 3},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["hook"] == "Who is the last gift for?" and body["formula"] == "curiosity"
        assert body["score"] == 71.5
        listed = client.get(f"/api/clips/{clip.id}/results").json()
        assert [x["id"] for x in listed] == [body["id"]]
        assert client.delete(f"/api/clips/{clip.id}/results/{body['id']}").status_code == 204
        assert client.get(f"/api/clips/{clip.id}/results").json() == []
        assert client.delete(f"/api/clips/{clip.id}/results/{body['id']}").status_code == 404

    def test_validation(self, client, clip) -> None:
        url = f"/api/clips/{clip.id}/results"
        assert client.post(url, json={"platform": "vimeo", "views": 1}).status_code == 422
        assert client.post(url, json={"platform": "tiktok", "views": -1}).status_code == 422
        assert client.post(url, json={"platform": "tiktok", "viewed_pct": 140}).status_code == 422
        assert (
            client.post("/api/clips/nope/results", json={"platform": "tiktok"}).status_code == 404
        )

    def test_alt_hook_recorded_for_platform(self, client, clip) -> None:
        r = client.post(
            f"/api/clips/{clip.id}/results",
            json={"platform": "tiktok", "views": 500, "hook": "Nic opens the locked trunk"},
        )
        assert r.json()["formula"] == "in_medias_res"


def _row(hook, **kw):
    return {"hook": hook, "formula": "claim", "platform": "youtube", "views": 100,
            "viewed_pct": None, "avg_view_pct": None, "days_live": 1, "title": "T", **kw}  # fmt: skip


class TestLearning:
    def test_score_prefers_viewed_then_avg_then_views_per_day(self) -> None:
        assert hookroom.score_result(_row("a", viewed_pct=80, avg_view_pct=10)) == 80
        assert hookroom.score_result(_row("a", avg_view_pct=40)) == 40
        slow = hookroom.score_result(_row("a", views=1000, days_live=10))
        fast = hookroom.score_result(_row("a", views=1000, days_live=1))
        assert fast > slow > 0

    def test_needs_three_results(self) -> None:
        assert hookroom.select_examples(
            [_row("a one two", viewed_pct=60), _row("b one two", viewed_pct=30)]
        ) == ([], [])
        assert hookroom.examples_block([], []) == ""

    def test_top5_bottom3(self) -> None:
        rows = [_row(f"hook number {i}", viewed_pct=float(i * 10)) for i in range(1, 11)]
        top, bottom = hookroom.select_examples(rows)
        assert [e["hook"] for e in top] == [f"hook number {i}" for i in (10, 9, 8, 7, 6)]
        assert [e["hook"] for e in bottom] == ["hook number 1", "hook number 2", "hook number 3"]
        block = hookroom.examples_block(top, bottom)
        assert "Examples that worked" in block and "Examples that did not work" in block
        assert '"hook number 10" (claim; 100% viewed on youtube)' in block

    def test_same_hook_is_averaged(self) -> None:
        rows = [_row("same hook here", viewed_pct=80), _row("Same hook here", viewed_pct=40),
                _row("other hook now", viewed_pct=70)]  # fmt: skip
        top, bottom = hookroom.select_examples(rows)
        assert top[0]["hook"].lower() == "other hook now"
        assert bottom[0]["score"] == 60

    def test_few_hooks_still_get_a_bad_example(self) -> None:
        rows = [_row("a good hook", viewed_pct=90), _row("a mid hook", viewed_pct=50),
                _row("a bad hook", viewed_pct=10)]  # fmt: skip
        top, bottom = hookroom.select_examples(rows)
        assert [e["hook"] for e in bottom] == ["a bad hook"]
        assert [e["hook"] for e in top] == ["a good hook", "a mid hook"]

    def test_load_examples_from_db(self, client, clip) -> None:
        assert hookroom.load_examples() == ""
        for pct in (70, 40, 20):
            client.post(
                f"/api/clips/{clip.id}/results", json={"platform": "youtube", "viewed_pct": pct}
            )
        # one hook, three results -> averaged into a single entry
        assert "Who is the last gift for?" in hookroom.load_examples()
