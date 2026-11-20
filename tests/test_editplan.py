"""Edit plan validation/heuristics, split layout and render graph pieces."""

from __future__ import annotations

import asyncio

from clipstudio.pipeline import captions, editplan, export, highlights, layout
from clipstudio.pipeline.reframe.croppath import CropKeyframe, CropPath, CropSegment, Strategy
from clipstudio.pipeline.transcript import Word


def _words(n: int = 40) -> list[Word]:
    out = []
    for i in range(n):
        base = "".join(chr(97 + int(d)) for d in str(i))  # letters only: no digits
        text = "100" if i in (10, 25) else base + "." if i % 12 == 11 else base
        out.append(Word(text, i * 0.5, i * 0.5 + 0.4))
    return out


LIMITS = editplan.TemplateLimits(
    zoom_strength=1.12, zoom_min_gap_s=4.0, max_emoji=1, sfx_enabled=True
)


class TestValidate:
    def test_snaps_and_clamps(self) -> None:
        raw = {
            "hook_text": "one two three four five six seven eight nine ten",
            "zooms": [
                {"t": 5.1, "dur": 5.0, "scale": 1.9},  # snaps to 5.0, dur/scale clamped
                {"t": 6.0, "dur": 1.0, "scale": 1.1},  # inside the min gap: dropped
                {"t": 99, "dur": 1.0, "scale": 1.1},  # no word nearby: dropped
                {"t": "x"},  # junk
            ],
            "emoji": [{"t": 7.0, "char": "🔥"}, {"t": 12.0, "char": "🔥"}, {"t": 8, "char": "ab"}],
            "sfx": [
                {"t": 5.0, "kind": "whoosh"},
                {"t": 5.5, "kind": "whoosh"},
                {"t": 6, "kind": "boom"},
            ],
        }
        plan = editplan.validate_plan(raw, _words(), LIMITS, 20.0)
        assert [z["t"] for z in plan.zooms] == [5.0]
        assert plan.zooms[0]["scale"] == 1.12 and plan.zooms[0]["dur"] == 1.6
        assert len(plan.emoji) == 1  # capped at max_emoji
        assert [s["t"] for s in plan.sfx] == [5.0]  # min gap + unknown kind dropped
        assert len(plan.hook_text.split()) <= editplan.HOOK_MAX_WORDS

    def test_garbage_never_raises(self) -> None:
        assert editplan.validate_plan([1, 2], _words(), LIMITS, 20.0).zooms == []
        assert (
            editplan.validate_plan({"zooms": "no", "emoji": 3}, _words(), LIMITS, 20.0).emoji == []
        )

    def test_sfx_disabled_by_template(self) -> None:
        limits = editplan.TemplateLimits(sfx_enabled=False)
        raw = {"sfx": [{"t": 5.0, "kind": "pop"}]}
        assert editplan.validate_plan(raw, _words(), limits, 20.0).sfx == []


class TestHeuristic:
    def test_zooms_on_numbers_spaced(self) -> None:
        plan = editplan.heuristic_plan(_words(), 20.0, LIMITS, hook="Big claim")
        assert plan.source == "heuristic" and plan.hook_text == "Big claim"
        times = [z["t"] for z in plan.zooms]
        assert 5.0 in times and 12.5 in times  # the two "100" words
        assert all(b - a >= LIMITS.zoom_min_gap_s for a, b in zip(times, times[1:], strict=False))
        assert all(z["scale"] <= LIMITS.zoom_strength for z in plan.zooms)

    def test_falls_back_when_llm_fails(self) -> None:
        class Broken:
            async def _complete(self, *a, **k):
                raise RuntimeError("down")

        plan = asyncio.run(
            editplan.plan_clip(
                _words(), 20.0, LIMITS, provider=Broken(), hook="Hook", template_name="talk"
            )
        )
        assert plan.source == "heuristic" and plan.template == "talk"

    def test_uses_llm_reply(self) -> None:
        class Fake:
            async def _complete(self, *a, **k):
                return '{"hook_text": "Wow", "zooms": [{"t": 5.0, "dur": 1.2, "scale": 1.1}]}'

        plan = asyncio.run(editplan.plan_clip(_words(), 20.0, LIMITS, provider=Fake()))
        assert plan.source == "llm" and plan.hook_text == "Wow" and plan.zooms[0]["t"] == 5.0

    def test_roundtrip(self, tmp_path) -> None:
        plan = editplan.heuristic_plan(_words(), 20.0, LIMITS)
        path = plan.save(tmp_path / "p" / "plan.json")
        assert editplan.EditPlanData.load(path) == plan


def _path() -> CropPath:
    seg_fit = CropSegment(0, 5, 1920, 1080, [CropKeyframe(0, 0, 0)], Strategy.WIDE, fit=True)
    seg_track = CropSegment(5, 10, 606, 1080, [CropKeyframe(5, 700, 0)], Strategy.TRACK)
    return CropPath(
        1920, 1080, [seg_fit, seg_track], speaker={"cx": 960.0, "eye_y": 400.0, "w": 200.0}
    )


