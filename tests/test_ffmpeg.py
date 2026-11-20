"""ffmpeg helpers — especially filtergraph path escaping.

Escaping is the single most common Windows-only failure in ffmpeg-based tools:
an unescaped drive-letter colon is read as an option separator, so the `ass`
filter silently looks for a file called "C" and the render either dies or comes
out with no captions.
"""

from __future__ import annotations

from pathlib import PureWindowsPath

import pytest
from clipstudio.pipeline import ffmpeg


class TestFilterPathEscaping:
    """Filter option values survive two unescaping rounds, so specials need two
    backslashes. Verified against the real ffmpeg binary: a single backslash
    produces "No option name near '/Users/...'"."""

    def test_windows_drive_colon_gets_two_backslashes(self) -> None:
        escaped = ffmpeg.escape_filter_path(PureWindowsPath(r"C:\Users\Jad\clip.ass"))

        assert escaped == r"C\\:/Users/Jad/clip.ass"

    def test_backslashes_become_forward_slashes(self) -> None:
        escaped = ffmpeg.escape_filter_path(PureWindowsPath(r"D:\opusclip\work\a.ass"))

        assert escaped.count("/") == 3

    def test_posix_paths_pass_through_unchanged(self) -> None:
        assert ffmpeg.escape_filter_path("/home/jad/clip.ass") == "/home/jad/clip.ass"

    @pytest.mark.parametrize("char", ["'", "[", "]", ",", ";", ":"])
    def test_filtergraph_specials_are_escaped(self, char: str) -> None:
        escaped = ffmpeg.escape_filter_path(f"/tmp/a{char}b.ass")

        assert f"\\\\{char}" in escaped

    def test_spaces_are_left_alone(self) -> None:
        # Spaces are safe inside a filtergraph and escaping them breaks libass.
        escaped = ffmpeg.escape_filter_path(PureWindowsPath(r"C:\My Videos\clip.ass"))

        assert escaped == r"C\\:/My Videos/clip.ass"

    def test_unicode_survives(self) -> None:
        escaped = ffmpeg.escape_filter_path("/tmp/café_日本.ass")

        assert "café_日本" in escaped

    def test_filter_value_escaping_matches(self) -> None:
        assert ffmpeg.escape_filter_value("Arial:Bold") == "Arial\\:Bold"


class TestRelativeFilterWorkspace:
    """The escaping-free path used for real renders."""

    def test_returns_bare_names(self, tmp_path) -> None:
        subtitles = tmp_path / "captions.ass"
        subtitles.write_text("[Script Info]\n", encoding="utf-8")
        fonts = tmp_path / "srcfonts"
        fonts.mkdir()
        (fonts / "Anton-Regular.ttf").write_bytes(b"\x00\x01\x00\x00")

        workspace, subtitle_name, fonts_name = ffmpeg.relative_filter_workspace(subtitles, fonts)

        # Bare names contain nothing a filtergraph parser could misread.
        assert subtitle_name == "captions.ass"
        assert fonts_name == "fonts"
        assert not any(c in subtitle_name + fonts_name for c in ":'[],;\\/")
        assert workspace == tmp_path

    def test_fonts_are_staged_into_the_workspace(self, tmp_path) -> None:
        subtitles = tmp_path / "captions.ass"
        subtitles.write_text("[Script Info]\n", encoding="utf-8")
        fonts = tmp_path / "srcfonts"
        fonts.mkdir()
        (fonts / "Anton-Regular.ttf").write_bytes(b"\x00\x01\x00\x00")

        workspace, _, fonts_name = ffmpeg.relative_filter_workspace(subtitles, fonts)

        assert (workspace / fonts_name / "Anton-Regular.ttf").exists()

    def test_works_when_the_directory_has_awkward_characters(self, tmp_path) -> None:
        awkward = tmp_path / "My Clips (2026)" / "it's here"
        awkward.mkdir(parents=True)
        subtitles = awkward / "captions.ass"
        subtitles.write_text("[Script Info]\n", encoding="utf-8")
        fonts = tmp_path / "srcfonts"
        fonts.mkdir()

        workspace, subtitle_name, _ = ffmpeg.relative_filter_workspace(subtitles, fonts)

        assert subtitle_name == "captions.ass"
        assert workspace == awkward

    def test_is_idempotent(self, tmp_path) -> None:
        subtitles = tmp_path / "captions.ass"
        subtitles.write_text("[Script Info]\n", encoding="utf-8")
        fonts = tmp_path / "srcfonts"
        fonts.mkdir()
        (fonts / "Anton-Regular.ttf").write_bytes(b"\x00\x01\x00\x00")

        ffmpeg.relative_filter_workspace(subtitles, fonts)
        workspace, subtitle_name, fonts_name = ffmpeg.relative_filter_workspace(subtitles, fonts)

        assert (workspace / subtitle_name).exists()
        assert (workspace / fonts_name / "Anton-Regular.ttf").exists()


