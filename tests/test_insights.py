"""Leren van eigen resultaten: views ophalen, lessen trekken en meegeven aan de AI."""
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.db import new_session
from app.core.models import Clip, UserSettings
from app.estimation import insights
from app.pipeline import copywriting, pick
from app.pipeline.pick import LLMConfig
from tests.conftest import make_project_with_clip, user_id


def _post_all(settings, n, views=None):
    pid, cids = make_project_with_clip(settings, "admin", n_clips=n)
    db = new_session()
    for i, cid in enumerate(cids):
        c = db.get(Clip, cid)
        c.posted_platform = "YouTube Shorts"
        c.posted_url = f"https://youtube.com/shorts/VIDEO{i:06d}"
        c.posted_at = c.created_at
        c.est_mid_at_post = 1000
        if views is not None:
            c.actual_views = views[i]
            c.title = f"Titel {i}"
    db.commit()
    db.close()
    return pid, cids


def test_youtube_id():
    c = Clip(posted_url="https://youtube.com/shorts/abcdefghijk")
    assert insights.youtube_id(c) == "abcdefghijk"
    assert insights.youtube_id(Clip(posted_url="https://www.youtube.com/watch?v=abcdefghijk&t=3")) == "abcdefghijk"
    assert insights.youtube_id(Clip(posted_url="https://youtu.be/abcdefghijk")) == "abcdefghijk"
    assert insights.youtube_id(Clip(posted_url="https://tiktok.com/@x/video/1")) is None
    assert insights.youtube_id(Clip(yt_video_id="VIDEO123456", posted_url=None)) == "VIDEO123456"


def test_sync_views_with_api_key(admin_client, settings, monkeypatch):
    admin_client.post("/keys/youtube", data={"api_key": "AIzaSyTESTKEY0000000000000"})
    pid, cids = _post_all(settings, 2)
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("x-goog-api-key")
        ids = dict(request.url.params)["id"].split(",")
        return httpx.Response(200, json={"items": [
            {"id": vid, "statistics": {"viewCount": str(1000 * (k + 1)), "likeCount": str(10 * (k + 1)), "commentCount": "3"}}
            for k, vid in enumerate(ids)]})

    monkeypatch.setattr(insights, "_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    r = admin_client.post("/stats/sync")
    assert r.status_code == 303
    db = new_session()
    views = sorted(db.get(Clip, c).actual_views for c in cids)
    assert views == [1000, 2000] and db.get(Clip, cids[0]).likes in (10, 20)
    assert db.get(UserSettings, user_id("admin")).stats_synced_at is not None
    db.close()
    assert seen["key"] == "AIzaSyTESTKEY0000000000000"


def test_lessons_and_prompt_context(admin_client, settings, monkeypatch):
    # te weinig data
    _post_all(settings, 3, views=[100, 200, 300])
    db = new_session()
    ok, msg = insights.refresh_lessons(db, user_id("admin"), force=True)
    db.close()
    assert not ok and "te weinig" in msg
    assert "Nog te weinig data" in admin_client.get("/stats").text

    _post_all(settings, 4, views=[5000, 50, 900, 120])
    admin_client.post("/keys/llm", data={"name": "OR", "base_url": "https://openrouter.ai/api/v1", "model": "m",
                                         "api_key": "sk-or-v1-test000000000", "make_active": "1"})
    seen = {}

    def fake_chat(cfg, messages, **kw):
        seen["prompt"] = messages[1]["content"]
        return {"lessen": ["Clips met een directe reactie in de eerste seconde doen het best.",
                           "Korte clips (onder 25 s) presteren beter."]}

    monkeypatch.setattr(pick, "chat_json", fake_chat)
    r = admin_client.post("/stats/insights")
    assert r.status_code == 303
    data_lines = [l for l in seen["prompt"].split("\n") if l.startswith("- ")]
    assert data_lines[0].startswith("- 5000 views") and data_lines[-1].startswith("- 50 views")  # beste eerst
    page = admin_client.get("/stats").text
    assert "Wat de AI heeft geleerd" in page and "directe reactie" in page
    db = new_session()
    ctx = insights.context_for_prompt(db, user_id("admin"))
    db.close()
    assert ctx.startswith("Lessen:") and "Best presterend:" in ctx and "Slechtst presterend:" in ctx
    assert len(ctx) <= insights.MAX_CONTEXT


def test_insights_reach_the_prompts():
    p = pick._user_prompt("[0.0-1.0] hoi", 2, 10, 30, "en", insights="Lessen:\n- kort werkt")
    assert p.startswith("ERVARING VAN DIT KANAAL") and "kort werkt" in p
    assert "ERVARING" not in pick._user_prompt("[0.0-1.0] hoi", 2, 10, 30, "en")
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"clips": [
            {"id": 1, "title": "T", "description": "D", "hashtags": []}]})}}]})

    copywriting.generate_post_texts(LLMConfig("https://x/v1", "m", "k"), [{"id": 1, "text": "x", "title": "w"}],
                                    client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None,
                                    insights="Lessen:\n- vragen in de titel werken")
    assert "vragen in de titel werken" in seen["body"]["messages"][1]["content"]
