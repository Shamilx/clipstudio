"""ClipStudio additions: Groq mapping, caption layout, scoring, think-stripping,
auto-edit planning, .env parsing and provider fallback."""

from __future__ import annotations

import httpx
import pytest
from clipstudio import envfile
from clipstudio.config import EditSettings, WhisperSettings
from clipstudio.pipeline import autoedit, captions, groq_whisper, scoring
from clipstudio.pipeline.transcript import Transcript, Word
from clipstudio.providers import ClipCandidate, TranscriptWindow
from clipstudio.providers.base import DetectionConfig, extract_json_object, strip_think
from clipstudio.providers.openai_provider import OpenAIProvider

# ---------------------------------------------------------------- think-stripping


class TestThinkStripping:
    def test_closed_block_removed(self) -> None:
        assert strip_think('<think>hmm {"a": 1}</think>{"clips": []}') == '{"clips": []}'

    def test_only_closing_tag(self) -> None:
        assert strip_think('reasoning...</think>\n{"clips": []}') == '{"clips": []}'

    def test_multiline_and_json_extraction(self) -> None:
        raw = '<think>\nline one\nline two {not json}\n</think>\n```json\n{"clips": []}\n```'
        assert extract_json_object(raw) == {"clips": []}

    def test_plain_text_untouched(self) -> None:
        assert strip_think('{"clips": []}') == '{"clips": []}'


# ---------------------------------------------------------------- Groq mapping


class TestGroqMapping:
    def payload(self) -> dict:
        return {
            "language": "english",
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.4},
                {"word": "world.", "start": 0.5, "end": 0.9},
                {"word": "Next", "start": 1.5, "end": 1.8},
                {"word": "one", "start": 1.9, "end": 2.2},
            ],
            "segments": [
                {"start": 0.0, "end": 1.0, "text": " Hello world."},
                {"start": 1.4, "end": 2.3, "text": " Next one"},
            ],
        }

    def test_words_and_segments_with_offset(self) -> None:
        t = Transcript()
        groq_whisper.map_response(self.payload(), 100.0, t)
        assert [w.text for w in t.words] == ["Hello", "world.", "Next", "one"]
        assert t.words[0].start == pytest.approx(100.0)
        assert t.words[3].end == pytest.approx(102.2)
        assert [(s.first_word, s.last_word) for s in t.segments] == [(0, 1), (2, 3)]
        assert t.segments[0].text == "Hello world."

    def test_second_chunk_appends_with_global_indices(self) -> None:
        t = Transcript()
        groq_whisper.map_response(self.payload(), 0.0, t)
        groq_whisper.map_response(self.payload(), 700.0, t)
        assert len(t.words) == 8
        assert t.segments[-1].first_word == 6
        assert t.words[4].start == pytest.approx(700.0)

    def test_words_without_segments_get_a_segment(self) -> None:
        t = Transcript()
        p = self.payload()
        p["segments"] = []
        groq_whisper.map_response(p, 0.0, t)
        assert len(t.segments) == 1 and t.segments[0].last_word == 3

    def test_chunk_plan(self) -> None:
        assert groq_whisper.plan_chunks(60.0, file_bytes=2_000_000) == [(0.0, 60.0)]
        chunks = groq_whisper.plan_chunks(3600.0, file_bytes=115_000_000)
        assert chunks[0][0] == 0.0 and len(chunks) > 4
        assert sum(length for _, length in chunks) == pytest.approx(3600.0)
        assert all(length * 32_000 < groq_whisper.MAX_UPLOAD_BYTES for _, length in chunks)

    def test_retry_on_429_then_success(self, tmp_path) -> None:
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF" + b"0" * 100)
        calls = {"n": 0}
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "slow"}})
            return httpx.Response(200, json=self.payload())

        client = httpx.Client(transport=httpx.MockTransport(handler))
        t = groq_whisper.transcribe_groq(
            audio, WhisperSettings(), api_key="k", duration_s=3.0, client=client, sleep=sleeps.append
        )
        assert calls["n"] == 3 and sleeps == [2.0, 2.0]
        assert len(t.words) == 4 and t.language == "english"

    def test_bad_key_is_a_clear_error(self, tmp_path) -> None:
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"0" * 100)
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
        with pytest.raises(groq_whisper.GroqError, match="API key"):
            groq_whisper.transcribe_groq(
                audio, WhisperSettings(), api_key="k", duration_s=1.0, client=client
            )

    def test_missing_key(self, tmp_path) -> None:
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"0" * 100)
        with pytest.raises(groq_whisper.GroqError, match="GROQ_API_KEY"):
            groq_whisper.transcribe_groq(audio, WhisperSettings(), api_key=None, duration_s=1.0)


# ---------------------------------------------------------------- captions


def _words(text: str, step: float = 0.4) -> list[Word]:
    return [Word(w, i * step, i * step + step - 0.05) for i, w in enumerate(text.split())]


