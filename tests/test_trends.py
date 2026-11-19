"""Tags van wat nu werkt op YouTube, en het sterkste moment kort vooraan (teaser)."""
import subprocess

import httpx
import pytest

from app.core.db import new_session
from app.core.models import Clip
from app.estimation import trends
from app.pipeline import captions, media, render
from app.pipeline import run as run_mod


class FakeYT:
    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request):
        self.calls.append(request.url.path)
        q = dict(request.url.params)
        if request.url.path.endswith("/search"):
            assert q["order"] == "viewCount" and q["type"] == "video"
            return httpx.Response(200, json={"items": [{"id": {"videoId": f"V{i}"}} for i in range(3)]})
        if request.url.path.endswith("/videos"):
            return httpx.Response(200, json={"items": [
                {"id": "V0", "snippet": {"title": "SPEED CRASHES IN GTA 6 #gta6 #speed", "description": "#ishowspeed",
                                         "tags": ["gta 6", "ishowspeed", "speed"]}, "statistics": {"viewCount": "2000000"}},
                {"id": "V1", "snippet": {"title": "gta 6 funny moments", "description": "",
                                         "tags": ["gta 6", "funny moments"]}, "statistics": {"viewCount": "50000"}},
                {"id": "V2", "snippet": {"title": "random", "tags": ["x" * 40]}, "statistics": {"viewCount": "10"}},
            ]})
        return httpx.Response(404)


@pytest.fixture
def fake_yt(monkeypatch):
    f = FakeYT()
    monkeypatch.setattr(trends, "_client_factory", lambda: httpx.Client(transport=httpx.MockTransport(f.handler)))
    trends._cache.clear()
    return f


def test_summary_and_tags():
    s = trends.summarize([
        {"snippet": {"title": "A #gta6", "tags": ["GTA 6", "speed"]}, "statistics": {"viewCount": "1000000"}},
        {"snippet": {"title": "B", "tags": ["gta 6", "other"]}, "statistics": {"viewCount": "100"}},
    ])
    assert s["tags"][0] == "GTA 6" and s["hashtags"] == ["#gta6"] and s["titles"][0][0] == "A #gta6"
    assert trends.normalize_tags("#gta 6, GTA 6, speed,, " + "y" * 31) == "gta 6, speed"
    long = ", ".join(f"tag nummer {i}" for i in range(60))
    assert len(trends.normalize_tags(long)) <= trends.TAGS_MAX_CHARS
    assert trends.upload_tags("gta 6, speed", "#shorts #gta6 #speed") == ["gta 6", "speed", "shorts", "gta6"]
    assert trends.query_from_meta({"game": "Grand Theft Auto VI", "streamer": "Speed"}) == "Grand Theft Auto VI Speed"
    assert trends.query_from_meta({}, "een twee drie vier vijf zes zeven") == "een twee drie vier vijf zes"


def test_fetch_uses_key_and_caches(admin_client, fake_yt):
    admin_client.post("/keys/youtube", data={"api_key": "AIzaSyTESTKEY0000000000000"})
    db = new_session()
    from tests.conftest import user_id

    uid = user_id("admin")
    tr = trends.fetch(db, uid, "gta 6 speed")
    assert tr["tags"][0] == "gta 6" and "#gta6" in tr["hashtags"] and tr["n"] == 3
    assert all(len(t) <= 30 for t in tr["tags"])
    n = len(fake_yt.calls)
    assert trends.fetch(db, uid, "GTA 6 speed") == tr and len(fake_yt.calls) == n  # 24 uur bewaard
    text = trends.prompt_text(tr)
    assert "WAT NU WERKT OP YOUTUBE" in text and "NIET overnemen" in text
    # uit te zetten bij Instellingen
    from app.core.store import set_global

    set_global(db, "trend_tags", "0")
    db.commit()
    trends._cache.clear()
    assert trends.fetch(db, uid, "gta 6 speed") is None
    db.close()


