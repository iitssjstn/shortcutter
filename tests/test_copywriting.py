import json

import httpx
import pytest
from sqlalchemy import select

from app.core.db import new_session
from app.core.models import Clip
from app.pipeline import copywriting as cw
from app.pipeline import pick
from app.pipeline.errors import LLMError
from app.pipeline.pick import LLMConfig
from tests.conftest import make_project_with_clip


def test_normalize_hashtags():
    assert cw.normalize_hashtags(["motivation", "#Gym", "gym", "#mind set!"]) == "#motivation #Gym #mindset #shorts"
    assert cw.normalize_hashtags("#a, #b #shorts") == "#a #b #shorts"
    assert cw.normalize_hashtags(None) == "#shorts"
    assert len(cw.normalize_hashtags([f"t{i}" for i in range(20)]).split()) == cw.MAX_TAGS


def test_clean_title():
    assert cw.clean_title('"Why nobody starts"', "x") == "Why nobody starts"
    assert cw.clean_title("", "fallback") == "fallback"
    assert len(cw.clean_title("a" * 300, "x")) == cw.TITLE_MAX


def _llm(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_prompt_asks_english_by_default():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"clips": [
            {"id": 7, "title": "Why nobody starts", "description": "Do you agree?", "hashtags": ["motivation"]}]})}}]})

    out = cw.generate_post_texts(LLMConfig("https://x/v1", "m", "k"), [{"id": 7, "text": "Waarom begint niemand?", "title": "w"}],
                                 client=_llm(handler), sleep=lambda s: None)
    assert out[7] == {"title": "Why nobody starts", "description": "Do you agree?", "hashtags": "#motivation #shorts"}
    assert "ENGELS" in seen["body"]["messages"][1]["content"]
    assert "NEDERLANDS" in cw.copy_rules("nl")


def test_pick_prompt_contains_language_and_parses_post_fields():
    t = {"duration": 60, "segments": [{"start": i * 3.0, "end": i * 3.0 + 2.8, "text": f"Zin {i}.",
          "words": [{"start": i * 3.0, "end": i * 3.0 + 2.8, "word": f"Zin{i}."}]} for i in range(20)]}
    seen = {}

    def handler(request):
        seen["prompt"] = json.loads(request.content)["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"clips": [
            {"start": 0, "end": 20, "title": "Big title", "description": "Desc", "hashtags": ["#a"],
             "hook_sterkte": 7, "emotie_of_verrassing": 7, "begrijpelijkheid_zonder_context": 7, "afronding": 7}]})}}]})

    clips = pick.llm_pick(t, 2, 10, 30, LLMConfig("https://x/v1", "m", "k"), client=_llm(handler),
                          sleep=lambda s: None, post_language="en")
    assert "ENGELS" in seen["prompt"]
    assert clips[0]["title"] == "Big title" and clips[0]["description"] == "Desc" and clips[0]["hashtags"] == "#a #shorts"


def test_generate_buttons(admin_client, settings, monkeypatch):
    pid, cids = make_project_with_clip(settings, "admin", n_clips=2)
    # zonder sleutel: nette melding, niets kapot
    r = admin_client.post(f"/projects/{pid}/generate-texts")
    assert r.status_code == 303
    admin_client.post("/keys/llm", data={"name": "OR", "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini",
                                         "api_key": "sk-or-v1-test000000000", "make_active": "1"})

    def fake_chat(cfg, messages, **kw):
        ids = [int(x.split()[1]) for x in messages[1]["content"].split("\n") if x.startswith("CLIP ")]
        return {"clips": [{"id": i, "title": f"English title {i}", "description": "Would you try this?",
                           "hashtags": ["motivation", "shorts"]} for i in ids]}

    monkeypatch.setattr(pick, "chat_json", fake_chat)
    assert admin_client.post(f"/projects/{pid}/generate-texts").status_code == 303
    db = new_session()
    clips = db.scalars(select(Clip).where(Clip.project_id == pid).order_by(Clip.id)).all()
    assert [c.title for c in clips] == [f"English title {cids[0]}", f"English title {cids[1]}"]
    assert clips[0].post_text == "Would you try this?\n\n#motivation #shorts"
    db.close()
    page = admin_client.get(f"/projects/{pid}").text
    assert "Would you try this?" in page and 'data-copy="ptitle-' in page

    def failing(*a, **k):
        raise LLMError("serverfout (500)")

    monkeypatch.setattr(pick, "chat_json", failing)
    r = admin_client.post(f"/clips/{cids[0]}/generate-text")
    assert r.status_code == 303
    # handmatig bewerken van beschrijving en hashtags
    admin_client.post(f"/clips/{cids[0]}/retitle", data={"title": "Eigen titel", "description": "Mijn tekst", "hashtags": "gym fit"})
    db = new_session()
    c = db.get(Clip, cids[0])
    assert (c.title, c.description, c.hashtags) == ("Eigen titel", "Mijn tekst", "#gym #fit #shorts")
    db.close()


def test_settings_post_language(admin_client):
    r = admin_client.post("/settings", data={"channel_avg_views": "1000", "default_num_clips": "3", "default_min_dur": "20",
                                             "default_max_dur": "50", "caption_style": "geel", "framing_default": "auto",
                                             "post_language": "nl"})
    assert r.status_code == 303
    assert 'value="nl" selected' in admin_client.get("/settings").text
