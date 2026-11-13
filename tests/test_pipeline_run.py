"""De pipeline-orchestratie met nep-download/transcriptie en echte ffmpeg-render."""
import json
import shutil

import httpx
import pytest
from sqlalchemy import select

from app.core.db import new_session
from app.core.models import Clip, Project
from app.pipeline import download as download_mod
from app.pipeline import pick as pick_mod
from app.pipeline import run as run_mod
from app.pipeline.errors import DownloadBlocked, WhisperError
from app.worker import queue
from app.worker.runner import run_job_once
from tests.conftest import fake_transcript, user_id


def _fake_transcribe(media, cache, *a, **kw):
    t = fake_transcript(8.0)
    cache.write_text(json.dumps(t))
    return t


@pytest.fixture
def project_factory(admin_client, settings):
    def make(source_type="upload", url=None):
        db = new_session()
        p = Project(owner_id=user_id("admin"), source_type=source_type, source_url=url, title="Testvideo",
                    num_clips=2, min_dur=2, max_dur=6)
        db.add(p)
        db.commit()
        queue.enqueue(db, p)
        pid = p.id
        db.close()
        return pid
    return make


def _put_source(settings, pid, sample_video):
    d = settings.input_dir / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(sample_video, d / "source.mp4")


def test_full_pipeline_llm_failure_falls_back(project_factory, settings, sample_video, monkeypatch, admin_client):
    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)
    admin_client.post("/keys/llm", data={"name": "x", "base_url": "https://llm.invalid/v1", "model": "m",
                                         "api_key": "gsk_abc_1234567890", "make_active": "1"})

    def failing(*a, **kw):
        raise pick_mod.LLMError("serverfout (500)", "error")

    monkeypatch.setattr(pick_mod, "chat_json", failing)
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    assert run_job_once()
    db = new_session()
    p = db.get(Project, pid)
    assert p.status == "done", p.error
    assert p.picker_used == "heuristiek"
    assert any("heuristiek" in w for w in p.warnings)
    clips = db.scalars(select(Clip).where(Clip.project_id == pid)).all()
    assert clips and all(c.status == "ready" for c in clips)
    out = settings.output_dir / str(pid)
    for c in clips:
        assert (out / c.file_path).exists() and (out / c.raw_path).exists() and (out / c.ass_path).exists()
    assert (out / "clips.json").exists() and (out / "transcript.json").exists() and (out / "thumb.jpg").exists()
    db.close()
    # project pagina toont clips en waarschuwing
    page = admin_client.get(f"/projects/{pid}").text
    assert "heuristiek" in page and "Download mp4" in page
    # trim + opnieuw renderen via de wachtrij
    cid = clips[0].id
    assert admin_client.post(f"/clips/{cid}/trim", data={"start": "0.5", "end": "4.0"}).status_code == 303
    assert run_job_once()
    db = new_session()
    c = db.get(Clip, cid)
    assert c.status == "ready" and (c.start, c.end) == (0.5, 4.0)
    db.close()


def test_download_blocked_then_upload_continues(project_factory, settings, sample_video, monkeypatch, admin_client):
    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)

    def blocked(*a, **kw):
        raise DownloadBlocked("YouTube blokkeert de download (Sign in to confirm you're not a bot).")

    monkeypatch.setattr(run_mod, "download", blocked)
    pid = project_factory("youtube", "https://www.youtube.com/watch?v=abc")
    run_job_once()
    db = new_session()
    p = db.get(Project, pid)
    assert p.status == "failed" and p.error_kind == "download_blocked"
    db.close()
    page = admin_client.get(f"/projects/{pid}").text
    assert "Download geblokkeerd" in page and "upload-source" in page and "Opnieuw proberen" in page
    with open(sample_video, "rb") as fh:
        r = admin_client.post(f"/projects/{pid}/upload-source", files={"file": ("eigen.mp4", fh, "video/mp4")})
    assert r.status_code == 303
    run_job_once()
    db = new_session()
    p = db.get(Project, pid)
    assert p.status == "done", p.error
    db.close()


def test_whisper_error_shown(project_factory, settings, sample_video, monkeypatch, admin_client):
    def bad(*a, **kw):
        raise WhisperError("Whisper-model 'huge' laden mislukt (OSError: geen geheugen).")

    monkeypatch.setattr(run_mod, "transcribe", bad)
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    run_job_once()
    page = admin_client.get(f"/projects/{pid}").text
    assert "Transcriptie mislukt" in page and "laden mislukt" in page