def test_post_text_gets_trends_and_returns_tags():
    from app.pipeline import copywriting, pick

    seen = {}

    def fake_chat(cfg, messages, **kw):
        seen["prompt"] = messages[1]["content"]
        return {"clips": [{"id": 7, "title": "T", "description": "D", "hashtags": ["#gta6"],
                           "tags": ["gta 6", "#speed", "ishowspeed"]}]}

    orig = pick.chat_json
    pick.chat_json = fake_chat
    try:
        out = copywriting.generate_post_texts(pick.LLMConfig("u", "m", "k"), [{"id": 7, "text": "hoi"}],
                                              trends="WAT NU WERKT OP YOUTUBE voor 'gta'")
    finally:
        pick.chat_json = orig
    assert seen["prompt"].startswith("WAT NU WERKT") and "TAGS:" in seen["prompt"]
    assert out[7]["tags"] == "gta 6, speed, ishowspeed"


@pytest.fixture(scope="module")
def loud_video(tmp_path_factory):
    """12 s video: zacht geluid met een harde piek tussen 7 en 8 s."""
    p = tmp_path_factory.mktemp("teaser") / "loud.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=12",
                    "-f", "lavfi", "-i",
                    "aevalsrc=0.02*sin(2*PI*440*t)+if(between(t\\,7\\,8)\\,0.8*sin(2*PI*880*t)\\,0):s=44100:d=12",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(p)],
                   check=True)
    return p


def test_loudest_moment_and_teaser(loud_video, tmp_path):
    peak = media.loudest_moment(loud_video, 0.0, 12.0)
    assert 6.8 <= peak <= 8.2
    clip = Clip(start=0.0, end=12.0, teaser=True, hook_at=None)
    win = run_mod.teaser_window(clip, loud_video, {"has_audio": True})
    assert win is not None and win[0] < peak < win[1] and abs((win[1] - win[0]) - run_mod.TEASER_LEN) < 0.01
    assert clip.hook_at == peak
    # piek van de AI gaat voor
    clip2 = Clip(start=0.0, end=12.0, teaser=True, hook_at=4.0)
    assert run_mod.teaser_window(clip2, loud_video, {"has_audio": True}) == (3.0, 4.8)
    # piek vooraan of uitgezet: geen teaser
    assert run_mod.teaser_window(Clip(start=0.0, end=12.0, teaser=True, hook_at=2.5), loud_video, {}) is None
    assert run_mod.teaser_window(Clip(start=0.0, end=12.0, teaser=False), loud_video, {}) is None
    out = render.add_teaser(loud_video, win[0], win[1], tmp_path / "t.mp4")
    info = media.probe(out)
    assert abs(info["duration"] - (12.0 + run_mod.TEASER_LEN)) < 0.3 and info["has_audio"]


def test_captions_shift_with_teaser():
    words = [{"start": 1.0, "end": 1.5, "word": "Hallo"}, {"start": 1.6, "end": 2.0, "word": "daar"}]
    plain = captions.build_ass(words, 0.0, 5.0)
    shifted = captions.build_ass(words, 0.0, 5.0, offset=1.8)
    assert "0:00:01.00" in plain and "0:00:02.80" in shifted
    assert "00:00:02,800" in captions.build_srt(words, 0.0, 5.0, offset=1.8)


def test_teaser_toggle_and_tags_saved(admin_client, settings):
    from tests.conftest import make_project_with_clip

    pid, (cid,) = make_project_with_clip(settings, "admin")
    r = admin_client.post(f"/clips/{cid}/retitle", data={"title": "T", "yt_tags": "#gta 6, speed, speed"})
    assert r.status_code == 303
    admin_client.post(f"/clips/{cid}/teaser", data={"enabled": "1"})
    db = new_session()
    c = db.get(Clip, cid)
    assert c.yt_tags == "gta 6, speed" and c.teaser
    db.close()
    page = admin_client.get(f"/clips/{cid}/panel").text
    assert "Teaser vooraan uitzetten" in page and "gta 6, speed" in page
