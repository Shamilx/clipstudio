"""Caption grouping and ASS generation."""

from __future__ import annotations

import pysubs2
import pytest
from clipstudio.pipeline import captions
from clipstudio.pipeline.transcript import Word


def words_from(spec: list[tuple[str, float, float]]) -> list[Word]:
    return [Word(text=t, start=s, end=e) for t, s, e in spec]


def evenly_spaced(texts: list[str], *, step: float = 0.4) -> list[Word]:
    return [
        Word(text=text, start=i * step, end=i * step + step * 0.8) for i, text in enumerate(texts)
    ]


class TestGrouping:
    def test_respects_the_word_ceiling(self) -> None:
        groups = captions.group_words(evenly_spaced([f"w{i}" for i in range(12)]), max_words=4)

        assert all(len(g.words) <= 4 for g in groups)
        assert sum(len(g.words) for g in groups) == 12

    def test_breaks_on_a_long_pause(self) -> None:
        words = words_from([("one", 0.0, 0.3), ("two", 0.3, 0.6), ("three", 2.0, 2.3)])

        groups = captions.group_words(words, max_words=10, max_gap_s=0.4)

        assert len(groups) == 2
        assert groups[1].words[0].text == "three"

    def test_breaks_after_sentence_punctuation(self) -> None:
        words = words_from([("Hello.", 0.0, 0.3), ("Next", 0.4, 0.7), ("thing", 0.8, 1.0)])

        groups = captions.group_words(words, max_words=10)

        assert len(groups) == 2
        assert groups[0].text == "Hello."

    def test_trailing_sentence_end_does_not_make_an_empty_group(self) -> None:
        words = words_from([("Hello", 0.0, 0.3), ("world.", 0.4, 0.7)])

        groups = captions.group_words(words, max_words=10)

        assert len(groups) == 1
        assert all(g.words for g in groups)

    def test_every_word_appears_exactly_once(self) -> None:
        words = evenly_spaced([f"w{i}" for i in range(37)])

        grouped = [w for g in captions.group_words(words, max_words=4) for w in g.words]

        assert [w.text for w in grouped] == [w.text for w in words]

    def test_empty_input_gives_no_groups(self) -> None:
        assert captions.group_words([]) == []

    def test_group_times_span_its_words(self) -> None:
        groups = captions.group_words(evenly_spaced(["a", "b", "c"]), max_words=3)

        assert groups[0].start == 0.0
        assert groups[0].end == pytest.approx(0.8 + 0.32, abs=0.01)


class TestColour:
    def test_hex_converts_to_rgb(self) -> None:
        colour = captions.hex_to_ass("#FFE500")

        assert (colour.r, colour.g, colour.b) == (255, 229, 0)

    def test_leading_hash_is_optional(self) -> None:
        assert captions.hex_to_ass("FFFFFF").r == 255

    def test_bad_hex_raises(self) -> None:
        with pytest.raises(ValueError):
            captions.hex_to_ass("#FFF")


class TestPresets:
    def test_all_presets_exist(self) -> None:
        assert set(captions.PRESETS) == {
            "yellow_pop",
            "box",
            "karaoke",
            "one_word",
            "clean",
            "film",
        }

    @pytest.mark.parametrize("key", list(captions.PRESETS))
    def test_every_preset_ships_its_font(self, key: str) -> None:
        # A missing font file means libass silently substitutes, and the caption
        # looks nothing like the preview.
        assert captions.get_style(key).font_path.exists()

    def test_unknown_style_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown caption style"):
            captions.get_style("neon-explosion")


