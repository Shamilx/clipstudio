"""Campaign rules: extraction, fallback, metadata merge, overrides, post text."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace

from clipstudio import campaign
from clipstudio.config import Settings
from clipstudio.db.models import Clip
from clipstudio.pipeline import editplan
from clipstudio.pipeline.runner import write_post_text
from clipstudio.providers.base import DetectionConfig, TranscriptWindow, render_window_prompt

BRIEF = "Required hashtags: #Texas #Movie\nCaption must say: Coming November 17 on digital."


class FakeProvider:
    def __init__(self, reply: str = "", fail: bool = False) -> None:
        self.reply, self.fail, self.user = reply, fail, ""

    async def _complete(self, system, user, config):
        self.user = user
        if self.fail:
            raise RuntimeError("boom")
        return self.reply


def _run(coro):
    return asyncio.run(coro)


def test_extract_parses_and_passes_outdated_warning() -> None:
    reply = "```json\n" + json.dumps({
        "required_hashtags": ["texas", "#Movie"],
        "required_caption_text": ["Out now on digital"],
        "caption_style": "funny",
        "forbidden": ["no ad look"],
        "footage_type": "film",
        "onscreen_text_required": False,
        "warnings": ["'coming November 17' is outdated"],
    }) + "\n```"
    provider = FakeProvider(reply)
    rules = _run(campaign.extract_rules(BRIEF, date(2026, 11, 19), provider))
    assert "2026-11-19" in provider.user
    assert rules.required_hashtags == ["#texas", "#Movie"]
    assert rules.footage_type == "film"
    assert rules.warnings == ["'coming November 17' is outdated"]
    assert rules.required_caption_text == ["Out now on digital"]
    assert rules.brand_safe


def test_fallback_on_llm_failure_and_garbage() -> None:
    for provider in (FakeProvider(fail=True), FakeProvider("not json"), None):
        rules = _run(campaign.extract_rules(BRIEF, date(2026, 1, 1), provider))
        assert rules.required_hashtags == ["#Texas", "#Movie"]
        assert rules.required_caption_text == [] and rules.footage_type == "other"


def test_merge_metadata_dedupes_and_orders() -> None:
    rules = campaign.CampaignRules(
        required_hashtags=["#Texas", "#movie"],
        required_caption_text=["Out now on digital", "Already here"],
    )
    desc, tags = campaign.merge_metadata(
        rules, "Great moment? already here too.", ["#shorts", "#TEXAS", "#a", "#b", "#c", "#d", "#e", "#f"]
    )
    assert tags[:2] == ["#Texas", "#movie"]
    assert len({t.lower() for t in tags}) == len(tags) and len(tags) <= 8
    assert desc.endswith("\n\nOut now on digital")
    assert desc.lower().count("already here") == 1
    # idempotent
    assert campaign.merge_metadata(rules, desc, tags) == (desc, tags)


def test_film_profile_disables_trims() -> None:
    from clipstudio import profiles

    settings = Settings()
    profiles.apply_to_edit(profiles.load_profile("film"), settings.edit)
    assert not settings.edit.trim_silence
    assert not settings.edit.remove_fillers
    assert not settings.edit.punch_zoom
    assert not settings.edit.progress_bar


def test_brand_safe_overrides_template_limits() -> None:
    template = SimpleNamespace(max_emoji=3, sfx_enabled=True)
    limits = editplan.TemplateLimits.of(template, brand_safe=True)
    assert limits.max_emoji == 0 and not limits.sfx_enabled
    assert editplan.TemplateLimits.of(template).max_emoji == 3


def test_post_text_written(tmp_path) -> None:
    clip = Clip(id="c", job_id="j", start_s=0, end_s=5, title="My title",
                description="Caption.\n\nOut now", hashtags=["#a", "#b"])
    out = write_post_text(clip, tmp_path / "clip_9x16.mp4")
    assert out.name == "clip_9x16.txt"
    assert out.read_text(encoding="utf-8") == "My title\n\nCaption.\n\nOut now\n\n#a #b\n"


def test_prompt_mentions_caption_style() -> None:
    window = TranscriptWindow(text="[0]hi", first_word=0, last_word=5)
    note = campaign.prompt_section(campaign.CampaignRules(caption_style="funny and organic"))
    prompt = render_window_prompt(window, DetectionConfig(brief=BRIEF, campaign_note=note))
    assert "funny and organic" in prompt
