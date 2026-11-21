"""Regressietests voor gevonden bugs: uploads die blijven hangen, rare invoer, sleutelbescherming."""
import math

import httpx
import pytest
from sqlalchemy import select

from app.core.db import new_session
from app.core.models import Clip, Credential, Job
from app.integrations import youtube_upload as yt
from app.worker.runner import run_job_once
from tests.conftest import create_user, login, make_client, make_project_with_clip
from tests.test_youtube_upload import _connect, _setup_oauth, google  # noqa: F401  (fixture)


def test_unexpected_upload_error_does_not_leave_clip_stuck(admin_client, settings, google, monkeypatch):  # noqa: F811
    _setup_oauth(admin_client)
    _connect(admin_client)
    _, (cid,) = make_project_with_clip(settings, "admin")

    def boom(*a, **k):
        raise RuntimeError("sleutel weg")

    monkeypatch.setattr(yt, "upload", boom)
    assert admin_client.post(f"/clips/{cid}/youtube", data={"title": "x"}).status_code == 303
    run_job_once()
    db = new_session()
    c = db.get(Clip, cid)
    assert c.yt_status == "failed" and "onverwachte fout" in c.yt_error and "sleutel weg" not in c.yt_error
    db.close()
    monkeypatch.undo()


def test_resumable_upload_continues_after_503(admin_client, settings, google, monkeypatch):  # noqa: F811
    _setup_oauth(admin_client)
    _connect(admin_client)
    _, (cid,) = make_project_with_clip(settings, "admin")
    monkeypatch.setattr(yt.time, "sleep", lambda s: None)
    state = {"n": 0, "ranges": []}
    orig = google.handler

    def handler(request: httpx.Request):
        if str(request.url).startswith("https://upload.example/sessie123"):
            state["ranges"].append(request.headers.get("content-range"))
            state["n"] += 1
            if state["n"] == 1:
                request.read()
                return httpx.Response(503)
            if request.headers.get("content-range", "").startswith("bytes */"):
                return httpx.Response(308, headers={"Range": "bytes=0-3"})
        return orig(request)

    monkeypatch.setattr(yt, "_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    admin_client.post(f"/clips/{cid}/youtube", data={"title": "x"})
    assert run_job_once()
    db = new_session()
    assert db.get(Clip, cid).yt_status == "done"
    db.close()
    assert state["ranges"][1].startswith("bytes */") and state["ranges"][2].startswith("bytes 4-")


def test_recover_releases_orphan_uploads(app, settings):
    from app.worker import queue

    create_user("bob")
    _, (cid,) = make_project_with_clip(settings, "bob")
    db = new_session()
    db.get(Clip, cid).yt_status = "uploading"
    db.commit()
    queue.recover(db)
    c = db.get(Clip, cid)
    assert c.yt_status == "failed" and c.status == "ready"
    db.close()


def test_admin_cannot_upload_someone_elses_clip(admin_client, settings, google):  # noqa: F811
    create_user("bob")
    _, (cid,) = make_project_with_clip(settings, "bob")
    r = admin_client.post(f"/clips/{cid}/youtube", data={"title": "x", "privacy": "public"})
    assert r.status_code == 403
    db = new_session()
    assert db.get(Clip, cid).yt_status is None and db.scalar(select(Job).where(Job.kind == "upload")) is None
    db.close()


def test_nan_times_rejected(admin_client, settings):
    _, (cid,) = make_project_with_clip(settings, "admin")
    r = admin_client.post(f"/clips/{cid}/trim", data={"start": "nan", "end": "nan"})
    assert r.status_code == 400


def test_file_part_instead_of_text_is_not_a_500(admin_client):
    r = admin_client.post("/review-settings", files={"uploads_per_day": ("x.txt", b"3")},
                          data={"min_gap_hours": "3"})
    assert r.status_code in (303, 400)


def test_llm_key_not_sent_to_new_host_without_reentry(admin_client):
    admin_client.post("/keys/llm", data={"name": "OR", "base_url": "https://openrouter.ai/api/v1",
                                         "api_key": "sk-or-v1-abc0000000000", "make_active": "1"})
    db = new_session()
    kid = db.scalar(select(Credential.id))
    db.close()
    admin_client.post(f"/keys/{kid}/edit", data={"name": "OR", "base_url": "https://evil.example/v1"})
    db = new_session()
    assert db.get(Credential, kid).base_url == "https://openrouter.ai/api/v1"
    db.close()
    admin_client.post(f"/keys/{kid}/edit", data={"name": "OR", "base_url": "https://other.example/v1",
                                                 "api_key": "sk-nieuwe-sleutel-000000"})
    db = new_session()
    assert db.get(Credential, kid).base_url == "https://other.example/v1"
    db.close()


def test_short_csrf_cookie_is_replaced(client):
    client.cookies.set("sc_csrf", "kort")
    r = client.get("/login")
    assert len(r.cookies.get("sc_csrf") or "") >= 20


def test_big_anonymous_body_refused(client):
    r = client.post("/login", content=b"x", headers={"content-length": str(50 * 1024 * 1024),
                                                      "content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_calibration_slope_is_bounded():
    from app.estimation.calibration import Point, apply, fit

    pts = [Point(score=5.0, baseline=100, actual=100) for _ in range(19)] + [Point(score=5.1, baseline=100, actual=1_000_000)]
    cal = fit(pts)
    mid, _, _ = apply(cal, 9.0, 100)
    assert math.isfinite(mid) and mid < 1e7


def test_dt_filter_shows_local_time():
    from datetime import datetime

    from datetime import timezone

    from app.api.common import fmt_dt
    from app.api.youtube import local_tz

    want = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).astimezone(local_tz()).strftime("%d-%m-%Y %H:%M")
    assert fmt_dt(datetime(2026, 7, 1, 12, 0)) == want


def test_unicode_compilation_title_download_header():
    from app.api.downloads import attachment

    h = attachment("Łódź-中文-top.mp4")["Content-Disposition"]
    h.encode("latin-1")


def test_mask_hyphenated_and_nan_subscore():
    from app.pipeline import pick, safety

    assert safety.mask("shit-faced!") == "s***-f****!"
    assert safety.mask("fucking!") == "f******!"
    assert pick._num(float("nan"), 5.0) == 5.0


def test_render_clip_unexpected_error_marks_failed(settings, monkeypatch, app):
    from app.pipeline import framing, run

    create_user("bob")
    pid, (cid,) = make_project_with_clip(settings, "bob")

    def boom(*a, **k):
        raise ValueError("cv2 kapot")

    monkeypatch.setattr(framing, "choose_framing", boom)
    db = new_session()
    clip = db.get(Clip, cid)
    clip.layout_json = None
    db.commit()
    run.render_clip(db, clip, settings.data_dir / "nergens.mp4", {"width": 640, "height": 360, "has_audio": True}, {"segments": []})
    clip = db.get(Clip, cid)
    assert clip.status == "failed" and "ValueError" in clip.render_error
    db.close()


def test_half_merged_download_not_used_as_source(settings):
    from app.core.paths import find_source, project_input

    d = project_input(99)
    d.mkdir(parents=True, exist_ok=True)
    (d / "source.f137.mp4").write_bytes(b"x" * 100)
    assert find_source(99) is None
    (d / "source.mp4").write_bytes(b"x" * 10)
    assert find_source(99).name == "source.mp4"


def test_lower_priority_uses_nice_prefix():
    from app.core import resources

    cmd = resources.lower_priority(["ffmpeg", "-version"])
    assert cmd[-2:] == ["ffmpeg", "-version"]
