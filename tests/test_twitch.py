"""Twitch: links, sleutel, kanalen (alleen eigen/met toestemming) en clips importeren."""
import json
import shutil

import httpx
import pytest
from sqlalchemy import select

from app.core import crypto
from app.core.db import new_session
from app.core.models import Clip, Credential, Project, TwitchChannel
from app.integrations import twitch
from app.pipeline import download as download_mod
from app.pipeline import run as run_mod
from app.worker.runner import run_job_once
from tests.conftest import create_user, login, make_client, user_id

SECRET = "twitchsecret0123456789abcdef"


class FakeTwitch:
    """Nagebootste Twitch-API (token + helix users/clips)."""

    def __init__(self):
        self.calls = []
        self.token_status = 200
        self.clips = {
            "123": [
                {"id": "SlugEen-abc", "url": "https://clips.twitch.tv/SlugEen-abc", "title": "Insane 1v5 clutch",
                 "view_count": 5400, "duration": 28.4, "created_at": "2026-11-15T20:00:00Z", "creator_name": "fan1",
                 "broadcaster_id": "123", "broadcaster_name": "Streamer",
                 "thumbnail_url": "https://static-cdn.jtvnw.net/twitch-clips-thumbnails-prod/een.jpg"},
                {"id": "SlugTwee-def", "url": "https://clips.twitch.tv/SlugTwee-def", "title": "Funny fail",
                 "view_count": 12000, "duration": 15.0, "created_at": "2026-11-16T20:00:00Z", "creator_name": "fan2",
                 "broadcaster_id": "123", "broadcaster_name": "Streamer", "thumbnail_url": ""},
            ],
            "999": [
                {"id": "Vreemd-xyz", "url": "https://clips.twitch.tv/Vreemd-xyz", "title": "Ander kanaal",
                 "view_count": 1, "duration": 10, "broadcaster_id": "999"},
            ],
        }

    def handler(self, request: httpx.Request):
        self.calls.append(str(request.url))
        if request.url.host == "id.twitch.tv":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"message": "invalid client"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        assert request.headers["client-id"] == "clientid123"
        path = request.url.path
        q = dict(request.url.params)
        if path.endswith("/users"):
            if q["login"] == "bestaatniet":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"data": [{"id": "123", "login": q["login"], "display_name": "Streamer",
                                                       "profile_image_url": "https://static-cdn.jtvnw.net/p.png"}]})
        if path.endswith("/search/categories"):
            if "gta" in q["query"].lower():
                return httpx.Response(200, json={"data": [
                    {"id": "777", "name": "Grand Theft Auto VI", "box_art_url": "https://static-cdn.jtvnw.net/ttv-boxart/777-{width}x{height}.jpg"},
                    {"id": "32982", "name": "Grand Theft Auto V", "box_art_url": ""}]})
            return httpx.Response(200, json={"data": []})
        if path.endswith("/games"):
            return httpx.Response(200, json={"data": [{"id": q["id"], "name": "Grand Theft Auto VI"}]})
        if path.endswith("/clips") and "game_id" in q:
            return httpx.Response(200, json={"data": [
                dict(self.clips["123"][0], game_id=q["game_id"]),
                {"id": "Vreemd-xyz", "url": "https://clips.twitch.tv/Vreemd-xyz", "title": "Andere streamer",
                 "view_count": 99000, "duration": 20, "broadcaster_id": "999", "broadcaster_name": "AndereStreamer",
                 "created_at": "2026-11-16T10:00:00Z", "game_id": q["game_id"]},
            ]})
        if path.endswith("/clips"):
            if "id" in q:
                for lst in self.clips.values():
                    for c in lst:
                        if c["id"] == q["id"]:
                            return httpx.Response(200, json={"data": [c]})
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"data": self.clips.get(q["broadcaster_id"], [])})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    f = FakeTwitch()
    monkeypatch.setattr(twitch, "_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    twitch._tokens.clear()
    twitch._cache.clear()
    return f


def _add_key(admin):
    return admin.post("/keys/twitch", data={"client_id": "clientid123", "client_secret": SECRET})


def test_twitch_urls():
    ok = ["https://clips.twitch.tv/SlugEen-abc", "https://www.twitch.tv/streamer/clip/SlugEen-abc",
          "https://twitch.tv/videos/123456789", "https://m.twitch.tv/streamer/clip/Abc_d-1"]
    for u in ok:
        assert download_mod.url_platform(u) == "twitch", u
    assert download_mod.url_platform("https://www.twitch.tv/streamer") is None  # live kanaal: geen video
    assert download_mod.url_platform("https://evil.example/twitch.tv/x/clip/y") is None
    assert download_mod.twitch_clip_slug("https://www.twitch.tv/streamer/clip/SlugEen-abc?x=1") == "SlugEen-abc"
    assert twitch.twitch_credit("https://www.twitch.tv/Streamer/clip/abc") == "twitch.tv/streamer"


def test_key_saved_encrypted_and_tested(admin_client, fake):
    assert _add_key(admin_client).status_code == 303
    db = new_session()
    cred = db.scalar(select(Credential).where(Credential.provider == "twitch"))
    assert SECRET.encode() not in cred.encrypted_value
    assert json.loads(crypto.decrypt_str(cred.encrypted_value))["client_secret"] == SECRET
    kid = cred.id
    db.close()
    page = admin_client.get("/keys").text
    assert SECRET not in page and "clientid123" not in page
    r = admin_client.post(f"/keys/{kid}/test", headers={"HX-Request": "true"})
    assert "Sleutel werkt" in r.text
    fake.token_status = 403
    twitch._tokens.clear()
    r = admin_client.post(f"/keys/{kid}/test", headers={"HX-Request": "true"})
    assert "weigert" in r.text


def test_channel_requires_confirmation_and_is_personal(two_users, fake):
    _add_key(two_users["admin"])
    alice, bob = two_users["alice"], two_users["bob"]
    r = alice.post("/twitch/channels", data={"login": "streamer", "relation": "toestemming"})
    assert r.status_code == 303
    db = new_session()
    assert db.scalar(select(TwitchChannel)) is None  # zonder bevestiging niet toegevoegd
    db.close()
    alice.post("/twitch/channels", data={"login": "bestaatniet", "confirm": "1"})
    alice.post("/twitch/channels", data={"login": "https://www.twitch.tv/Streamer", "confirm": "1", "note": "akkoord via Discord"})
    db = new_session()
    ch = db.scalar(select(TwitchChannel))
    assert (ch.login, ch.broadcaster_id, ch.owner_id) == ("streamer", "123", user_id("alice"))
    db.close()
    page = alice.get("/twitch").text
    assert "Insane 1v5 clutch" in page and "akkoord via Discord" in page
    # top-clips op views gesorteerd
    assert page.index("Funny fail") < page.index("Insane 1v5 clutch")
    # bob ziet alice's kanaal niet en kan er niets mee
    assert "Insane 1v5 clutch" not in bob.get(f"/twitch?channel={ch.id}").text
    assert bob.post(f"/twitch/channels/{ch.id}/delete").status_code == 404
    r = bob.post("/twitch/import", data={"channel_id": str(ch.id), "urls": "https://clips.twitch.tv/SlugEen-abc"})
    assert r.status_code == 404


def test_import_only_clips_of_that_channel(admin_client, fake):
    _add_key(admin_client)
    admin_client.post("/twitch/channels", data={"login": "streamer", "confirm": "1", "relation": "eigen"})
    db = new_session()
    ch = db.scalar(select(TwitchChannel))
    db.close()
    r = admin_client.post("/twitch/import", data={
        "channel_id": str(ch.id), "framing": "split",
        "urls": ["https://clips.twitch.tv/SlugEen-abc", "https://clips.twitch.tv/Vreemd-xyz"]})
    assert r.status_code == 303
    db = new_session()
    projects = db.scalars(select(Project)).all()
    assert len(projects) == 1
    p = projects[0]
    assert (p.source_type, p.source_url, p.title, p.credit, p.framing) == (
        "twitch", "https://clips.twitch.tv/SlugEen-abc", "Insane 1v5 clutch", "twitch.tv/streamer", "split")
    db.close()
    assert "al omgezet" in admin_client.get(f"/twitch?channel={ch.id}").text


def test_twitch_project_pipeline_single_clip_with_credit(admin_client, settings, sample_video, monkeypatch, fake):
    """Korte clip wordt in zijn geheel één short; posttekst krijgt de credit."""
    from tests.test_pipeline_run import _fake_transcribe

    def fake_download(url, out_dir, cookies):
        assert cookies is None and "twitch.tv" in url
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / "source.mp4"
        shutil.copy(sample_video, dest)
        return dest, {"title": "van yt-dlp"}

    monkeypatch.setattr(run_mod, "download", fake_download)
    monkeypatch.setattr(run_mod, "transcribe", _fake_transcribe)
    r = admin_client.post("/projects", data={"url": "https://www.twitch.tv/streamer/clip/SlugEen-abc",
                                             "num_clips": "3", "min_dur": "20", "max_dur": "55"})
    assert r.headers["location"].startswith("/projects/")
    run_job_once()
    db = new_session()
    p = db.scalars(select(Project)).one()
    assert p.status == "done" and p.source_type == "twitch" and p.credit == "twitch.tv/streamer"
    clips = db.scalars(select(Clip).where(Clip.project_id == p.id)).all()
    assert len(clips) == 1 and clips[0].start == 0.0 and clips[0].end == pytest.approx(8.0, abs=0.2)
    assert clips[0].source == "volledig" and clips[0].status == "ready"
    assert "🎥 twitch.tv/streamer" in clips[0].post_text
    db.close()


def test_twitch_page_without_key(admin_client):
    page = admin_client.get("/twitch").text
    assert "nog geen Twitch API-sleutel" in page


def test_csp_allows_twitch_thumbnails(admin_client):
    csp = admin_client.get("/twitch").headers["content-security-policy"]
    assert "https://static-cdn.jtvnw.net" in csp


def test_search_by_game_and_import_only_permitted(admin_client, fake):
    _add_key(admin_client)
    page = admin_client.get("/twitch?view=zoeken&q=GTA").text
    assert "Grand Theft Auto VI" in page and "game=777" in page
    page = admin_client.get("/twitch?view=zoeken&q=GTA&game=777&period=week").text
    assert "Grand Theft Auto VI · meest bekeken clips" in page
    assert "AndereStreamer" in page and "Insane 1v5 clutch" in page
    # nog geen kanalen: geen selectievakjes, wel 'kanaal toevoegen'
    assert 'name="urls"' not in page and "add=anderestreamer" in page
    # kanaal 'streamer' toevoegen (met toestemming): die clip wordt selecteerbaar
    admin_client.post("/twitch/channels", data={"login": "streamer", "confirm": "1"})
    page = admin_client.get("/twitch?view=zoeken&q=GTA&game=777&period=week").text
    assert 'value="https://clips.twitch.tv/SlugEen-abc"' in page
    assert 'value="https://clips.twitch.tv/Vreemd-xyz"' not in page
    # importeren zonder vast kanaal: alleen de toegestane clip
    r = admin_client.post("/twitch/import", data={
        "back": "/twitch?view=zoeken&game=777", "framing": "split",
        "urls": ["https://clips.twitch.tv/SlugEen-abc", "https://clips.twitch.tv/Vreemd-xyz"]})
    assert r.status_code == 303
    db = new_session()
    urls = [p.source_url for p in db.scalars(select(Project))]
    db.close()
    assert urls == ["https://clips.twitch.tv/SlugEen-abc"]


def test_add_channel_prefilled_from_search(admin_client, fake):
    _add_key(admin_client)
    page = admin_client.get("/twitch?view=zoeken&add=anderestreamer").text
    assert 'value="anderestreamer"' in page
