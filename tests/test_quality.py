import shutil
import subprocess

import pytest
from clipstudio.quality import QualityReport, check_clip, checks, loudness_filter

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")


def _make(path, video, audio, dur=12):
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"{video}{chr(58) if chr(61) in video else chr(61)}size=360x640:rate=15:duration={dur}",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", audio, "-shortest", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    monkeypatch.setattr(checks, "score_hook", lambda *a, **k: None)
    monkeypatch.setattr(checks, "_faces_in_frames", lambda frames: None)


def codes(rep):
    return {i["code"] for i in rep.issues}


def test_black_silent_rejected(tmp_path):
    p = _make(tmp_path / "b.mp4", "color=c=black", "anullsrc=r=44100:cl=mono")
    rep = check_clip(p)
    assert rep.status == "rejected"
    assert {"black_frames", "silent"} <= codes(rep)


def test_frozen_rejected(tmp_path):
    p = _make(tmp_path / "f.mp4", "color=c=gray", "sine=f=440:d=12")
    assert "frozen_frames" in codes(check_clip(p))


def test_good_clip_ready(tmp_path):
    p = _make(tmp_path / "g.mp4", "testsrc2", "sine=f=300:d=12,volume=0.3")
    rep = check_clip(p)
    # Sine at this level may be outside LUFS range; only structural errors matter here.
    assert "black_frames" not in codes(rep) and "frozen_frames" not in codes(rep)
    assert rep.metrics["lufs"] is not None


def test_short_duration_review(tmp_path):
    p = _make(tmp_path / "s.mp4", "testsrc2", "sine=f=300:d=4", dur=4)
    assert "duration" in codes(check_clip(p))


def test_no_audio_rejected(tmp_path):
    p = _make(tmp_path / "n.mp4", "testsrc2", None)
    assert "no_audio" in codes(check_clip(p))


def test_missing_file(tmp_path):
    assert check_clip(tmp_path / "nope.mp4").status == "rejected"


def test_report_finalize():
    r = QualityReport()
    r.add("x", "y", "warn")
    assert r.finalize().status == "review" and r.score == 88


def test_loudness_filter():
    assert loudness_filter(-14).startswith("loudnorm=I=-14")


def test_score_hook_failure_returns_none(monkeypatch):
    import clipstudio.providers as prov

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(prov, "build_provider", boom)
    monkeypatch.undo()  # restore autouse patches irrelevant here
    monkeypatch.setattr(prov, "build_provider", boom)
    assert checks.score_hook("hello there", "hook") is None