def test_render_failure_keeps_raw(project_factory, settings, sample_video, monkeypatch, admin_client):
    from app.pipeline import render
    from app.pipeline.errors import RenderError

    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)

    def burn_fail(*a, **kw):
        raise RenderError("libass kapot")

    monkeypatch.setattr(render, "burn_subtitles", burn_fail)
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    run_job_once()
    db = new_session()
    p = db.get(Project, pid)
    assert p.status == "done"
    c = db.scalars(select(Clip).where(Clip.project_id == pid)).first()
    assert c.status == "failed" and c.raw_path and c.ass_path and c.file_path is None
    db.close()
    assert admin_client.get(f"/clips/{c.id}/download-raw").status_code == 200
    assert admin_client.get(f"/clips/{c.id}/download").status_code == 200  # valt terug op raw
    assert admin_client.get(f"/clips/{c.id}/subtitles.ass").status_code == 200
    assert "Opnieuw renderen" in admin_client.get(f"/projects/{pid}").text


def test_manual_clip(project_factory, settings, sample_video, monkeypatch, admin_client):
    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)
    monkeypatch.setattr(pick_mod, "llm_pick", lambda *a, **k: [])
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    run_job_once()
    r = admin_client.post(f"/projects/{pid}/clips", data={"start": "0:01", "end": "5.5", "title": "Mijn clip"})
    assert r.status_code == 303
    run_job_once()
    db = new_session()
    c = db.scalars(select(Clip).where(Clip.project_id == pid, Clip.source == "handmatig")).one()
    assert c.status == "ready" and c.start == 1.0 and c.title == "Mijn clip"
    db.close()


def test_queue_positions(admin_client, settings):
    from app.core.db import new_session
    db = new_session()
    ids = []
    for i in range(3):
        p = Project(owner_id=user_id("admin"), source_type="upload", title=f"p{i}")
        db.add(p)
        db.commit()
        queue.enqueue(db, p)
        ids.append(p.id)
    assert [queue.position(db, i) for i in ids] == [1, 2, 3]
    queue.claim_next(db)
    assert [queue.position(db, i) for i in ids] == [0, 1, 2]
    db.close()


def test_max_active_projects(admin_client):
    admin_client.post("/settings/global", data={"max_active_per_user": "1", "source_retention_days": "7", "project_retention_days": "0"})
    r = admin_client.post("/projects", data={"url": "https://www.youtube.com/watch?v=a", "num_clips": "3", "min_dur": "20", "max_dur": "50"})
    assert r.headers["location"].startswith("/projects/")
    r = admin_client.post("/projects", data={"url": "https://www.youtube.com/watch?v=b", "num_clips": "3", "min_dur": "20", "max_dur": "50"})
    assert r.headers["location"] == "/"


def test_cleanup_removes_old_sources(project_factory, settings, sample_video, admin_client):
    from datetime import timedelta
    from app.worker.cleanup import run_cleanup
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    db = new_session()
    p = db.get(Project, pid)
    p.status = "done"
    p.finished_at = p.created_at - timedelta(days=8)
    db.commit()
    stats = run_cleanup(db)
    assert stats["sources_removed"] == 1
    assert not (settings.input_dir / str(pid)).exists()
    assert db.get(Project, pid).source_deleted
    db.close()


def test_youtube_url_validation():
    assert download_mod.is_valid_url("https://www.youtube.com/watch?v=abc")
    assert download_mod.is_valid_url("https://youtu.be/abc")
    assert not download_mod.is_valid_url("https://evil.example.com/watch?v=abc")
    assert not download_mod.is_valid_url("file:///etc/passwd")