class TestFpsParsing:
    @pytest.mark.parametrize(
        ("rate", "expected"),
        [
            ("30/1", 30.0),
            ("30000/1001", pytest.approx(29.97, abs=0.01)),
            ("25", 25.0),
            ("60000/1001", pytest.approx(59.94, abs=0.01)),
        ],
    )
    def test_rational_frame_rates(self, rate: str, expected) -> None:
        assert ffmpeg._parse_fps(rate) == expected

    @pytest.mark.parametrize("rate", [None, "", "0/0", "not-a-rate"])
    def test_unparseable_rates_return_none(self, rate) -> None:
        assert ffmpeg._parse_fps(rate) is None


class TestMediaInfo:
    def test_aspect_ratio_is_derived(self) -> None:
        from pathlib import Path

        info = ffmpeg.MediaInfo(
            path=Path("x.mp4"),
            duration_s=10,
            has_video=True,
            has_audio=True,
            width=1920,
            height=1080,
        )

        assert info.aspect_ratio == pytest.approx(16 / 9)
        assert info.is_vertical is False

    def test_vertical_video_is_detected(self) -> None:
        from pathlib import Path

        info = ffmpeg.MediaInfo(
            path=Path("x.mp4"),
            duration_s=10,
            has_video=True,
            has_audio=True,
            width=1080,
            height=1920,
        )

        assert info.is_vertical is True

    def test_missing_dimensions_give_no_ratio(self) -> None:
        from pathlib import Path

        info = ffmpeg.MediaInfo(path=Path("x.m4a"), duration_s=10, has_video=False, has_audio=True)

        assert info.aspect_ratio is None
        assert info.is_vertical is False


class TestRunSupervision:
    """The runner must survive a chatty stderr, cancel quickly and kill stalls."""

    @staticmethod
    def _fake(script: str) -> list[str]:
        import sys

        return [sys.executable, "-u", "-c", script]

    def test_large_stderr_does_not_deadlock(self) -> None:
        import time

        script = (
            "import sys\n"
            "for i in range(20):\n"
            "    sys.stderr.write('x' * 100000 + chr(10)); sys.stderr.flush()\n"
            "    print(f'out_time_us={i * 1000000}'); print('progress=continue')\n"
            "print('progress=end')\n"
        )
        seen: list[float] = []
        started = time.monotonic()
        ffmpeg.run_command(self._fake(script), total_duration_s=20.0, on_progress=seen.append)
        assert time.monotonic() - started < 15
        assert seen[-1] == 1.0 and len(seen) > 5

    def test_failure_reports_stderr_tail(self) -> None:
        script = "import sys; sys.stderr.write('boom\n'); sys.exit(3)"
        with pytest.raises(ffmpeg.FFmpegError) as info:
            ffmpeg.run_command(self._fake(script))
        assert "boom" in str(info.value)

    def test_cancel_kills_within_a_second_even_without_output(self) -> None:
        import time

        script = "import time; time.sleep(60)"
        started = time.monotonic()
        with pytest.raises(ffmpeg.Cancelled):
            ffmpeg.run_command(
                self._fake(script), cancelled=lambda: time.monotonic() - started > 0.3
            )
        assert time.monotonic() - started < 1.5

    def test_watchdog_kills_a_stalled_process(self) -> None:
        import time

        script = (
            "import time\n"
            "print('out_time_us=1000000', flush=True)\n"
            "while True:\n"
            "    print('progress=continue', flush=True); time.sleep(0.2)\n"
        )
        started = time.monotonic()
        with pytest.raises(ffmpeg.FFmpegError, match="no progress"):
            ffmpeg.run_command(self._fake(script), stall_timeout_s=1.0)
        assert time.monotonic() - started < 6


def test_children_never_start_below_normal_priority() -> None:
    import subprocess
    import sys

    from clipstudio import resources

    if sys.platform == "win32":
        flags = resources.popen_kwargs()["creationflags"]
        assert not flags & subprocess.BELOW_NORMAL_PRIORITY_CLASS
        assert not flags & subprocess.IDLE_PRIORITY_CLASS
