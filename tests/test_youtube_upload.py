"""YouTube-upload: koppelen via OAuth (nagebootst), uploaden, inplannen en foutafhandeling."""
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.core import crypto
from app.core.db import new_session
from app.core.models import Clip, YoutubeAccount, utcnow
from app.integrations import youtube_upload as yt
from app.worker.runner import run_job_once
from tests.conftest import create_user, login, make_client, make_project_with_clip

CLIENT_ID = "1234-abc.apps.googleusercontent.com"


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.put_status = 200
        self.privacy_back = "private"
        self.quota = False
        self.refreshed = 0

    def handler(self, request: httpx.Request):
        url = str(request.url)
        self.calls.append((request.method, url))
        if url.startswith(yt.TOKEN_URL):
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                self.refreshed += 1
                return httpx.Response(200, json={"access_token": "nieuw-token", "expires_in": 3600})
            assert form["code"] == ["goede-code"] and form["client_id"] == [CLIENT_ID]
            return httpx.Response(200, json={"access_token": "acc-token", "refresh_token": "ref-token-geheim", "expires_in": 3600})
        if url.startswith(yt.REVOKE_URL):
            return httpx.Response(200)
        if url.startswith(f"{yt.API}/channels"):
            return httpx.Response(200, json={"items": [{"id": "UCkanaal", "snippet": {"title": "Mijn Kanaal"}}]})
        if url.startswith(yt.UPLOAD_URL):
            if self.quota:
                return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})
            body = json.loads(request.content)
            self.last_meta = body
            return httpx.Response(200, headers={"Location": "https://upload.example/sessie123"})
        if url.startswith("https://upload.example/sessie123"):
            self.uploaded = request.read()
            return httpx.Response(self.put_status, json={"id": "VIDEO123", "status": {"privacyStatus": self.privacy_back}})
        return httpx.Response(404)