class TestLayout:
    def test_auto_splits_only_fit_segments(self) -> None:
        out = layout.resolve_layout(_path(), "auto", 720, 1280, "9:16")
        assert [s.layout for s in out.segments] == ["split", "speaker"]

    def test_speaker_mode_never_splits(self) -> None:
        out = layout.resolve_layout(_path(), "speaker", 720, 1280, "9:16")
        assert all(s.layout == "speaker" for s in out.segments)
        assert not out.segments[0].fit

    def test_split_mode_forces_split(self) -> None:
        out = layout.resolve_layout(_path(), "split", 720, 1280, "9:16")
        assert all(s.layout == "split" for s in out.segments)

    def test_face_crop_matches_panel_aspect_and_stays_inside(self) -> None:
        top, bot = layout.panel_heights(1280)
        assert top == 512 and bot == 768
        w, h, x, y = layout.face_crop(_path(), 720, bot)
        assert abs(w / h - 720 / 768) < 0.01
        assert x >= 0 and x + w <= 1920 and y >= 0 and y + h <= 1080

    def test_other_ratios_untouched(self) -> None:
        out = layout.resolve_layout(_path(), "split", 720, 720, "1:1")
        assert all(s.layout == "" for s in out.segments)

    def test_speaker_survives_json(self, tmp_path) -> None:
        loaded = CropPath.load(_path().save(tmp_path / "c.json"))
        assert loaded.speaker and loaded.speaker["cx"] == 960.0


class TestCaptionsAndHook:
    def test_captions_stay_out_of_bottom_safe_zone(self) -> None:
        for style in captions.PRESETS.values():
            subs = captions.build_ass(_words(6), style, width=720, height=1280)
            assert subs.styles[captions.STYLE_NAME].marginv >= 0.2 * 1280

    def test_hook_event_in_top_third_and_seam_captions(self) -> None:
        hook = captions.HookSpec(text="Why this matters", style="box")
        subs = captions.build_ass(
            _words(12),
            captions.PRESETS["yellow_pop"],
            width=720,
            height=1280,
            seam_intervals=[(0.0, 3.0)],
            hook=hook,
        )
        hook_events = [e for e in subs.events if e.style == captions.HOOK_STYLE_NAME]
        assert len(hook_events) == 1 and hook_events[0].end <= 4600
        st = subs.styles[captions.HOOK_STYLE_NAME]
        assert st.marginv < 1280 / 3
        seam = [e for e in subs.events if e.style == captions.SEAM_STYLE_NAME]
        assert seam and all("\\pos(360,512)" in e.text for e in seam)
        assert all(e.start >= 0 for e in subs.events)

    def test_plain_hook_duration_long_and_short_clip(self) -> None:
        hook = captions.HookSpec(text="Wait what is this")
        assert hook.style == "plain"
        for clip_s, expected in ((30.0, 2800), (5.0, 2800), (2.5, 2000)):
            subs = captions.build_ass(
                _words(6),
                captions.PRESETS["yellow_pop"],
                width=720,
                height=1280,
                hook=hook,
                clip_duration_s=clip_s,
            )
            ev = [e for e in subs.events if e.style == captions.HOOK_STYLE_NAME]
            assert len(ev) == 1 and abs(ev[0].end - expected) <= 5
            assert "\\c&H" in ev[0].text  # accent word


class TestFiltergraph:
    def test_split_chain_and_zoom_anchor(self) -> None:
        crop = layout.resolve_layout(_path(), "auto", 720, 1280, "9:16")
        req = export.ExportRequest(
            source=__import__("pathlib").Path("x.mp4"),
            destination=__import__("pathlib").Path("y.mp4"),
            start_s=0, end_s=10, crop_path=crop, words=[],
            style=captions.PRESETS["clean"],
        )  # fmt: skip
        graph = export.build_video_filtergraph(
            req, subtitle_name=None, zoom_expr="(1+0.1)", emoji_inputs=[(1, 2.0)]
        )
        assert "vstack=inputs=2" in graph and "overlay=x=" in graph
        assert "(ih-oh)*0.3" in graph and graph.endswith("[vout]")

    def test_sfx_mix_in_audio_graph(self) -> None:
        graph = export.build_audio_filtergraph(
            [(0, 10)], whole=True, normalize=True, loudness_lufs=-14, sfx=[(2, 1.5, 0.3)]
        )
        assert "adelay=1500:all=1" in graph and "amix=inputs=2" in graph and "[aout]" in graph


class TestHighlightDedupe:
    def test_threshold_merges_over_half_overlap(self) -> None:
        assert highlights.DEDUPE_IOU == 0.5