def test_framing_choices_and_reframe(project_factory, settings, sample_video, monkeypatch, admin_client):
    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)
    admin_client.post("/settings", data={"channel_avg_views": "1000", "default_num_clips": "2", "default_min_dur": "5",
                                         "default_max_dur": "20", "caption_style": "geel", "framing_default": "crop"})
    page = admin_client.get("/").text
    assert 'value="crop" selected' in page
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    run_job_once()
    db = new_session()
    clips = db.scalars(select(Clip).where(Clip.project_id == pid)).all()
    assert all(c.framing_mode == "blur" for c in clips)  # project stond op 'auto', testsrc heeft geen gezicht
    cid = clips[0].id
    db.close()

    # per clip: vullen met positie links
    assert admin_client.post(f"/clips/{cid}/framing", data={"mode": "crop", "position": "0"}).status_code == 303
    run_job_once()
    db = new_session()
    c = db.get(Clip, cid)
    assert c.framing_mode == "crop" and c.crop_x == 0.0 and c.framing_choice == "crop" and c.status == "ready"
    db.close()

    # hele project op vullen zetten
    assert admin_client.post(f"/projects/{pid}/reframe", data={"framing": "crop"}).status_code == 303
    while run_job_once():
        pass
    db = new_session()
    p = db.get(Project, pid)
    assert p.framing == "crop"
    for c in p.clips:
        assert c.framing_mode == "crop" and c.framing_choice is None and c.crop_x is None and c.status == "ready"
    db.close()
    assert admin_client.post(f"/projects/{pid}/reframe", data={"framing": "zoom"}).status_code == 400


def test_split_mode_via_clip_route(project_factory, settings, sample_video, monkeypatch, admin_client):
    from app.pipeline import framing

    class Cam:
        name = "nep"

        def detect(self, frame):
            return [(0.15, 0.25, 0.08, 0.9)]

    monkeypatch.setattr(framing, "_detector", lambda: Cam())
    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    run_job_once()
    db = new_session()
    cid = db.scalars(select(Clip).where(Clip.project_id == pid)).first().id
    db.close()
    assert "Gesplitst (facecam boven, gameplay onder)" in admin_client.get(f"/projects/{pid}").text
    assert admin_client.post(f"/clips/{cid}/framing", data={"mode": "split", "position": "70"}).status_code == 303
    run_job_once()
    db = new_session()
    c = db.get(Clip, cid)
    assert c.framing_mode == "split" and c.framing_choice == "split" and c.crop_x == 0.7 and c.status == "ready"
    db.close()
    assert "gesplitst" in admin_client.get(f"/projects/{pid}").text


def test_facecam_routes(project_factory, settings, sample_video, monkeypatch, admin_client, app):
    from tests.conftest import create_user, login, make_client

    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)
    pid = project_factory()
    _put_source(settings, pid, sample_video)
    run_job_once()
    page = admin_client.get(f"/projects/{pid}").text
    assert "Facecam aanwijzen" in page and f"/projects/{pid}/frame.jpg?t=" in page
    r = admin_client.get(f"/projects/{pid}/frame.jpg?t=1")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    # zonder rechthoek: nette melding
    r = admin_client.post(f"/projects/{pid}/facecam", data={"x": "", "y": "", "w": "", "h": ""})
    assert r.status_code == 303
    db = new_session()
    assert db.get(Project, pid).facecam_box is None
    db.close()
    # opslaan -> project wordt gesplitst en clips opnieuw gerenderd
    r = admin_client.post(f"/projects/{pid}/facecam", data={"x": "0", "y": "0", "w": "30", "h": "30"})
    assert r.status_code == 303
    while run_job_once():
        pass
    db = new_session()
    p = db.get(Project, pid)
    assert p.facecam_box == "0.0000,0.0000,0.3000,0.3000" and p.framing == "split"
    assert all(c.framing_mode == "split" and c.status == "ready" for c in p.clips)
    db.close()
    assert 'data-box="0.00,0.00,30.00,30.00"' in admin_client.get(f"/projects/{pid}").text
    # wissen
    admin_client.post(f"/projects/{pid}/facecam", data={"clear": "1"})
    db = new_session()
    assert db.get(Project, pid).facecam_box is None
    db.close()
    # een andere gebruiker kan het beeld en de facecam niet zien of wijzigen
    create_user("eve")
    eve = make_client(app)
    login(eve, "eve")
    assert eve.get(f"/projects/{pid}/frame.jpg?t=1").status_code == 404
    assert eve.post(f"/projects/{pid}/facecam", data={"x": "0", "y": "0", "w": "30", "h": "30"}).status_code == 404


def test_parse_box():
    from app.pipeline.facecam import format_box, parse_box

    assert parse_box("0.1,0.2,0.3,0.4") == (0.1, 0.2, 0.3, 0.4)
    assert parse_box(format_box((0.1, 0.2, 0.3, 0.4))) == (0.1, 0.2, 0.3, 0.4)
    for bad in (None, "", "a,b,c,d", "0.9,0.9,0.5,0.5", "0,0,0,0.5", "-0.1,0,0.5,0.5"):
        assert parse_box(bad) is None
