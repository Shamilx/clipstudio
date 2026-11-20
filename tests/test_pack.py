"""Export pack zips and campaign-rule metadata."""

from __future__ import annotations

import json
import zipfile

from clipstudio import templates as tpl
from clipstudio.api import pack
from clipstudio.db import store
from clipstudio.db.models import Clip, Export, Job, Source, new_id
from clipstudio.templates.rules import apply_to_metadata, clamp_duration


def test_template_rule_fields_validate():
    t = tpl.validate({
        "name": "camp", "max_duration_s": 9999, "required_hashtags": "foo, #Bar, foo, !!",
        "credit_text": "Credit: @x", "watermark_text": "  ",
    })
    assert t.max_duration_s == 600
    assert t.required_hashtags == ["#foo", "#Bar"]
    assert t.credit_text == "Credit: @x" and t.watermark_text is None
    assert tpl.validate({"name": "a"}).max_duration_s is None
    assert tpl.validate({"name": "a", "max_duration_s": 1}).max_duration_s == 5
    assert clamp_duration(t, 120) == 120 or clamp_duration(t, 700) == 600


def test_apply_to_metadata():
    t = tpl.Template(required_hashtags=["#Ad", "#brand"], credit_text="Credit: @creator")
    title, desc, tags = apply_to_metadata(t, " T " * 60, "hello", ["#ad", "fun"])
    assert len(title) <= 100
    assert tags == ["#Ad", "#brand", "#fun"]
    assert desc == "hello\n\nCredit: @creator"
    _, desc2, _ = apply_to_metadata(t, "x", desc, [])
    assert desc2.count("Credit: @creator") == 1
    assert apply_to_metadata(None, "x", "d", ["a"]) == ("x", "d", ["#a"])


def _seed(tmp_path, template="auto"):
    video = tmp_path / "out.mp4"
    video.write_bytes(b"not really a video")
    src = Source(id=new_id(), type="upload", path=str(video), title="s")
    store.create_source(src)
    job = Job(id=new_id(), source_id=src.id, provider="x", settings={"template": template})
    store.create_job(job)
    clip = Clip(id=new_id(), job_id=job.id, start_s=1, end_s=31, title="Big Idea",
                description="Why it matters", hashtags=["#tips"])
    store.replace_clips(job.id, [clip])
    store.create_export(Export(id=new_id(), clip_id=clip.id, path=str(video)))
    return job, clip


def test_clip_and_job_pack(initialised_db, tmp_path, monkeypatch):
    monkeypatch.setattr(pack, "make_thumbnail", lambda v, o, at=1.0: o.write_bytes(b"jpg") or True)
    job, clip = _seed(tmp_path)

    dest = tmp_path / "clip.zip"
    name = pack.build_clip_pack(clip.id, dest)
    assert name.endswith("-pack.zip")
    with zipfile.ZipFile(dest) as zf:
        assert sorted(zf.namelist()) == [
            "big-idea.mp4", "description.txt", "meta.json", "thumbnail.jpg", "title.txt",
        ]
        assert zf.read("title.txt").decode().strip() == "Big Idea"
        assert "#tips" in zf.read("description.txt").decode()
        assert json.loads(zf.read("meta.json"))["clip_id"] == clip.id

    dest2 = tmp_path / "job.zip"
    pack.build_job_pack(job.id, dest2)
    with zipfile.ZipFile(dest2) as zf:
        assert "01-big-idea/title.txt" in zf.namelist()


def test_pack_applies_template_rules(initialised_db, tmp_path, monkeypatch):
    monkeypatch.setattr(pack, "make_thumbnail", lambda v, o, at=1.0: False)
    tpl.save_template(tpl.validate({
        "name": "camp", "required_hashtags": ["promo"], "credit_text": "Credit: @me"}))
    job, clip = _seed(tmp_path, "camp")
    dest = tmp_path / "p.zip"
    pack.build_clip_pack(clip.id, dest)
    with zipfile.ZipFile(dest) as zf:
        d = zf.read("description.txt").decode()
        assert "Credit: @me" in d and d.strip().endswith("#promo #tips")
        assert "thumbnail.jpg" not in zf.namelist()


def test_missing_export(initialised_db, tmp_path):
    import pytest

    job, clip = _seed(tmp_path)
    (tmp_path / "out.mp4").unlink()
    with pytest.raises(FileNotFoundError):
        pack.build_clip_pack(clip.id, tmp_path / "z.zip")
    with pytest.raises(LookupError):
        pack.build_clip_pack("nope", tmp_path / "z.zip")
