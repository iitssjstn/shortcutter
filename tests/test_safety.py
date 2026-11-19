"""Familievriendelijke modus: scheldwoorden piepen/maskeren en ongeschikte beelden vervagen."""
import json

from app.core.db import new_session
from app.core.models import Clip, UserSettings
from app.pipeline import media, safety
from app.pipeline import run as run_mod
from tests.conftest import make_project_with_clip, user_id
from tests.test_trends import loud_video  # noqa: F401 (fixture)


def test_profanity_detection_and_mask():
    for w in ["fuck", "Fucking!", "shit,", "kanker", "Tering", "klootzak", "WTF"]:
        assert safety.is_profane(w), w
    for w in ["cockpit", "hoera", "classic", "hello", "Speed", "fun"]:
        assert not safety.is_profane(w), w
    assert safety.mask("fucking!") == "f******!" and safety.mask("Kanker") == "K*****"
    words = [{"start": 1.0, "end": 1.4, "word": " fuck"}, {"start": 1.5, "end": 1.8, "word": " yes"}]
    assert [w["word"] for w in safety.censor_words(words)] == [" f***", " yes"]


def test_mute_intervals_with_teaser():
    words = [{"start": 5.0, "end": 5.4, "word": "shit"}, {"start": 8.0, "end": 8.3, "word": "nice"}]
    assert safety.mute_intervals(words, 2.0, 12.0) == [(2.95, 3.45)]
    # teaser [2.5, 4.3] (cliptijd) staat vooraan: het woord klinkt zowel in de teaser als in de clip
    iv = safety.mute_intervals(words, 2.0, 12.0, teaser=(2.5, 4.3))
    assert iv == [(0.45, 0.95), (4.75, 5.25)]


def test_censor_and_frame_check(loud_video, tmp_path, monkeypatch):
    from app.pipeline import pick

    def fake_chat(cfg, messages, **kw):
        n = sum(1 for p in messages[1]["content"] if p.get("type") == "image_url")
        return {"frames": [{"i": i, "probleem": "naakt" if i == 2 else "geen"} for i in range(n)]}

    monkeypatch.setattr(pick, "chat_json", fake_chat)
    flags = safety.check_frames(pick.LLMConfig("u", "m", "k"), loud_video, 12.0)
    assert len(flags) == 1 and flags[0][1] == "naakt"
    blur = safety.blur_intervals(flags, 12.0)
    out = safety.censor(loud_video, tmp_path / "c.mp4", [(7.0, 8.0)], blur, has_audio=True)
    info = media.probe(out)
    assert abs(info["duration"] - 12.0) < 0.3 and info["has_audio"]
    # alleen piepen: beeld wordt niet opnieuw gecodeerd
    out2 = safety.censor(loud_video, tmp_path / "d.mp4", [(1.0, 1.5)], [], has_audio=True)
    assert media.probe(out2)["width"] == 640


def test_apply_safety_summary(loud_video, tmp_path, monkeypatch, settings, admin_client):
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(loud_video.read_bytes())
    monkeypatch.setattr(run_mod, "load_llm_config", lambda db, task: object())
    monkeypatch.setattr(safety, "check_frames", lambda cfg, v, d: [(t, "naakt") for t in range(1, 11)])
    db = new_session()
    us = db.get(UserSettings, user_id("admin"))
    us.safe_mode, us.safe_visual = True, True
    clip = Clip(start=0.0, end=12.0)
    words = [{"start": 3.0, "end": 3.3, "word": "fuck"}]
    notes = run_mod.apply_safety(db, clip, raw, words, None, us)
    assert notes["muted"] == 1 and notes["checked"] and notes["reasons"] == ["naakt"]
    assert notes["blurred"] and "groot deel" in notes["warning"]
    db.close()


def test_downloads_and_prompts_family(admin_client, settings):
    from app.pipeline import copywriting, pick

    pid, (cid,) = make_project_with_clip(settings, "admin")
    out = settings.output_dir / str(pid) / "transcript.json"
    t = json.loads(out.read_text())
    t["segments"][0]["words"][1]["word"] = "shit"
    out.write_text(json.dumps(t))
    assert "shit" in admin_client.get(f"/clips/{cid}/subtitles.srt").text
    r = admin_client.post("/settings", data={"channel_avg_views": "1000", "default_num_clips": "5", "default_min_dur": "20",
                                             "default_max_dur": "55", "safe_mode": "1", "safe_visual": "1"})
    assert r.status_code == 303
    db = new_session()
    assert db.get(UserSettings, user_id("admin")).safe_mode
    db.close()
    page = admin_client.get("/settings").text
    assert "Familievriendelijk" in page
    srt = admin_client.get(f"/clips/{cid}/subtitles.srt").text
    assert "s***" in srt and "shit" not in srt
    seen = {}

    def fake_chat(cfg, messages, **kw):
        seen["p"] = messages[1]["content"]
        return {"clips": [{"id": 1, "title": "T", "description": "D", "hashtags": []}]}

    orig = pick.chat_json
    pick.chat_json = fake_chat
    try:
        copywriting.generate_post_texts(pick.LLMConfig("u", "m", "k"), [{"id": 1, "text": "x"}], family=True)
    finally:
        pick.chat_json = orig
    assert "FAMILIEVRIENDELIJK" in seen["p"]
    assert "FAMILIEVRIENDELIJK" in pick._user_prompt("[0-1] x", 1, 5, 30, family=True)
