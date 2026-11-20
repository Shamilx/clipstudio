"""Cold open (no dead air at the start) and the teaser flash-forward."""

from __future__ import annotations

import pytest
from clipstudio.config import EditSettings
from clipstudio.pipeline import coldopen, export, teaser
from clipstudio.pipeline.captions import PRESETS
from clipstudio.pipeline.placement import ZoneSegment
from clipstudio.pipeline.reframe.croppath import centre_crop
from clipstudio.pipeline.transcript import Word


def W(text: str, start: float, end: float) -> Word:
    return Word(text, start, end)


class TestColdOpen:
    def test_leading_dead_air_is_trimmed(self) -> None:
        words = [W("Hello", 1.4, 1.8), W("there.", 1.9, 2.3)]
        trim = coldopen.head_trim(words, clip_duration_s=30, min_duration_s=20)
        assert trim == pytest.approx(1.3, abs=0.01)  # first word lands 0.10 s in

    def test_already_tight_start_is_untouched(self) -> None:
        words = [W("Hello", 0.1, 0.4), W("there.", 0.5, 0.9)]
        assert coldopen.head_trim(words, clip_duration_s=30, min_duration_s=20) == 0.0

    def test_weak_opener_jumps_to_next_sentence(self) -> None:
        words = [W("So", 0.1, 0.3), W("yeah.", 0.4, 0.7), W("Listen", 1.2, 1.6), W("up.", 1.7, 2.0)]
        trim = coldopen.head_trim(words, clip_duration_s=30, min_duration_s=20)
        assert trim == pytest.approx(1.1, abs=0.01)

    def test_weak_opener_not_skipped_when_sentence_is_far(self) -> None:
        words = [
            W("So", 0.1, 0.3), W("this", 0.4, 0.7), W("is", 0.8, 1.0), W("long", 1.1, 4.5),
            W("Next", 4.6, 5.0),
        ]  # fmt: skip
        assert coldopen.head_trim(words, clip_duration_s=30, min_duration_s=20) == 0.0

    def test_user_trimmed_never_moves(self) -> None:
        words = [W("Hello", 2.0, 2.4)]
        assert coldopen.head_trim(words, clip_duration_s=30, user_trimmed=True) == 0.0

    def test_never_drops_below_minimum_duration(self) -> None:
        words = [W("Hello", 3.0, 3.4)]
        trim = coldopen.head_trim(words, clip_duration_s=21.0, min_duration_s=20.0)
        assert trim == pytest.approx(1.0, abs=0.01)
        assert coldopen.head_trim(words, clip_duration_s=20.0, min_duration_s=20.0) == 0.0

    def test_silence_map_corrects_early_word_timing(self) -> None:
        words = [W("Hello", 0.5, 1.2)]
        # the measured silence runs to 0.8: the word really starts there
        trim = coldopen.head_trim(
            words, clip_duration_s=30, min_duration_s=20, silences=[(0.0, 0.8)]
        )
        assert trim == pytest.approx(0.7, abs=0.01)

    def test_keep_start_not_trimmed_twice(self) -> None:
        words = [W("Hello", 1.4, 1.8)]
        assert coldopen.head_trim(words, clip_duration_s=30, keep_start=1.26) == 0.0

    def test_trim_keep_head_only_moves_the_start(self) -> None:
        keep = [(0.0, 5.0), (6.0, 20.0)]
        assert coldopen.trim_keep_head(keep, 1.0) == [(1.0, 5.0), (6.0, 20.0)]
        assert coldopen.trim_keep_head(keep, 5.5) == [(6.0, 20.0)]

    def test_make_plan_trims_head_and_keeps_end(self, tmp_path) -> None:
        words = [W("Hello", 5.0 + 1.5, 5.0 + 1.9), W("world.", 5.0 + 2.0, 5.0 + 2.4)]
        req = _request(tmp_path, words, min_duration_s=20.0)
        plan = export.make_plan(req, EditSettings(trim_silence=False, remove_fillers=False))
        assert plan.plan.keep[0][0] == pytest.approx(1.4, abs=0.01)
        assert plan.plan.keep[-1][1] == pytest.approx(req.duration_s)
        assert plan.cold_open_trim_s == pytest.approx(1.4, abs=0.01)
        assert plan.words[0].start == pytest.approx(0.1, abs=0.02)  # captions follow
        trimmed = export.make_plan(
            _request(tmp_path, words, min_duration_s=20.0, user_trimmed=True),
            EditSettings(trim_silence=False, remove_fillers=False),
        )
        assert trimmed.cold_open_trim_s == 0.0 and trimmed.plan.is_noop


def _request(tmp_path, words, **kw) -> export.ExportRequest:
    return export.ExportRequest(
        source=tmp_path / "s.mp4", destination=tmp_path / "o.mp4", start_s=5.0, end_s=35.0,
        crop_path=centre_crop(1280, 720, 30.0), words=words, style=PRESETS["yellow_pop"], **kw,
    )  # fmt: skip


