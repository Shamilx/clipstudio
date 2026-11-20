"""Content profiles, letterbox, film captions, hooks, brief guarantees, names and self-QA."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
from clipstudio import campaign, profiles
from clipstudio.pipeline import captions, hooks, letterbox, qa, vocabulary
from clipstudio.pipeline.placement import ZoneSegment, build_zones
from clipstudio.pipeline.reframe.croppath import CropKeyframe, CropPath, CropSegment
from clipstudio.pipeline.transcript import Word

TODAY = date(2026, 11, 19)


def words_from(
    text: str, step: float = 0.3, gap_after: dict[int, float] | None = None
) -> list[Word]:
    out, t = [], 0.0
    for i, w in enumerate(text.split()):
        out.append(Word(w, t, t + step * 0.9))
        t += step + (gap_after or {}).get(i, 0.0)
    return out


# -- profiles ---------------------------------------------------------------


class TestProfiles:
    def test_all_builtin_profiles_load(self) -> None:
        assert {p.name for p in profiles.list_profiles()} >= {"film", "talking", "event", "gaming"}

    def test_film_profile_look(self) -> None:
        film = profiles.load_profile("film")
        assert film.caption_preset == "film" and not film.progress_bar and film.max_emoji == 0
        assert not film.sfx and not film.trim_silence and not film.punch_zoom

    def test_brief_footage_type_wins(self) -> None:
        pick = profiles.choose_profile(
            footage_type="film", has_brief=True, words_per_min=200, letterbox=False, cuts_per_min=0
        )
        assert pick == "film"
        assert profiles.choose_profile(footage_type="gaming", has_brief=True) == "gaming"

    def test_brief_other_falls_back_to_detection(self) -> None:
        pick = profiles.choose_profile(
            footage_type="other", has_brief=True, words_per_min=60, letterbox=True
        )
        assert pick == "film"

    def test_no_brief_ignores_footage_type(self) -> None:
        assert profiles.choose_profile(footage_type="film", has_brief=False) == "talking"

    def test_autodetect(self) -> None:
        assert profiles.detect_profile(words_per_min=150, letterbox=True) == "film"
        assert profiles.detect_profile(words_per_min=80, cuts_per_min=12) == "film"
        assert profiles.detect_profile(words_per_min=160, cuts_per_min=2) == "talking"
        assert profiles.detect_profile(words_per_min=160, cuts_per_min=15) == "talking"
        assert profiles.detect_profile(words_per_min=20, cuts_per_min=6) == "event"
        assert profiles.detect_profile() == "talking"

    def test_apply_to_edit_respects_explicit(self) -> None:
        from clipstudio.config import Settings

        settings = Settings()
        settings.edit.punch_zoom = True
        profiles.apply_to_edit(profiles.load_profile("film"), settings.edit, {"punch_zoom"})
        assert settings.edit.punch_zoom and not settings.edit.trim_silence

    def test_apply_to_template(self) -> None:
        from clipstudio.templates import load_template

        t = profiles.apply_to_template(profiles.load_profile("film"), load_template("hype"))
        assert t.caption_preset == "film" and t.max_emoji == 0 and not t.progress_bar
        assert not t.sfx_enabled and t.hook_duration_s == 3.0


# -- letterbox --------------------------------------------------------------


class TestLetterbox:
    def test_parse_cropdetect_takes_last(self) -> None:
        err = (
            "[Parsed_cropdetect_1 @ 0x1] x1:0 x2:639 y1:60 y2:299 w:640 h:240 x:0 y:60 pts:0 crop=640:240:0:60\n"
            "[Parsed_cropdetect_1 @ 0x1] x1:0 x2:639 y1:62 y2:297 w:640 h:236 x:0 y:62 pts:1 crop=640:236:0:62\n"
        )
        assert letterbox.parse_cropdetect(err) == (0, 62, 640, 236)
        assert letterbox.parse_cropdetect("nothing") is None

    def test_median_ignores_dark_scene_outliers(self) -> None:
        good = (0, 138, 1920, 804)
        samples = [good] * 15 + [(0, 400, 1920, 100), (0, 0, 1920, 1080), (300, 200, 800, 300)]
        assert letterbox.median_area(samples, 1920, 1080, inset=0) == (0, 138, 1920, 804)

    def test_inset_shaves_bars_only(self) -> None:
        samples = [(0, 138, 1920, 804)] * 10
        x, y, w, h = letterbox.median_area(samples, 1920, 1080, inset=2)
        assert (x, w) == (0, 1920) and y == 140 and h == 800

    def test_small_bars_ignored(self) -> None:
        assert letterbox.median_area([(0, 10, 1920, 1060)] * 10, 1920, 1080) is None

    def test_too_few_samples(self) -> None:
        assert letterbox.median_area([(0, 138, 1920, 804)] * 2, 1920, 1080) is None

    def test_pillarbox(self) -> None:
        area = letterbox.median_area([(240, 0, 1440, 1080)] * 10, 1920, 1080, inset=0)
        assert area == (240, 0, 1440, 1080)


# -- film captions ----------------------------------------------------------


def _film_ass(words, zones=None, **kw):
    style = captions.get_style("film")
    return captions.build_ass(words, style, width=720, height=1280, zones=zones, **kw)


class TestFilmCaptions:
    def test_style_flags(self) -> None:
        s = captions.get_style("film")
        assert not s.all_caps and not s.boxed and s.accent is None and s.animation == "none"
        assert s.grouping == "phrase" and s.min_on_screen_s >= 0.8
        assert abs(s.size_ratio - 0.043) < 1e-6

    def test_phrases_split_on_punctuation_and_pause(self) -> None:
        words = words_from(
            "I never wanted this fight. But here we are, standing in the dust and the wind now",
            gap_after={13: 0.6},
        )
        groups = captions.group_phrases(words, max_chars_per_line=34, max_gap_s=0.35)
        texts = [g.text for g in groups]
        assert texts[0] == "I never wanted this fight."
        assert all(len(t) <= 68 for t in texts)
        assert len(texts) >= 3  # sentence end + pause split

    def test_max_two_lines_and_balanced(self) -> None:
        lines = captions.balanced_lines(
            ["We", "ride", "at", "dawn", "and", "nobody", "follows", "us", "home"], 34
        )
        assert len(lines) == 2

    def test_build_ass_sentence_case_no_box_no_colour(self) -> None:
        words = words_from("The war is over. Quanah Parker rides home tonight.")
        subs = _film_ass(words)
        body = " ".join(e.text for e in subs.events)
        assert "The war is over." in body
        assert "THE WAR" not in body
        assert "\\c&H" not in body and "\\fscx" not in body and "\\kf" not in body
        style = subs.styles[captions.STYLE_NAME]
        assert style.borderstyle == 1  # outline + shadow, no opaque box
        assert style.shadow > 0 and style.fontsize == round(1280 * 0.043)

    def test_min_on_screen(self) -> None:
        words = [Word("No.", 0.0, 0.25), Word("Never", 5.0, 5.4), Word("again.", 5.4, 5.8)]
        first = _film_ass(words).events[0]
        assert first.end - first.start >= 800

    def test_two_line_limit_in_events(self) -> None:
        words = words_from(
            "a " * 5 + "long sentence that keeps going and going without any stop at all"
        )
        for e in _film_ass(words).events:
            assert e.text.count("\\N") <= 1


# -- caption placement ------------------------------------------------------


def _pos_y(text: str) -> int:
    return int(text.split("\\pos(")[1].split(")")[0].split(",")[1])


class TestPlacement:
    def test_moves_off_face(self) -> None:
        words = words_from("Stand where you are and listen to me.")
        base = _film_ass(words)
        assert "\\pos(" not in base.events[0].text
        face = (1.0, 220.0, 790.0, 500.0, 1010.0)  # sits on the default caption zone
        zones = [ZoneSegment(0, 10, "speaker", faces=[face])]
        text = _film_ass(words, zones=zones).events[0].text
        assert "\\pos(" in text and "\\an2" in text
        y = _pos_y(text)
        # the caption block (about 70 px tall) must be clear of the face rectangle
        assert y <= 790 or y - 70 >= 1010

    def test_unchanged_without_overlap(self) -> None:
        words = words_from("Stand where you are and listen to me.")
        zones = [ZoneSegment(0, 10, "speaker", faces=[(1.0, 250.0, 100.0, 470.0, 300.0)])]
        assert "\\pos(" not in _film_ass(words, zones=zones).events[0].text

    def test_fit_layout_uses_lower_band_below_picture(self) -> None:
        words = words_from("Stand where you are and listen to me.")
        zones = [ZoneSegment(0, 10, "fit", picture=(489.0, 790.0))]
        y = _pos_y(_film_ass(words, zones=zones).events[0].text)
        assert 790 < y < 1280 * 0.8  # below the picture, above the platform UI

    def test_build_zones_fit_picture_and_faces(self) -> None:
        seg = CropSegment(0.0, 4.0, 1920, 804, [CropKeyframe(0.0, 0, 0)], fit=True)
        crop = CropPath(
            1920, 804, [seg], faces=[{"t": 1.0, "x0": 900, "y0": 300, "x1": 1000, "y1": 420}]
        )
        zones = build_zones(crop, 720, 1280)
        assert zones[0].kind == "fit"
        y0, y1 = zones[0].picture
        assert abs((y1 - y0) - 804 * 720 / 1920) < 1
        assert len(zones[0].faces) == 1

    def test_crop_path_roundtrip_keeps_active_and_faces(self, tmp_path) -> None:
        seg = CropSegment(0.0, 4.0, 900, 1600, [CropKeyframe(0.0, 0, 0)])
        crop = CropPath(1920, 804, [seg], active=(0, 138, 1920, 804), faces=[{"t": 1.0}])
        crop.save(tmp_path / "c.json")
        loaded = CropPath.load(tmp_path / "c.json")
        assert loaded.active == (0, 138, 1920, 804) and loaded.faces == [{"t": 1.0}]


# -- hooks ------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def _complete(self, system, user, config):
        return self.reply


class TestHooks:
    def test_generic_hooks_rejected(self) -> None:
        for h in (
            "Ready for a wild twist?",
            "Can you believe this?",
            "You won't believe this",
            "Wait for it",
            "Watch till the end",
            "This is crazy",
            "Wow",
            "",
            "What just happened?",
        ):
            assert hooks.is_generic_hook(h), h

    def test_specific_hooks_pass(self) -> None:
        for h in (
            "The sheriff draws first and misses",
            "This mistake cost me $40,000",
            "Quanah Parker rides in",
        ):
            assert not hooks.is_generic_hook(h), h

    def test_long_hooks_are_rejected_not_clamped(self) -> None:
        assert hooks.clean_hook("one two three four five six seven eight nine ten") == ""

    def test_ensure_regenerates_then_falls_back(self) -> None:
        import asyncio

        good = _FakeProvider('{"hook": "Parker spares the captured scout"}')
        hook, how = asyncio.run(
            hooks.ensure_specific_hook(
                "Can you believe this?", provider=good, title="Scout", transcript="x"
            )
        )
        assert (hook, how) == ("Parker spares the captured scout", "regenerated")
        bad = _FakeProvider('{"hook": "This is crazy"}')
        hook, how = asyncio.run(
            hooks.ensure_specific_hook(
                "Ready for a wild twist?", provider=bad, title="The Ambush at Dawn", transcript="x"
            )
        )
        assert how == "title" and hook == "The Ambush at Dawn"
        hook, how = asyncio.run(
            hooks.ensure_specific_hook(
                "Parker spares the scout", provider=None, title="t", transcript=""
            )
        )
        assert how == "kept"


# -- brief guarantees -------------------------------------------------------


class TestBriefGuarantees:
    def test_preorder_dropped_when_released_warning(self) -> None:
        out = campaign.sanitize_caption_text(
            ["Pre-order now on Apple TV", "Streaming on Max"],
            ["Brief says pre-order but the film is already out"],
            TODAY,
        )
        assert out == ["Streaming on Max"]

    def test_past_date_drops_coming_soon(self) -> None:
        assert campaign.sanitize_caption_text(["Coming soon November 17"], [], TODAY) == []

    def test_future_date_kept(self) -> None:
        assert campaign.sanitize_caption_text(["Pre-order now, out December 5"], [], TODAY) == [
            "Pre-order now, out December 5"
        ]

    def test_coming_to_past_date_rewritten(self) -> None:
        out = campaign.sanitize_caption_text(["Coming to digital November 17"], [], TODAY)
        assert out == ["Out now on digital"]

    def test_iso_and_day_first_dates(self) -> None:
        assert campaign.sanitize_caption_text(["Pre-order, out 2026-11-01"], [], TODAY) == []
        assert campaign.sanitize_caption_text(["Pre-order, out 5 November"], [], TODAY) == []

    def test_hashtag_cleanup(self) -> None:
        tags = campaign.clean_hashtags(
            ["#Brand", "#brand", "#Film"],
            [
                "#ab",
                "#butt",
                "#Film",
                "#western",
                "#western",
                "#12345",
                "#cinema",
                "#history",
                "#epic",
            ],
            max_topical=3,
        )
        assert tags == ["#Brand", "#Film", "#western", "#cinema", "#history"]

    def test_merge_and_validate(self) -> None:
        rules = campaign.CampaignRules(
            required_hashtags=["#Movie"], required_caption_text=["Out now on digital"]
        )
        desc, tags = campaign.merge_metadata(
            rules, "Great scene.", ["#shorts", "#x"], max_topical=3
        )
        assert "Out now on digital" in desc and tags[0] == "#Movie"
        assert campaign.validate_metadata(rules, desc, tags) == []
        assert len(campaign.validate_metadata(rules, "nothing", [])) == 2

    def test_enforce_fixes_clip(self) -> None:
        rules = campaign.CampaignRules(
            required_hashtags=["#Movie"], required_caption_text=["Out now"]
        )
        clip = SimpleNamespace(description="Hi", hashtags=["#a1b"])
        campaign.enforce_metadata(rules, clip)
        assert campaign.validate_metadata(rules, clip.description, clip.hashtags) == []

    def test_extract_rules_sanitizes_llm_output(self) -> None:
        import asyncio
        import json

        reply = json.dumps(
            {
                "required_hashtags": ["#Film"],
                "required_caption_text": ["Pre-order now on Apple TV", "Out on digital"],
                "footage_type": "film",
                "warnings": ["Film is already out"],
                "vocabulary": ["Quanah Parker"],
            }
        )
        rules = asyncio.run(campaign.extract_rules("some brief", TODAY, _FakeProvider(reply)))
        assert rules.required_caption_text == ["Out on digital"]
        assert rules.vocabulary == ["Quanah Parker"] and rules.footage_type == "film"


# -- vocabulary -------------------------------------------------------------


class TestVocabulary:
    def test_fuzzy_corrects_misheard_name(self) -> None:
        words = words_from("They call him Quanta Parker, the chief.")
        assert vocabulary.correct_words(words, ["Quanah Parker"]) == 1
        assert " ".join(w.text for w in words) == "They call him Quanah Parker, the chief."

    def test_single_token_and_first_letter_rule(self) -> None:
        words = words_from("Quanta rides. Barker waits.")
        vocabulary.correct_words(words, ["Quanah Parker"])
        assert words[0].text == "Quanah"
        assert words[2].text == "Barker"  # different first letter: untouched

    def test_does_not_touch_unrelated_words(self) -> None:
        words = words_from("The sun went down over the plains.")
        assert vocabulary.correct_words(words, ["Quanah Parker", "Texas"]) == 0

    def test_whisper_prompt_cap(self) -> None:
        out = vocabulary.whisper_prompt([f"Name{i} Surname{i}" for i in range(200)])
        assert 0 < len(out) <= vocabulary.PROMPT_MAX_CHARS

    def test_names_from_descriptions(self) -> None:
        names = vocabulary.names_from_descriptions(
            [
                "A rider on a horse. A caption reads 'QUANAH PARKER'.",
                "Close shot of Ryan Gosling smiling",
            ]
        )
        assert any("Quanah" in n for n in names) and "Ryan Gosling" in names


# -- QA ---------------------------------------------------------------------


class TestQA:
    def _frame(self, black_rows: int = 0, h: int = 640, w: int = 360) -> np.ndarray:
        rng = np.random.default_rng(1)
        img = rng.integers(60, 230, size=(h, w), dtype=np.uint8)
        if black_rows:
            img[:black_rows] = 12
            img[-black_rows:] = 12
        return img

    def test_detects_black_bands(self) -> None:
        rows, cols = qa.detect_black_bands([self._frame(60)] * 6)
        assert rows > qa.BAND_FRACTION and cols < 0.01

    def test_clean_frame_passes(self) -> None:
        rows, cols = qa.detect_black_bands([self._frame(0)] * 6)
        assert rows == 0 and cols == 0

    def test_fully_black_frame_is_not_a_band(self) -> None:
        assert qa.band_fractions(np.full((100, 100), 5, dtype=np.uint8)) == (0.0, 0.0)

    def test_median_over_frames(self) -> None:
        frames = [self._frame(60)] + [self._frame(0)] * 5  # one dark frame only
        rows, _ = qa.detect_black_bands(frames)
        assert rows < qa.BAND_FRACTION

    def test_vertical_bands(self) -> None:
        img = self._frame(0)
        img[:, :40] = 10
        _, cols = qa.band_fractions(img)
        assert cols > qa.BAND_FRACTION

    def test_caption_over_face_overlap(self) -> None:
        assert qa.box_covers_face((0, 100, 100, 200), (10, 120, 60, 180)) == 1.0
        assert qa.box_covers_face((0, 0, 50, 50), (60, 60, 100, 100)) == 0.0

    def test_parse_vision(self) -> None:
        assert qa.parse_vision('{"ok": true, "issues": []}') == []
        issues = qa.parse_vision(
            '{"ok": false, "issues": [{"type": "subject_cut", "detail": "head cut"}]}'
        )
        assert issues == [{"code": "subject_cut", "message": "head cut"}]
        assert qa.parse_vision("garbage") is None

    def test_result_to_dict(self) -> None:
        r = qa.QAResult()
        assert r.to_dict()["status"] == "pass"
        r.add(qa.BLACK_BANDS, "x")
        assert r.to_dict()["status"] == "warn" and r.codes() == {qa.BLACK_BANDS}

    def test_caption_box_from_ass(self, tmp_path) -> None:
        subs = _film_ass(words_from("Stand where you are and listen to me."))
        path = tmp_path / "c.ass"
        subs.save(str(path), encoding="utf-8")
        box = qa.caption_box_at(path, 0.5, 720, 1280)
        assert box is not None and box[3] > 800 and box[2] > box[0]


class TestCoordinatorFixes:
    BRIEF = (
        "Intro text\n\nRequired Release Information:\n- Coming to digital November 17\n"
        "- Pre-order now on Apple TV\n- Includes accurate info\n\nOther: x\n"
    )

    def test_required_section_lines(self) -> None:
        lines = campaign.required_section_lines(self.BRIEF)
        assert "Coming to digital November 17" in lines and "Pre-order now on Apple TV" in lines
        assert not any(line.startswith("Includes") for line in lines)

    def test_near_duplicate_hashtag_dropped(self) -> None:
        tags = campaign.clean_hashtags(
            ["#OnceUponATimeInTexas"],
            ["#shorts", "#onceuponatimeinttexas", "#shorts", "#western"],
            4,
        )
        assert tags == ["#OnceUponATimeInTexas", "#shorts", "#western"]

    def test_label_hooks_generic(self) -> None:
        assert hooks.is_generic_hook("Charlie's Jacket Reveal")
        assert hooks.is_generic_hook("Charlie's Teeth Challenge")
        assert not hooks.is_generic_hook("He just threatened to knock his teeth out")

    def test_vision_source_judgement_filtered(self) -> None:
        raw = '{"ok": false, "issues": [{"type": "rule_violation", "detail": "imagery from another film"}]}'
        assert qa.parse_vision(raw) == []


class TestDescriptionEndsWithHashtags:
    def test_with_hashtags_idempotent(self) -> None:
        once = campaign.with_hashtags("Great scene.\n\nOut now", ["#A", "#B"])
        assert once.endswith("Out now\n\n#A #B")
        assert campaign.with_hashtags(once, ["#A", "#B"]) == once
        assert campaign.with_hashtags(once, ["#A", "#C"]).endswith("\n\n#A #C")

    def test_enforce_appends_once_and_stays_in_sync(self) -> None:
        rules = campaign.CampaignRules(
            required_hashtags=["#Movie"], required_caption_text=["Out now"]
        )
        clip = SimpleNamespace(description="Nice.", hashtags=["#x1y"])
        campaign.enforce_metadata(rules, clip)
        campaign.enforce_metadata(rules, clip)
        assert clip.description.count("#Movie") == 1
        assert clip.description.endswith(" ".join(clip.hashtags))
        assert "Out now" in clip.description