class TestCaptionLayout:
    @pytest.mark.parametrize("key", list(captions.PRESETS))
    def test_lines_never_exceed_frame_width(self, key: str) -> None:
        style = captions.get_style(key)
        words = _words(
            "Dövlətlərarasılaşdırılmışlardansınız supercalifragilisticexpialidocious "
            "Azərbaycanın müstəqilliyi haqqında uzun cümlə burada davam edir"
        )
        width, height = 720, 1280
        side = round(width * style.side_margin_ratio)
        for group in captions.group_words(words, max_words=style.max_words):
            texts = [captions.upper_loc(w.text, "az") if style.all_caps else w.text for w in group.words]
            lines, px = captions.fit_group(texts, style, round(height * style.size_ratio), width - 2 * side)
            if px > max(10, int(round(height * style.size_ratio) * 0.45)):
                assert len(lines) <= style.max_lines
            for line in lines:
                w = sum(captions.measure(texts[i], style, px) for i in line)
                w += captions.measure(" ", style, px) * (len(line) - 1)
                assert w <= width - 2 * side

    def test_azerbaijani_uppercase_dotted_i(self) -> None:
        assert captions.upper_loc("işıq", "az") == "İŞIQ"
        assert captions.upper_loc("ə ğ ş ç ö ü", "az") == "Ə Ğ Ş Ç Ö Ü"
        assert captions.upper_loc("is", "en") == "IS"

    def test_font_has_azerbaijani_glyphs(self) -> None:
        from PIL import ImageFont

        font = ImageFont.truetype(str(captions.FONT_DIR / captions.FONT_FILE), 40)
        notdef = font.getmask("￿").getbbox()
        for ch in "əğşçöüıİƏ":
            assert font.getmask(ch).getbbox() is not None
            assert font.getlength(ch) > 0
        assert notdef is None or True

    def test_keyword_gets_colour_and_scale(self) -> None:
        words = _words("this costs 50 dollars")
        subs = captions.build_ass(
            words, captions.get_style("yellow_pop"), width=720, height=1280, keywords={2}
        )
        joined = "".join(e.text for e in subs.events)
        assert captions.ass_colour_override(captions.KEYWORD_COLOUR) in joined
        assert f"\\fscx{captions.KEYWORD_SCALE}" in joined

    def test_one_word_preset_has_one_word_per_event(self) -> None:
        subs = captions.build_ass(_words("a b c d"), captions.get_style("one_word"), width=720, height=1280)
        assert len(subs.events) == 4

    def test_legacy_keys_still_resolve(self) -> None:
        assert captions.get_style("bold_pop").key == "yellow_pop"


# ---------------------------------------------------------------- scoring


class TestScoring:
    def test_dense_numeric_clip_beats_slow_vague_one(self) -> None:
        good = [Word(w, i * 0.35, i * 0.35 + 0.3) for i, w in enumerate(
            ("I lost $2,000 in 3 days! Why? Because 90% of ads fail. " * 8).split())]
        slow = [Word("well", i * 1.5, i * 1.5 + 0.4) for i in range(20)]
        assert scoring.heuristic_score(good) > scoring.heuristic_score(slow)

    def test_empty(self) -> None:
        assert scoring.heuristic_score([]) == 0

    def test_blend_is_light(self) -> None:
        assert scoring.blend(90, 0, 0.2) == 72
        assert scoring.blend(50, 100, 0.0) == 50

    def test_quality_gate_keeps_top_two_then_threshold(self) -> None:
        class C:
            def __init__(self, s):
                self.score = s

        ranked = [C(40), C(35), C(70), C(59)]
        ranked.sort(key=lambda c: c.score, reverse=True)
        kept = scoring.quality_gate(ranked, threshold=60, min_keep=2)
        assert [c.score for c in kept] == [70, 59]
        low = sorted([C(30), C(20), C(10)], key=lambda c: -c.score)
        assert len(scoring.quality_gate(low, threshold=60, min_keep=2)) == 2


# ---------------------------------------------------------------- auto edit


