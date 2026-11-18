"""Clip-editor: eigen indeling (gesplitst/vullen) en eigen ondertitels."""
import json

import pytest
from sqlalchemy import select

from app.core.db import new_session
from app.core.models import Clip
from app.pipeline import captions, framing, media
from app.worker.runner import run_job_once
from tests.conftest import create_user, login, make_client
from tests.test_pipeline_run import _fake_transcribe, _put_source, project_factory  # noqa: F401
from app.pipeline import run as run_mod

SPLIT = {"type": "split", "cam": [0.0, 0.0, 0.3, 0.3], "game": [0.3, 0.0, 0.4, 1.0], "top": 0.4}


def test_validate_layout():
    assert framing.validate_layout(SPLIT)["top"] == 0.4
    assert framing.validate_layout({"type": "crop", "rect": [0.2, 0, 0.32, 1]})["type"] == "crop"
    for bad in (None, {}, {"type": "split", "cam": [0, 0, 0.3, 0.3]}, {"type": "crop", "rect": [0.9, 0, 0.5, 1]},
                {"type": "crop", "rect": [0, 0, 0, 1]}, {"type": "x"}, {"type": "split", "cam": [0, 0, 1, 1], "game": "a", "top": 0.3}):
        assert framing.validate_layout(bad) is None


def test_layout_filter_keeps_aspect():
    f = framing.layout_filter(1920, 1080, SPLIT)
    top_h, bot_h = framing.split_heights(0.4)
    assert f"scale=1080:{top_h}" in f and f"scale=1080:{bot_h}" in f and top_h + bot_h == 1920
    # facecam-vak 576x324 wordt bijgesneden tot de verhouding van het bovenste vak (1080:768)
    cam = f.split("[cam]crop=")[1].split(",")[0].split(":")
    w, h = int(cam[0]), int(cam[1])
    assert abs(w / h - 1080 / top_h) < 0.02
    f2 = framing.layout_filter(1920, 1080, {"type": "crop", "rect": [0.5, 0, 0.5, 1]})
    w, h, x, _ = (int(v) for v in f2.split("crop=")[1].split(",")[0].split(":"))
    assert abs(w / h - 9 / 16) < 0.01 and x + w <= 1920


def test_custom_captions_ass_and_srt():
    words = [{"start": 1.0 + i * 0.3, "end": 1.25 + i * 0.3, "word": w} for i, w in enumerate("dit is fout gehoord".split())]
    groups = captions.default_groups(words, 1.0, 3.0)
    edited = [captions.retime(groups[0], "dit is goed gehoord")] + groups[1:]
    ass = captions.build_ass(words, 1.0, 3.0, "geel", {"pos": 35, "size": "L", "upper": False, "color": "groen"}, edited)
    assert "goed" in ass and "fout" not in ass and "\\pos(540,672)" in ass and ",108," in ass and "&H0047E05A" in ass
    srt = captions.build_srt(words, 1.0, 3.0, edited)
    assert "dit is goed" in srt


def test_editor_flow(project_factory, settings, sample_video, monkeypatch, admin_client, app):
    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    run_job_once()
    db = new_session()
    clip = db.scalars(select(Clip).where(Clip.project_id == pid)).first()
    cid = clip.id
    assert clip.auto_layout is not None or clip.framing_mode in ("blur", "vertical")
    db.close()

    page = admin_client.get(f"/clips/{cid}/edit")
    assert page.status_code == 200 and 'id="le-stage"' in page.text and 'name="cap_text"' in page.text
    n = page.text.count('name="cap_text"')
    texts = ["AANGEPASTE tekst"] + [""] * (n - 1)  # eerste regel wijzigen, rest verwijderen

    # ongeldige indeling
    r = admin_client.post(f"/clips/{cid}/editor", data={"layout_mode": "split", "layout_json": "{}", "cap_text": texts})
    assert r.status_code == 400
    # geldig: gesplitst + eigen ondertitel bovenin op de naad
    r = admin_client.post(f"/clips/{cid}/editor", data={
        "layout_mode": "split", "layout_json": json.dumps(SPLIT), "cap_pos": "40", "cap_size": "L",
        "cap_color": "wit", "cap_upper": "1", "cap_enabled": "1", "cap_text": texts})
    assert r.status_code == 303
    run_job_once()
    db = new_session()
    clip = db.get(Clip, cid)
    assert clip.status == "ready" and clip.framing_mode == "split" and clip.layout["top"] == 0.4
    assert [g["text"] for g in clip.custom_captions] == ["AANGEPASTE tekst"]
    ass = (settings.output_dir / str(pid) / clip.ass_path).read_text()
    assert "AANGEPASTE" in ass and "TEKST" in ass and "\\pos(540,768)" in ass
    final = settings.output_dir / str(pid) / clip.file_path
    p = media.probe(final)
    assert (p["width"], p["height"]) == (1080, 1920)
    db.close()
    srt = admin_client.get(f"/clips/{cid}/subtitles.srt").text
    assert "AANGEPASTE tekst" in srt

    # andere gebruiker kan niet bij de editor
    create_user("mallory")
    m = make_client(app)
    login(m, "mallory")
    assert m.get(f"/clips/{cid}/edit").status_code == 404
    assert m.post(f"/clips/{cid}/editor", data={"layout_mode": "auto"}).status_code == 404

    # terug naar automatisch + ondertitels resetten
    r = admin_client.post(f"/clips/{cid}/editor", data={"layout_mode": "auto", "reset_captions": "1", "cap_pos": "72"})
    assert r.status_code == 303
    db = new_session()
    clip = db.get(Clip, cid)
    assert clip.layout_json is None and clip.captions_json is None
    db.close()


def test_trim_resets_custom_captions(project_factory, settings, sample_video, monkeypatch, admin_client):
    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    run_job_once()
    db = new_session()
    clip = db.scalars(select(Clip).where(Clip.project_id == pid)).first()
    clip.captions_json = json.dumps([{"start": 0, "end": 1, "text": "x", "words": []}])
    db.commit()
    cid = clip.id
    db.close()
    admin_client.post(f"/clips/{cid}/trim", data={"start": "0.5", "end": "4"})
    db = new_session()
    assert db.get(Clip, cid).captions_json is None
    db.close()