def _talk(total: float = 30.0) -> list[Word]:
    return [W(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(int(total / 0.5))]


class TestTeaserSelection:
    def test_prefers_emphasis_in_last_60_percent(self) -> None:
        t = teaser.select_teaser(_talk(), 30.0, emphasis=[21.0])
        assert t is not None and t.start <= 21.0 <= t.end
        assert teaser.TEASER_MIN_S - 1e-6 <= t.duration_s <= teaser.TEASER_MAX_S + 1e-6

    def test_zoom_anchor_is_used_when_no_emphasis(self) -> None:
        t = teaser.select_teaser(_talk(), 30.0, zooms=[15.0])
        assert t is not None and t.start <= 15.0 <= t.end

    def test_falls_back_to_densest_window_in_last_60_percent(self) -> None:
        words = [W(f"a{i}", i * 0.5, i * 0.5 + 0.4) for i in range(60)]  # sparse
        dense = [W(f"d{i}", 20.0 + i * 0.15, 20.0 + i * 0.15 + 0.12) for i in range(10)]
        words = [w for w in words if not 19.8 < w.start < 21.8] + dense
        t = teaser.select_teaser(sorted(words, key=lambda w: w.start), 30.0)
        assert t is not None and 19.9 <= t.start <= 21.0
        assert t.start >= 30.0 * teaser.SEARCH_FROM

    def test_respects_max_total_by_shortening_or_skipping(self) -> None:
        t = teaser.select_teaser(_talk(), 30.0, emphasis=[21.0], max_total_s=31.2)
        assert t is not None and t.duration_s <= 1.2 + 1e-6
        assert teaser.select_teaser(_talk(), 30.0, emphasis=[21.0], max_total_s=30.5) is None

    def test_too_short_clip_skips(self) -> None:
        assert teaser.select_teaser(_talk(4.0), 4.0) is None


class TestTeaserTimeline:
    def test_words_show_teaser_words_then_shifted_story(self) -> None:
        words = [W("a", 1.0, 1.4), W("b", 10.0, 10.4), W("c", 10.5, 10.9), W("d", 20.0, 20.4)]
        t = teaser.Teaser(10.0, 11.2)
        out, flags = teaser.shift_words(words, {2}, t)
        assert [w.text for w in out] == ["b", "c", "a", "b", "c", "d"]
        assert out[0].start == pytest.approx(0.0) and out[1].end == pytest.approx(0.9)
        assert out[2].start == pytest.approx(1.0 + 1.2)  # story starts after the teaser
        assert out[5].start == pytest.approx(21.2)
        assert flags == {1, 4}  # keyword flag follows both copies of "c"

    def test_intervals_and_zones_shift(self) -> None:
        t = teaser.Teaser(10.0, 11.0)
        assert teaser.shift_intervals([(9.0, 12.0)], t) == [(0.0, 1.0), (10.0, 13.0)]
        zone = ZoneSegment(0.0, 30.0, faces=[(10.5, 1, 2, 3, 4), (2.0, 1, 2, 3, 4)])
        out = teaser.shift_zones([zone], t)
        assert (out[0].start, out[0].end) == (0.0, 1.0) and out[0].faces == [(0.5, 1, 2, 3, 4)]
        assert (out[1].start, out[1].end) == (1.0, 31.0)
        assert out[1].faces[1][0] == pytest.approx(3.0)

    def test_make_plan_shifts_everything_and_total_grows(self, tmp_path) -> None:
        words = _talk(30.0)
        req = _request(tmp_path, [W(w.text, w.start + 5.0, w.end + 5.0) for w in words])
        edit = EditSettings(trim_silence=False, remove_fillers=False, teaser=True)
        plan = export.make_plan(req, edit)
        assert plan.teaser is not None
        assert plan.duration_s == pytest.approx(30.0 + plan.teaser.duration_s)
        assert plan.zones[0].start == 0.0 and plan.zones[-1].end == pytest.approx(plan.duration_s)
        # teaser words first, all inside the teaser; story words after
        head = [w for w in plan.words if w.end <= plan.shift_s + 1e-6]
        assert head and head[0].start >= 0.0
        off = export.make_plan(req, EditSettings(trim_silence=False, remove_fillers=False))
        assert off.teaser is None and off.duration_s == pytest.approx(30.0)

    def test_filtergraphs_contain_teaser_segment_with_fades(self, tmp_path) -> None:
        t = teaser.Teaser(10.0, 11.2)
        req = _request(tmp_path, [])
        graph = export.build_video_filtergraph(req, subtitle_name=None, teaser=t)
        assert "trim=start=10.0000:end=11.2000" in graph and "color=white" in graph
        assert "concat=n=2:v=1:a=0[vtz]" in graph and graph.endswith("[vout]")
        audio = export.build_audio_filtergraph(
            [(0.0, 30.0)], whole=True, normalize=False, loudness_lufs=-14, teaser=t
        )
        assert "atrim=start=10.0000:end=11.2000" in audio and "afade=t=in" in audio
        assert "concat=n=2:v=0:a=1[ateaser]" in audio and audio.endswith("[aout]")