@pytest.fixture
def google(monkeypatch):
    g = FakeGoogle()
    monkeypatch.setattr(yt, "_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(g.handler)))
    return g


def _setup_oauth(admin):
    return admin.post("/keys/google-oauth", data={"client_id": CLIENT_ID, "client_secret": "GOCSPX-geheim-123"})


def _connect(c):
    r = c.get("/youtube/connect")
    assert r.status_code == 303 and r.headers["location"].startswith(yt.AUTH_URL)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["access_type"] == ["offline"] and "youtube.upload" in q["scope"][0]
    assert q["redirect_uri"] == ["http://testserver/youtube/callback"]
    return c.get(f"/youtube/callback?code=goede-code&state={q['state'][0]}")


def test_oauth_client_saved_encrypted(admin_client):
    assert _setup_oauth(admin_client).status_code == 303
    r = admin_client.post("/keys/google-oauth", data={"client_id": "fout", "client_secret": "x"})
    assert r.status_code == 303
    page = admin_client.get("/keys").text
    assert "GOCSPX-geheim-123" not in page and "http://testserver/youtube/callback" in page


def test_connect_state_check_and_tokens_encrypted(admin_client, google):
    _setup_oauth(admin_client)
    r = admin_client.get("/youtube/callback?code=goede-code&state=verkeerd")
    assert r.status_code == 303
    db = new_session()
    assert db.scalar(select(YoutubeAccount)) is None
    db.close()
    r = _connect(admin_client)
    assert r.status_code == 303
    db = new_session()
    acc = db.scalar(select(YoutubeAccount))
    assert acc.channel_title == "Mijn Kanaal" and b"ref-token-geheim" not in acc.refresh_token_enc
    assert crypto.decrypt_str(acc.refresh_token_enc) == "ref-token-geheim"
    db.close()
    page = admin_client.get("/account").text
    assert "Mijn Kanaal" in page and "ref-token" not in page
    admin_client.post("/youtube/disconnect")
    db = new_session()
    assert db.scalar(select(YoutubeAccount)) is None
    db.close()
    assert any(u.startswith(yt.REVOKE_URL) for _, u in google.calls)


def test_upload_clip_marks_posted(admin_client, settings, google, app):
    _setup_oauth(admin_client)
    _connect(admin_client)
    pid, (cid,) = make_project_with_clip(settings, "admin")
    page = admin_client.get(f"/projects/{pid}").text
    assert f'action="/clips/{cid}/youtube"' in page
    r = admin_client.post(f"/clips/{cid}/youtube", data={"title": "Mijn <titel>", "description": "Tekst #shorts", "privacy": "public"})
    assert r.status_code == 303
    assert run_job_once()
    db = new_session()
    c = db.get(Clip, cid)
    assert c.yt_status == "done" and c.yt_video_id == "VIDEO123"
    assert c.posted_platform == "YouTube Shorts" and c.posted_url == "https://youtube.com/shorts/VIDEO123" and c.est_mid_at_post
    assert "privé" in (c.yt_error or "")  # publiek gevraagd maar YouTube zette hem privé
    db.close()
    assert google.last_meta["snippet"]["title"] == "Mijn ‹titel›"
    assert google.last_meta["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    assert google.uploaded.startswith(b"FAKE-MP4-FINAL")
    # andere gebruiker kan deze clip niet uploaden
    create_user("eve")
    eve = make_client(app)
    login(eve, "eve")
    assert eve.post(f"/clips/{cid}/youtube", data={"title": "x"}).status_code == 404


def test_upload_quota_error(admin_client, settings, google):
    _setup_oauth(admin_client)
    _connect(admin_client)
    _, (cid,) = make_project_with_clip(settings, "admin")
    google.quota = True
    admin_client.post(f"/clips/{cid}/youtube", data={"title": "t", "privacy": "private"})
    run_job_once()
    db = new_session()
    c = db.get(Clip, cid)
    assert c.yt_status == "failed" and "quotum" in c.yt_error and c.posted_at is None
    db.close()


def test_upload_requires_connected_channel(admin_client, settings):
    _, (cid,) = make_project_with_clip(settings, "admin")
    assert admin_client.post(f"/clips/{cid}/youtube", data={"title": "t"}).status_code == 400


def test_schedule_and_token_refresh(admin_client, settings, google):
    _setup_oauth(admin_client)
    _connect(admin_client)
    db = new_session()
    acc = db.scalar(select(YoutubeAccount))
    acc.expires_at = utcnow() - timedelta(minutes=1)  # verlopen: moet vernieuwd worden
    db.commit()
    db.close()
    pid, cids = make_project_with_clip(settings, "admin", n_clips=2)
    start = (datetime.now() + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    r = admin_client.post(f"/projects/{pid}/youtube-schedule", data={
        "clip_ids": [str(c) for c in cids], "start": start.strftime("%Y-%m-%dT%H:%M"), "interval_hours": "24"})
    assert r.status_code == 303
    db = new_session()
    opts = [json.loads(db.get(Clip, c).yt_options_json) for c in cids]
    db.close()
    t1 = datetime.strptime(opts[0]["publish_at"], "%Y-%m-%dT%H:%M:%SZ")
    t2 = datetime.strptime(opts[1]["publish_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert t2 - t1 == timedelta(hours=24) and all(o["privacy"] == "private" for o in opts)
    run_job_once()
    assert google.refreshed == 1
    assert google.last_meta["status"]["publishAt"] == opts[0]["publish_at"]
    # te vroeg inplannen wordt geweigerd: er komt geen nieuwe upload bij
    db = new_session()
    before = db.get(Clip, cids[0]).yt_options_json
    db.close()
    admin_client.post(f"/projects/{pid}/youtube-schedule", data={"clip_ids": [str(cids[0])], "start": "2020-01-01T10:00"})
    db = new_session()
    assert db.get(Clip, cids[0]).yt_options_json == before
    db.close()