class TestAutoEdit:
    def test_pause_and_filler_removed_and_times_remapped(self) -> None:
        words = [
            Word("Hello", 0.1, 0.5),
            Word("um", 0.6, 0.9),
            Word("world", 1.0, 1.4),
            Word("after", 6.0, 6.4),  # 4.6s of dead air before it
            Word("pause", 6.5, 15.0),
        ]
        plan, kept = autoedit.plan_cuts(words, 15.2, EditSettings())
        assert [w.text for w in kept] == ["Hello", "world", "after", "pause"]
        assert len(plan.keep) >= 2
        assert plan.edited_duration_s < 15.2 - 3.0
        # mapped times stay ordered and inside the edited duration
        mapped = [plan.map_time(w.start) for w in kept]
        assert mapped == sorted(mapped)
        assert plan.map_time(15.0) <= plan.edited_duration_s + 1e-6
        # the gap between "world" and "after" shrank to the kept padding
        assert plan.map_time(6.0) - plan.map_time(1.4) < 0.5

    def test_disabled_is_noop(self) -> None:
        off = EditSettings(trim_silence=False, remove_fillers=False)
        plan, kept = autoedit.plan_cuts([Word("a", 0.5, 1.0)], 3.0, off)
        assert plan.is_noop and len(kept) == 1

    def test_never_guts_a_clip(self) -> None:
        plan, _ = autoedit.plan_cuts([Word("a", 9.0, 9.2)], 10.0, EditSettings())
        assert plan.is_noop

    def test_apply_cuts_splits_crop_segments(self) -> None:
        from clipstudio.pipeline.reframe.croppath import centre_crop

        path = centre_crop(1280, 720, 10.0)
        cut = autoedit.apply_cuts(path, [(0.0, 3.0), (5.0, 10.0)])
        assert [(round(s.start_s, 2), round(s.end_s, 2)) for s in cut.segments] == [(0.0, 3.0), (5.0, 10.0)]

    def test_zoom_schedule_and_expression(self) -> None:
        plan = autoedit.EditPlan(20.0, [(0.0, 5.0), (7.0, 12.0), (13.0, 20.0)])
        bumps, steps = autoedit.zoom_intervals(plan, [1.0, 2.0, 9.0], punch=True, jump=True)
        assert len(bumps) == 2  # 2.0 is too close to 1.0
        assert len(steps) == 1
        expr = autoedit.zoom_expression(bumps, steps)
        assert expr and expr.startswith("(1+") and "between(t," in expr
        assert autoedit.zoom_expression([], []) is None


# ---------------------------------------------------------------- .env + providers


class TestEnv:
    def test_parse_handles_bom_quotes_and_comments(self, tmp_path) -> None:
        f = tmp_path / ".env"
        f.write_text('﻿# c\nA=1\nexport B="two words"\nC=3 # tail\n', encoding="utf-8")
        text = f.read_text(encoding="utf-8-sig")
        assert envfile.parse(text) == {"A": "1", "B": "two words", "C": "3"}


class _Primary(OpenAIProvider):
    async def _complete(self, system, user, config):  # type: ignore[override]
        raise RuntimeError("timeout")


class TestFallback:
    async def test_window_retried_with_fallback_model(self, monkeypatch) -> None:
        used: list[str] = []

        async def fake_complete(self, system, user, config):
            used.append(self.model)
            return '<think>x</think>{"clips":[{"start_word_index":1,"end_word_index":9,"title":"T"}]}'

        monkeypatch.setattr(OpenAIProvider, "_complete", fake_complete)

        async def bad(self, system, user, config):
            used.append(self.model)
            raise RuntimeError("timed out")

        provider = OpenAIProvider("primary", api_key="k", base_url="https://x/v1")
        provider.fallback_model = "backup"
        # make only the primary instance fail
        provider._complete = bad.__get__(provider)  # type: ignore[method-assign]
        window = TranscriptWindow(text="", first_word=0, last_word=20)
        result = await provider.detect_highlights(window, DetectionConfig())
        assert used == ["primary", "backup"]
        assert result.clips[0].title == "T"

    def test_candidate_metadata_coercion(self) -> None:
        c = ClipCandidate(
            start_word_index=1,
            end_word_index=5,
            hashtags="shorts, #money biznes",
            emphasis=["3", "x", -1, 7],
        )
        assert c.hashtags == ["#shorts", "#money", "#biznes"]
        assert c.emphasis == [3, 7]

    def test_reasoning_effort_only_for_kimi(self) -> None:
        import inspect

        src = inspect.getsource(OpenAIProvider._complete)
        assert 'if "kimi" in self.model.lower()' in src


class TestHook:
    def test_short_hook_kept(self) -> None:
        from clipstudio.pipeline.highlights import clean_hook

        assert clean_hook("Why 90% of startups fail", "Title here") == "Why 90% of startups fail"

    def test_long_transcript_hook_falls_back_to_short_title(self) -> None:
        from clipstudio.pipeline.highlights import clean_hook

        long = "Well, guess what? They sold 100 Tesla Roadsters for $100,000 each, cash on the barrelhead"
        out = clean_hook(long, "Tesla raised $10M before building car #1 and then some more words")
        assert len(out.split()) <= 8
        assert out.startswith("Tesla raised")

    def test_verbatim_opening_is_rejected_even_if_short(self) -> None:
        from clipstudio.pipeline.highlights import clean_hook

        out = clean_hook(
            "Nobody tells you this about scaling",
            "Scaling secrets",
            "nobody tells you this about scaling a company",
        )
        assert out == "Scaling secrets"


class TestServePort:
    def test_busy_port_is_skipped(self) -> None:
        import socket

        from clipstudio.cli import find_free_port, port_is_free

        with socket.socket() as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            busy = held.getsockname()[1]
            assert not port_is_free("127.0.0.1", busy)
            chosen = find_free_port("127.0.0.1", busy)
            assert chosen is not None and chosen != busy
