"""Nakijk-lijst, aanbevolen uploadmomenten, autopilot en Discord-meldingen."""
import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core.db import new_session
from app.core.models import AutoPilot, Clip, Project, UserSettings, utcnow
from app.estimation import timing
from app.integrations import discord
from app.pipeline import autopilot
from tests.conftest import make_project_with_clip, user_id
from tests.test_twitch import _add_key, fake  # noqa: F401
from tests.test_youtube_upload import _connect, _setup_oauth, google  # noqa: F401

HOOK = "https://discord.com/api/webhooks/123456789012/abcdefghijklmnopqrstuvwxyz_ABCDEFG"


class FakeDiscord:
    def __init__(self):
        self.sent = []

    def handler(self, request):
        self.sent.append(json.loads(request.content))
        return httpx.Response(204)


@pytest.fixture
def dc(monkeypatch):
    f = FakeDiscord()
    monkeypatch.setattr(discord, "_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    return f


def test_slots_defaults_gap_and_per_day(admin_client, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    db = new_session()
    uid = user_id("admin")
    now = datetime(2026, 11, 23, 9, 0)  # maandag 10:00 Amsterdam
    slots, why = timing.next_slots(db, uid, 5, now)
    assert "Engelstalig" in why
    assert [s.strftime("%d %H") for s in slots] == ["23 18", "23 21", "24 18", "24 21", "25 18"]  # 23:00 te dicht op 21:00
    us = db.get(UserSettings, uid)
    us.uploads_per_day, us.min_gap_hours = 1, 3
    db.commit()
    slots, _ = timing.next_slots(db, uid, 3, now)
    assert [s.strftime("%d %H") for s in slots] == ["23 18", "24 18", "25 18"]
    db.close()


def test_slots_from_own_results(admin_client, settings, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    pid, cids = make_project_with_clip(settings, "admin", 12)
    db = new_session()
    for i, cid in enumerate(cids):
        c = db.get(Clip, cid)
        hour_utc = 13 if i % 2 else 6  # 14:00 of 07:00 Amsterdam
        c.posted_at = datetime(2026, 11, 1 + i, hour_utc, 0)
        c.actual_views = 5000 if i % 2 else 200
    db.commit()
    hours, why = timing.best_hours(db, user_id("admin"))
    assert 13 in hours and "eigen 12 clips" in why
    assert "12:00–15:00" in why
    db.close()


def test_review_list_skip_and_schedule(admin_client, settings, google, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    pid, cids = make_project_with_clip(settings, "admin", 3)
    page = admin_client.get("/review").text
    assert page.count('class="card rv-item"') == 3 and "aanbevolen" in page and "Koppel eerst" in page
    r = admin_client.post(f"/review/{cids[0]}", data={"action": "skip"}, follow_redirects=True)
    assert "overgeslagen" in r.text and r.text.count('class="card rv-item"') == 2
    _setup_oauth(admin_client)
    _connect(admin_client)
    when = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    r = admin_client.post(f"/review/{cids[1]}", data={"action": "schedule", "publish_at": when, "title": "Nieuwe titel",
                                                     "yt_tags": "gta 6, speed"}, follow_redirects=True)
    assert "ingepland voor" in r.text
    db = new_session()
    c = db.get(Clip, cids[1])
    opts = json.loads(c.yt_options_json)
    assert c.yt_status == "queued" and opts["privacy"] == "private" and opts["publish_at"] and c.title == "Nieuwe titel"
    assert c.yt_tags == "gta 6, speed"
    db.close()
    # bulk: de rest op aanbevolen momenten
    r = admin_client.post("/review-bulk", data={"clip_ids": [str(cids[2])]}, follow_redirects=True)
    assert "1 clip(s) ingepland" in r.text and "Niets om na te kijken" in r.text


def test_autopilot_run(admin_client, fake, dc):
    c = admin_client
    _add_key(c)
    c.post("/twitch/channels", data={"login": "streamer", "confirm": "1"})
    db = new_session()
    from app.core.models import TwitchChannel

    ch = db.scalar(select(TwitchChannel))
    db.close()
    c.post("/account/discord", data={"webhook": HOOK})
    assert dc.sent[-1]["embeds"][0]["title"] == "Shortcutter is gekoppeld"
    r = c.post("/autopilot", data={"enabled": "1", "channel_ids": [str(ch.id)], "every_hours": "12", "period": "week",
                                   "max_per_run": "5", "min_views": "6000", "run_now": "1"}, follow_redirects=True)
    assert "1 nieuwe clip(s)" in r.text  # alleen de clip met 12.000 views haalt de drempel
    assert dc.sent[-1]["embeds"][0]["title"].startswith("Autopilot: 1 nieuwe")
    db = new_session()
    assert db.scalar(select(func.count()).select_from(Project)) == 1
    ap = db.scalar(select(AutoPilot))
    assert ap.enabled and not autopilot.is_due(ap)
    assert autopilot.is_due(ap, utcnow() + timedelta(hours=13))
    ap.min_views = 0
    db.commit()
    made = autopilot.run(db, ap)
    assert len(made) == 1  # de al omgezette clip wordt overgeslagen
    assert autopilot.run(db, ap) == [] and "Geen nieuwe clips" in ap.last_result
    db.close()


def test_discord_webhook_secret_and_digest(admin_client, settings, dc):
    assert discord.valid_webhook(HOOK) and not discord.valid_webhook("https://evil.example/api/webhooks/1/x")
    pid_old, _ = make_project_with_clip(settings, "admin", 2)
    r = admin_client.post("/account/discord", data={"webhook": "https://example.com/hook"}, follow_redirects=True)
    assert "geen geldige" in r.text
    admin_client.post("/account/discord", data={"webhook": HOOK})
    page = admin_client.get("/account").text
    assert "abcdefghijklmnop" not in page and "discord.com/api/webhooks/1234…" in page
    db = new_session()
    us = db.get(UserSettings, user_id("admin"))
    assert HOOK.encode() not in us.discord_webhook_enc
    # bestaande clips worden niet alsnog gemeld; nieuwe wel, in één bericht
    n_before = len(dc.sent)
    assert discord.digest_ready_clips(db) == 0
    db.close()
    make_project_with_clip(settings, "admin", 3)
    db = new_session()
    assert discord.digest_ready_clips(db) == 1
    msg = dc.sent[-1]["embeds"][0]
    assert msg["title"] == "3 nieuwe clip(s) klaar om na te kijken" and len(dc.sent) == n_before + 1
    assert dc.sent[-1]["allowed_mentions"] == {"parse": []}
    assert discord.digest_ready_clips(db) == 0  # niet nog een keer
    db.close()
    admin_client.post("/account/discord", data={"remove": "1"})
    db = new_session()
    assert db.get(UserSettings, user_id("admin")).discord_webhook_enc is None
    db.close()