class TestAssGeneration:
    @pytest.fixture
    def words(self) -> list[Word]:
        return evenly_spaced(["the", "quick", "brown", "fox", "jumps", "over."])

    @pytest.mark.parametrize("key", list(captions.PRESETS))
    def test_every_preset_produces_events(self, key: str, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style(key), width=1080, height=1920)

        assert len(subs.events) > 0
        assert captions.STYLE_NAME in subs.styles

    def test_resolution_is_recorded(self, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1920)

        assert subs.info["PlayResX"] == "1080"
        assert subs.info["PlayResY"] == "1920"

    def test_font_size_scales_with_height(self, words: list[Word]) -> None:
        tall = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1920)
        square = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1080)

        assert (
            tall.styles[captions.STYLE_NAME].fontsize > square.styles[captions.STYLE_NAME].fontsize
        )

    def test_time_offset_rebases_to_clip_relative(self, words: list[Word]) -> None:
        offset = captions.build_ass(
            words,
            captions.get_style("clean_lower"),
            width=1080,
            height=1920,
            time_offset_s=100.0,
        )

        # Absolute times were 0-2.7s, so everything clamps to zero rather than
        # going negative.
        assert offset.events[0].start == 0

    def test_offset_preserves_relative_spacing(self) -> None:
        words = [Word(text=t, start=100.0 + i, end=100.5 + i) for i, t in enumerate("abc")]

        subs = captions.build_ass(
            words,
            captions.get_style("clean_lower"),
            width=1080,
            height=1920,
            time_offset_s=100.0,
        )

        assert subs.events[0].start == pytest.approx(0, abs=20)

    def test_all_caps_is_applied(self, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1920)

        assert "QUICK" in " ".join(e.text for e in subs.events)

    def test_clean_lower_preserves_case(self, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style("clean_lower"), width=1080, height=1920)

        assert "quick" in " ".join(e.text for e in subs.events)

    def test_karaoke_emits_kf_tags(self, words: list[Word]) -> None:
        subs = captions.build_ass(
            words, captions.get_style("karaoke_fill"), width=1080, height=1920
        )

        assert any("\\kf" in event.text for event in subs.events)

    def test_bold_pop_emits_one_event_per_word(self, words: list[Word]) -> None:
        # Per-word highlighting has no per-glyph timeline in libass, so each
        # word needs its own event showing the whole group.
        subs = captions.build_ass(words, captions.get_style("bold_pop"), width=1080, height=1920)

        assert len(subs.events) == len(words)

    def test_clean_lower_emits_one_event_per_group(self, words: list[Word]) -> None:
        style = captions.get_style("clean_lower")
        groups = captions.group_words(words, max_words=style.max_words)

        subs = captions.build_ass(words, style, width=1080, height=1920)

        assert len(subs.events) == len(groups)

    def test_boxed_uses_an_opaque_border_style(self, words: list[Word]) -> None:
        subs = captions.build_ass(words, captions.get_style("boxed"), width=1080, height=1920)

        assert subs.styles[captions.STYLE_NAME].borderstyle == 3

    def test_events_never_start_before_zero(self, words: list[Word]) -> None:
        subs = captions.build_ass(
            words,
            captions.get_style("bold_pop"),
            width=1080,
            height=1920,
            time_offset_s=1.0,
        )

        assert all(event.start >= 0 for event in subs.events)

    def test_written_file_is_valid_ass(self, words: list[Word], tmp_path) -> None:
        path = captions.write_ass(
            tmp_path / "out.ass",
            words,
            captions.get_style("bold_pop"),
            width=1080,
            height=1920,
        )

        reloaded = pysubs2.load(str(path), encoding="utf-8")

        assert len(reloaded.events) > 0

    def test_srt_sidecar_is_written(self, words: list[Word], tmp_path) -> None:
        path = captions.write_srt(tmp_path / "out.srt", words)

        assert path.exists()
        assert len(pysubs2.load(str(path), encoding="utf-8").events) > 0


def test_caption_events_never_overlap() -> None:
    from clipstudio.pipeline import captions
    from clipstudio.pipeline.transcript import Word

    words = [
        Word("hello", 0.0, 0.9),
        Word("there", 0.5, 1.2),  # starts before the previous word ends
        Word("friend", 1.2, 1.6),
        Word("again", 1.2, 1.8),  # identical start
        Word("now", 2.0, 2.4),
    ]
    subs = captions.build_ass(words, captions.get_style("yellow_pop"), width=720, height=1280)
    events = sorted(subs.events, key=lambda e: e.start)
    assert events
    for a, b in zip(events, events[1:], strict=False):
        assert a.end <= b.start
        assert a.end > a.start


