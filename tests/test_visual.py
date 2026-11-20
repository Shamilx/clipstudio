"""Visual timeline: sampling plan, prompt injection, cache reuse, graceful failure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from clipstudio.pipeline import highlights, visual
from clipstudio.pipeline.reframe.scenes import Shot
from clipstudio.pipeline.transcript import Transcript, Word
from clipstudio.providers.base import (
    ClipCandidate,
    DetectionConfig,
    TranscriptWindow,
    render_window_prompt,
)

MOMENTS = [(5.0, "model walks the runway", True), (70.0, "host laughs", False), (200.0, "far", False)]


class TestPlan:
    def test_one_frame_at_shot_middle(self) -> None:
        assert visual.sample_times([Shot(0, 4), Shot(4, 10)], 10) == [2.0, 7.0]

    def test_long_shot_gets_extra_samples(self) -> None:
        times = visual.sample_times([Shot(0, 30)], 30)
        assert times == [3.0, 9.0, 15.0, 21.0, 27.0]

    def test_cap_and_sheets_of_nine(self) -> None:
        shots = [Shot(i, i + 1) for i in range(0, 100)]
        times = visual.sample_times(shots, 100, max_frames=20)
        assert len(times) == 20
        sheets = visual.plan_sheets(times)
        assert [len(s) for s in sheets] == [9, 9, 2]

    def test_sheet_is_a_jpeg_with_tiles(self, tmp_path: Path) -> None:
        from PIL import Image

        frames = []
        for i in range(4):
            p = tmp_path / f"{i}.jpg"
            Image.new("RGB", (384, 216), (i * 50, 0, 0)).save(p)
            frames.append((float(i), p))
        data = visual.build_sheet(frames)
        assert data[:2] == b"\xff\xd8"


class TestPrompt:
    def test_only_window_descriptions_are_listed(self) -> None:
        window = TranscriptWindow(text="[0]hi", first_word=0, last_word=0, start_s=0, end_s=100)
        prompt = render_window_prompt(window, DetectionConfig(visual=MOMENTS))
        assert "What is on screen (from video frames)" in prompt
        assert "[00:05] model walks the runway (brand visible)" in prompt
        assert "[01:10] host laughs" in prompt
        assert "far" not in prompt

    def test_no_section_without_visuals_or_matches(self) -> None:
        window = TranscriptWindow(text="x", first_word=0, last_word=0, start_s=300, end_s=400)
        assert "on screen" not in render_window_prompt(window, DetectionConfig(visual=MOMENTS))
        assert "on screen" not in render_window_prompt(window, DetectionConfig())


class TestSpeechlessClip:
    def test_time_range_candidate_becomes_a_clip_without_words(self) -> None:
        words = [Word("hello", 0.0, 0.5), Word("bye.", 59.0, 59.5)]
        transcript = Transcript(words=words, language="en")
        cand = ClipCandidate(start_s=10, end_s=40, title="Runway", score=80)
        clips = highlights.build_clips(
            transcript, [cand], DetectionConfig(min_keep=1), job_id="j"
        )
        assert len(clips) == 1
        assert (clips[0].start_s, clips[0].end_s) == (10, 40)
        assert highlights.clip_words(transcript, 10, 40, clips[0].start_word, clips[0].end_word) == []


class TestTimeline:
    @pytest.fixture
    def video(self, tmp_path: Path) -> Path:
        v = tmp_path / "v.mp4"
        v.write_bytes(b"x")
        return v

    def _patch(self, monkeypatch, calls: list, reply=None, fail=False):
        monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
        monkeypatch.setattr(visual, "detect_shots", lambda *a, **k: [Shot(0, 6), Shot(6, 12)])
        monkeypatch.setattr(
            visual, "extract_frame", lambda video, t, out, **k: (out.write_bytes(_jpeg()), True)[1]
        )

        async def fake_chat(model, prompt, jpeg, api_key):
            calls.append(model)
            if fail:
                raise RuntimeError("boom")
            return reply or json.dumps(
                [{"n": 1, "t": 3, "desc": "00:03 — model, dress, walks", "brand_visible": True}]
            )

        monkeypatch.setattr(visual, "_chat", fake_chat)

    def test_cache_is_reused(self, monkeypatch, video, tmp_path) -> None:
        calls: list = []
        self._patch(monkeypatch, calls)
        cache = tmp_path / "visual.json"
        first = visual.build_timeline(video, 12, cache=cache)
        assert [m.t for m in first] == [3.0] and first[0].brand_visible
        n = len(calls)
        assert n >= 1 and cache.exists()
        second = visual.build_timeline(video, 12, cache=cache)
        assert second == first and len(calls) == n

    def test_failure_is_graceful_and_not_cached(self, monkeypatch, video, tmp_path) -> None:
        calls: list = []
        self._patch(monkeypatch, calls, fail=True)
        monkeypatch.setattr(visual, "RETRIES", 1)
        cache = tmp_path / "visual.json"
        assert visual.build_timeline(video, 12, cache=cache) == []
        assert len(calls) == 2  # one try + one retry
        assert not cache.exists()

    def test_garbage_reply_is_skipped(self, monkeypatch, video, tmp_path) -> None:
        self._patch(monkeypatch, [], reply="not json at all")
        assert visual.build_timeline(video, 12, cache=tmp_path / "c.json") == []

    def test_no_key_skips_quietly(self, monkeypatch, video) -> None:
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        assert visual.build_timeline(video, 12) == []


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (384, 216), (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()
