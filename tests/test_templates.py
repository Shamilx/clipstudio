import json

import pytest
from clipstudio import templates as T


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSTUDIO_HOME", str(tmp_path))


def test_builtins_present():
    names = {t.name for t in T.list_templates()}
    assert {"talk", "podcast", "hype", "story", "clean"} <= names


def test_unknown_falls_back():
    assert T.load_template("nope").name == "talk"
    assert T.load_template(None).name == "talk"


def test_user_override_and_clamp(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "hype.json").write_text(json.dumps(
        {"name": "hype", "zoom_strength": 9, "sfx_volume": "x", "accent_color": "red",
         "caption_preset": "bogus", "layout_mode": "?"}))
    (d / "broken.json").write_text("{not json")
    t = T.load_template("hype")
    assert t.zoom_strength == 1.3 and t.accent_color == "#FFD400"
    assert t.caption_preset == "yellow_pop" and t.layout_mode == "auto"


def test_save_delete():
    t = T.save_template(T.Template(name="My Brand", label="Mine"))
    assert t.name == "my-brand" and T.is_user("my-brand")
    assert T.delete_template("my-brand") and not T.is_user("my-brand")


@pytest.mark.parametrize("title,text,want", [
    ("How I made $1M", "money invest stock grind discipline never give up!", "hype"),
    ("Podcast ep 12", "welcome to the show, my guest today. tell me about it? what do you think?", "podcast"),
    ("My trip", "one day when I was a kid I remember the trip and then we went home", "story"),
    ("Lecture", "research shows the brain and psychology of learning, a study", "talk"),
    ("", "", "talk"),
])
def test_auto_pick(title, text, want):
    assert T.auto_pick(text, title) == want


def test_assets():
    from clipstudio.templates import assets
    for k in ("whoosh", "pop", "ding"):
        assert assets.sfx_path(k).is_file()
    assert assets.sfx_path("x") is None
    assert assets.emoji_png("\U0001F525").is_file()
    assert assets.emoji_png("❤️").is_file()
    assert assets.emoji_png("\U0001F9A4") is None