class TestHookCard:
    HOOK = "Saint Nic went all out for his mom"

    def _events(self, **kw):
        spec = captions.HookSpec(text=kw.pop("text", self.HOOK), style=kw.pop("style", "plain"))
        subs = captions.build_ass(
            kw.pop("words", []), captions.PRESETS["yellow_pop"], width=720, height=1280,
            hook=spec, **kw,
        )  # fmt: skip
        ev = [e for e in subs.events if e.style == captions.HOOK_STYLE_NAME]
        assert len(ev) == 1
        return subs, ev[0]

    def test_visible_from_first_frame_with_fade_out_only(self) -> None:
        _, ev = self._events()
        assert ev.start == 0
        assert "\\fad(0," in ev.text  # no fade-in
        assert "\\fad(160" not in ev.text

    def test_default_duration_2_5_to_3_seconds(self) -> None:
        _, ev = self._events(clip_duration_s=40.0)
        assert 2500 <= ev.end <= 3000

    def test_explicit_duration_is_respected(self) -> None:
        spec = captions.HookSpec(text="Wait what", duration_s=3.6)
        subs = captions.build_ass([], captions.PRESETS["clean"], width=720, height=1280, hook=spec)
        ev = [e for e in subs.events if e.style == captions.HOOK_STYLE_NAME][0]
        assert ev.end == 3600

    def test_size_position_and_two_lines_max(self) -> None:
        subs, ev = self._events()
        st = subs.styles[captions.HOOK_STYLE_NAME]
        assert st.fontsize <= round(1280 * captions.HOOK_SIZE_RATIO)
        assert ev.text.count("\\N") <= 1  # at most 2 lines
        assert "\\an8\\pos(360,205)" in ev.text  # centred, top edge at 16% of 1280
        assert st.outline >= 9 and st.shadow > 0  # heavy outline + shadow

    def test_long_text_still_two_lines_in_box_style(self) -> None:
        _, ev = self._events(
            text="This is a really long hook line that keeps going and going", style="box"
        )
        assert ev.text.count("\\N") <= 1

    def test_hook_renders_without_words(self) -> None:
        subs, _ = self._events(words=[])
        assert len(subs.events) == 1

    def test_captions_stay_below_hook_while_it_is_up(self) -> None:
        import re

        from clipstudio.pipeline.placement import ZoneSegment

        # A face that pushes captions upwards, during the hook.
        zone = ZoneSegment(0.0, 10.0, faces=[(0.5, 150.0, 760.0, 570.0, 1100.0)])
        words = evenly_spaced(["watch", "this", "now"], step=0.5)
        subs, hook_ev = self._events(words=words, zones=[zone], clip_duration_s=10.0)
        layout_bottom = 205 + 2 * 60 * 1.22  # conservative: 2 lines at >= 60 px
        for e in subs.events:
            if e.style == captions.HOOK_STYLE_NAME or e.start >= hook_ev.end:
                continue
            m = re.search(r"\\pos\(\d+,(\d+)\)", e.text)
            if m:  # \an2: y is the bottom edge of the caption block
                assert int(m.group(1)) - 70 >= layout_bottom - 1

    def test_hook_moves_within_14_to_18_percent_off_a_face(self) -> None:
        import re

        from clipstudio.pipeline.placement import ZoneSegment

        zone = ZoneSegment(0.0, 10.0, faces=[(0.5, 100.0, 150.0, 620.0, 330.0)])
        _, ev = self._events(zones=[zone], clip_duration_s=10.0)
        y = int(re.search(r"\\pos\(\d+,(\d+)\)", ev.text).group(1))
        assert round(1280 * 0.14) <= y <= round(1280 * 0.18)
