"""Compilaties: Twitch-clips achter elkaar als liggende 16:9-video, met hoofdstukken en upload."""
import json
import shutil

from sqlalchemy import func, select

from app.core.db import new_session
from app.core.models import Compilation, TwitchChannel
from app.pipeline import compile as cp
from app.pipeline import media
from app.worker.runner import run_compilation_once
from tests.conftest import create_user, login, make_client
from tests.test_twitch import _add_key, fake  # noqa: F401 (fixture)
from tests.test_youtube_upload import _connect, _setup_oauth, google  # noqa: F401 (fixture)


def _channel(c):
    c.post("/twitch/channels", data={"login": "streamer", "confirm": "1"})
    db = new_session()
    ch = db.scalar(select(TwitchChannel))
    db.close()
    return ch


def _fake_download(sample_video):
    def dl(url, out_dir, cookies_file=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "source.mp4"
        shutil.copy(sample_video, p)
        return p, {}
    return dl


def test_chapters_and_titles():
    starts = [(3.0, "#3 A"), (15.0, "#2 B"), (20.0, "#1 C"), (40.0, "#0 D")]
    ch = cp.build_chapters(starts)
    assert ch[0] == {"t": 0.0, "label": "#3 A"}  # eerste hoofdstuk altijd op 0:00 (intro telt mee)
    assert [c["label"] for c in ch] == ["#3 A", "#2 B", "#0 D"]  # < 10 s na de vorige: overgeslagen
    assert cp.build_chapters(starts[:2]) == []  # minder dan 3: geen hoofdstukken
    assert cp.fmt_ts(75) == "1:15" and cp.fmt_ts(3725) == "1:02:05"
    assert [cp.rank_of("countdown", k, 3) for k in range(3)] == [3, 2, 1]
    assert cp.rank_of("none", 0, 3) is None
    assert cp.default_title("maand", ["X", "X"], "en") == "Best X Clips of the Month"


def test_compilation_end_to_end(admin_client, fake, google, sample_video, monkeypatch):
    c = admin_client
    _add_key(c)
    ch = _channel(c)
    fake.clips["123"].append({"id": "SlugDrie-ghi", "url": "https://clips.twitch.tv/SlugDrie-ghi", "title": "Clutch #3",
                              "view_count": 800, "duration": 12.0, "broadcaster_id": "123", "broadcaster_name": "Streamer"})
    r = c.post("/compilations", data={"channel_ids": [str(ch.id)], "period": "week", "count": "10", "ranking": "countdown"})
    assert r.status_code == 303
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    db = new_session()
    comp = db.get(Compilation, cid)
    assert [i.views for i in comp.items] == [800, 5400, 12000]  # aflopend: #1 (meest bekeken) als laatste
    assert all(i.broadcaster_id == "123" for i in comp.items)
    ids = [i.id for i in comp.items]
    db.close()
    page = c.get(f"/compilations/{cid}").text
    assert "#3" in page and "#1" in page and "Insane 1v5 clutch" in page

    # volgorde aanpassen, titel en einde van één clip wijzigen, dan renderen
    form = {"title": "Best Streamer Clips", "ranking": "countdown", "intro": "1", "outro": "1"}
    for i in ids:
        form[f"inc_{i}"] = "1"
        form[f"start_{i}"] = "0"
        form[f"end_{i}"] = ""
    form[f"title_{ids[0]}"] = "Mijn eigen titel"
    form[f"end_{ids[1]}"] = "5"
    c.post(f"/compilations/{cid}", data=dict(form, move=f"{ids[2]}:up"))
    db = new_session()
    comp = db.get(Compilation, cid)
    assert [i.id for i in comp.items] == [ids[0], ids[2], ids[1]]
    assert comp.items[0].title == "Mijn eigen titel" and comp.items[2].trim_end == 5.0
    db.close()
    assert c.post(f"/compilations/{cid}", data=dict(form, render="1")).status_code == 303

    monkeypatch.setattr(cp, "download", _fake_download(sample_video))
    assert run_compilation_once() is True
    assert run_compilation_once() is False
    db = new_session()
    comp = db.get(Compilation, cid)
    assert comp.status == "done", comp.error
    out = cp.comp_dir(cid) / comp.file_path
    info = media.probe(out)
    assert (info["width"], info["height"]) == (1920, 1080) and info["has_audio"]  # liggend, geen 9:16
    # intro 3 + 8 + 8 + 5 + outro 4
    assert abs(info["duration"] - 28.0) < 1.0 and abs(comp.duration - info["duration"]) < 0.01
    assert "https://twitch.tv/streamer" in comp.description and comp.yt_title
    assert "#shorts" not in (comp.hashtags or "")
    assert comp.chapters == []  # clips van 8 s: te kort voor YouTube-hoofdstukken (min. 10 s)
    assert not (cp.comp_dir(cid) / "work").exists()
    db.close()
    page = c.get(f"/compilations/{cid}").text
    assert "Download mp4" in page and "Naar YouTube" in page
    r = c.get(f"/compilations/{cid}/video?download=1")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert c.get(f"/compilations/{cid}/status").headers.get("HX-Refresh") == "true"

    # uploaden als gewone (liggende) video
    _setup_oauth(c)
    _connect(c)
    r = c.post(f"/compilations/{cid}/youtube", data={"title": "Best Streamer Clips #47", "description": "Hallo\n0:00 Start",
                                                    "privacy": "unlisted", "hashtags": "twitch gaming"})
    assert r.status_code == 303
    assert c.get(f"/compilations/{cid}/status").status_code == 204
    assert run_compilation_once(upload=True) is True
    db = new_session()
    comp = db.get(Compilation, cid)
    assert comp.yt_status == "done" and comp.yt_video_id == "VIDEO123"
    db.close()
    meta = google.last_meta
    assert meta["snippet"]["title"] == "Best Streamer Clips #47" and meta["snippet"]["categoryId"] == "20"
    tags = meta["snippet"]["tags"]
    assert tags[0] == "twitch clips" and "Streamer" in tags and tags[-2:] == ["twitch", "gaming"]  # verborgen tags + hashtags


def test_compilation_permissions(two_users, fake, sample_video, monkeypatch):
    _add_key(two_users["admin"])
    alice, bob = two_users["alice"], two_users["bob"]
    ch = _channel(alice)
    r = alice.post("/compilations", data={"channel_ids": [str(ch.id)], "count": "5"})
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    # van een ander zijn compilatie: niet zichtbaar
    for url in (f"/compilations/{cid}", f"/compilations/{cid}/video", f"/compilations/{cid}/status"):
        assert bob.get(url).status_code == 404
    assert bob.post(f"/compilations/{cid}/render").status_code == 404
    # bob kan alice' kanaal niet gebruiken
    r = bob.post("/compilations", data={"channel_ids": [str(ch.id)]}, follow_redirects=True)
    assert "minstens één kanaal" in r.text
    # clip van een kanaal zonder toestemming kan niet worden toegevoegd
    r = alice.post(f"/compilations/{cid}/add", data={"url": "https://clips.twitch.tv/Vreemd-xyz"}, follow_redirects=True)
    assert "Geen toestemming" in r.text
    r = alice.post(f"/compilations/{cid}/add", data={"url": "https://clips.twitch.tv/SlugEen-abc"}, follow_redirects=True)
    assert "staat er al in" in r.text
    # minstens 2 clips nodig
    db = new_session()
    ids = [i.id for i in db.get(Compilation, cid).items]
    db.close()
    r = alice.post(f"/compilations/{cid}", data={f"inc_{ids[0]}": "1", "render": "1"}, follow_redirects=True)
    assert "minstens 2 clips" in r.text
    assert alice.post(f"/compilations/{cid}/delete").status_code == 303
    assert not cp.comp_dir(cid).exists()
    assert alice.get(f"/compilations/{cid}").status_code == 404


def test_recover_after_restart(admin_client):
    db = new_session()
    from tests.conftest import user_id

    comp = Compilation(owner_id=user_id("admin"), title="x", status="rendering", attempts=1, yt_status="uploading")
    db.add(comp)
    db.commit()
    cp.recover(db)
    assert comp.status == "queued" and comp.yt_status == "failed"
    comp.status, comp.attempts = "rendering", 2
    db.commit()
    cp.recover(db)
    assert comp.status == "failed"
    db.close()


def test_weekly_schedule(admin_client, fake, monkeypatch):
    from datetime import datetime

    from app.core.models import CompilationSchedule
    from app.pipeline import weekly
    from app.worker.runner import run_schedules
    from tests.conftest import user_id

    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    c = admin_client
    _add_key(c)
    ch = _channel(c)
    # aanzetten zonder kanaal kan niet
    r = c.post("/compilations/schedule", data={"enabled": "1"}, follow_redirects=True)
    assert "minstens één kanaal" in r.text
    r = c.post("/compilations/schedule", data={"enabled": "1", "channel_ids": [str(ch.id)], "weekday": "0", "hour": "9",
                                               "count": "10", "ranking": "countdown", "intro": "1", "auto_render": "1"},
               follow_redirects=True)
    assert "Wekelijkse compilatie staat aan" in r.text and "maandag" in r.text
    db = new_session()
    s = db.scalar(select(CompilationSchedule))
    assert s.enabled and s.channel_ids == [ch.id] and not s.outro
    # direct na aanzetten niet meteen een gemist moment inhalen
    assert not weekly.is_due(s)
    # maandag 9:00 Amsterdam = 8:00 UTC (wintertijd)
    s.last_run_at = datetime(2026, 11, 22, 12, 0)  # zondag
    db.commit()
    assert weekly.last_slot(s, datetime(2026, 11, 23, 8, 30)) == datetime(2026, 11, 23, 8, 0)
    assert not weekly.is_due(s, datetime(2026, 11, 23, 7, 59))
    assert weekly.is_due(s, datetime(2026, 11, 23, 8, 1))
    assert weekly.run_due(db, datetime(2026, 11, 23, 8, 1)) == 1
    assert weekly.run_due(db, datetime(2026, 11, 23, 8, 6)) == 0  # niet twee keer per week
    comp = db.scalars(select(Compilation)).one()
    assert comp.auto and comp.status == "queued" and comp.intro and not comp.outro
    assert len(comp.items) == 2 and comp.items[-1].views == 12000
    assert "Concept gemaakt met 2 clips" in s.last_result
    db.close()
    page = c.get("/compilations").text
    assert "automatisch" in page and "Volgende: maandag" in page
    # 'Nu een concept maken' werkt ook als de planning uit staat
    r = c.post("/compilations/schedule", data={"channel_ids": [str(ch.id)], "run_now": "1"})
    assert r.status_code == 303 and r.headers["location"].startswith("/compilations/")
    assert run_schedules() == 0  # uit: niets
    db = new_session()
    assert db.scalar(select(func.count()).select_from(Compilation)) == 2
    db.close()


def test_twitch_preview_buttons(admin_client, fake):
    c = admin_client
    _add_key(c)
    ch = _channel(c)
    page = c.get(f"/twitch?channel={ch.id}").text
    assert 'data-twitch-clip="SlugEen-abc"' in page
    r = c.post("/compilations", data={"channel_ids": [str(ch.id)], "count": "5"})
    page = c.get(r.headers["location"]).text
    assert 'data-twitch-clip="SlugTwee-def"' in page
    assert "frame-src https://clips.twitch.tv" in c.get("/").headers["content-security-policy"]


def test_custom_clip_count(admin_client, fake):
    from app.core.models import CompilationSchedule

    c = admin_client
    _add_key(c)
    ch = _channel(c)
    page = c.get("/compilations").text
    assert 'name="count" type="number" min="2" max="100"' in page
    for i in range(30):
        fake.clips["123"].append({"id": f"Extra{i}", "url": f"https://clips.twitch.tv/Extra{i}", "title": f"Extra {i}",
                                  "view_count": 100 + i, "duration": 10.0, "broadcaster_id": "123",
                                  "broadcaster_name": "Streamer"})
    r = c.post("/compilations", data={"channel_ids": [str(ch.id)], "count": "25"})
    db = new_session()
    comp = db.get(Compilation, int(r.headers["location"].rsplit("/", 1)[1]))
    assert len(comp.items) == 25  # meer dan de oude grens van 20
    db.close()
    c.post("/compilations/schedule", data={"channel_ids": [str(ch.id)], "count": "250"})
    c.post("/compilations/schedule", data={"channel_ids": [str(ch.id)], "count": "1"})
    db = new_session()
    assert db.scalar(select(CompilationSchedule)).count == 2
    db.close()
    c.post("/compilations/schedule", data={"channel_ids": [str(ch.id)], "count": "250"})
    db = new_session()
    assert db.scalar(select(CompilationSchedule)).count == cp.MAX_ITEMS
    db.close()


def test_short_top_list(admin_client, fake, sample_video, monkeypatch):
    from types import SimpleNamespace as N

    c = admin_client
    _add_key(c)
    ch = _channel(c)
    fake.clips["123"].append({"id": "SlugDrie-ghi", "url": "https://clips.twitch.tv/SlugDrie-ghi", "title": "Clutch #3",
                              "view_count": 800, "duration": 12.0, "broadcaster_id": "123", "broadcaster_name": "Streamer"})
    r = c.post("/compilations", data={"channel_ids": [str(ch.id)], "count": "50", "format": "short"})
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    db = new_session()
    comp = db.get(Compilation, cid)
    assert comp.format == "short" and not comp.intro and not comp.outro  # short begint meteen
    assert comp.title == "Ranking Top 3 *Streamer* Moments"
    db.close()
    page = c.get(f"/compilations/{cid}").text
    assert "staand 1080×1920" in page and "Titel bovenaan in beeld" in page

    # lijstje: plaats 1 bovenaan, nog niet getoonde clips als ???
    items = [N(title="a"), N(title="b"), N(title="c")]
    assert cp.short_list("countdown", items, 0) == [(1, "c", "nog"), (2, "b", "nog"), (3, "a", "nu")]
    assert cp.short_list("countdown", items, 2) == [(1, "c", "nu"), (2, "b", "gezien"), (3, "a", "gezien")]
    assert cp.short_list("none", items, 0) == []
    assert cp._title_words("Ranking Top 5 *Bruh* Moments") == [
        ("Ranking", (255, 255, 255)), ("Top", cp.TITLE_ACCENT), ("5", cp.TITLE_ACCENT),
        ("Bruh", cp.TITLE_MARK), ("Moments", (255, 255, 255))]

    assert c.post(f"/compilations/{cid}/render").status_code == 303
    monkeypatch.setattr(cp, "download", _fake_download(sample_video))
    assert run_compilation_once() is True
    db = new_session()
    comp = db.get(Compilation, cid)
    assert comp.status == "done", comp.error
    info = media.probe(cp.comp_dir(cid) / comp.file_path)
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert abs(info["duration"] - 24.0) < 1.0  # 3 x 8 s, zonder intro/outro
    assert comp.chapters == [] and "#shorts" in comp.hashtags
    assert "*" not in comp.yt_title
    db.close()


def test_short_limits(admin_client, fake):
    c = admin_client
    _add_key(c)
    ch = _channel(c)
    for i in range(14):
        fake.clips["123"].append({"id": f"L{i}", "url": f"https://clips.twitch.tv/L{i}", "title": f"Lang {i}",
                                  "view_count": 50 + i, "duration": 40.0, "broadcaster_id": "123",
                                  "broadcaster_name": "Streamer"})
    r = c.post("/compilations", data={"channel_ids": [str(ch.id)], "count": "30", "format": "short"})
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    db = new_session()
    comp = db.get(Compilation, cid)
    assert len(comp.items) == cp.MAX_SHORT_ITEMS  # maximaal 10 in een short
    ids = [i.id for i in comp.items]
    db.close()
    r = c.post(f"/compilations/{cid}/render", follow_redirects=True)
    assert "maximaal 3 minuten" in r.text  # 10 clips: ruim 3 min
    # inkorten tot 6 clips van 25 s: past
    form = {"title": "Top", "ranking": "countdown"}
    for i in ids[:6]:
        form.update({f"inc_{i}": "1", f"start_{i}": "0", f"end_{i}": "25"})
    r = c.post(f"/compilations/{cid}", data=dict(form, render="1"), follow_redirects=True)
    assert "in de wachtrij" in r.text
    c.post("/compilations/schedule", data={"channel_ids": [str(ch.id)], "format": "short"})
    from app.core.models import CompilationSchedule
    db = new_session()
    assert db.scalar(select(CompilationSchedule)).format == "short"
    db.close()


def test_short_item_layout(admin_client, fake, sample_video, monkeypatch):
    c = admin_client
    _add_key(c)
    ch = _channel(c)
    r = c.post("/compilations", data={"channel_ids": [str(ch.id)], "count": "5", "format": "short"})
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    db = new_session()
    ids = [i.id for i in db.get(Compilation, cid).items]
    db.close()
    page = c.get(f"/compilations/{cid}").text
    assert 'class="lay-ed"' in page and "Facecam onder" in page
    form = {"title": "Top", "ranking": "countdown"}
    for i in ids:
        form.update({f"inc_{i}": "1", f"start_{i}": "0", f"end_{i}": "", f"layout_{i}": "fill", f"cropx_{i}": "50",
                     f"campos_{i}": "bottom", f"cam_{i}": ""})
    form.update({f"layout_{ids[0]}": "split", f"campos_{ids[0]}": "top", f"cropx_{ids[0]}": "20",
                 f"cam_{ids[0]}": "0.0,0.0,0.3,0.4", f"cam_{ids[1]}": "onzin"})
    r = c.post(f"/compilations/{cid}", data=dict(form, apply_layout=str(ids[0])))
    assert r.status_code == 303 and f"open={ids[0]}" in r.headers["location"]
    db = new_session()
    items = db.get(Compilation, cid).items
    assert all((i.layout, i.cam_pos, i.cam_box, round(i.crop_x, 2)) == ("split", "top", "0.0000,0.0000,0.3000,0.4000", 0.2)
               for i in items)
    db.close()
    # render met gestapelde indeling
    assert c.post(f"/compilations/{cid}/render").status_code == 303
    monkeypatch.setattr(cp, "download", _fake_download(sample_video))
    assert run_compilation_once() is True
    db = new_session()
    comp = db.get(Compilation, cid)
    assert comp.status == "done", comp.error
    info = media.probe(cp.comp_dir(cid) / comp.file_path)
    assert (info["width"], info["height"]) == (1080, 1920)
    db.close()
    # zonder kader en zonder gezicht: automatisch zoeken valt terug op vol
    from types import SimpleNamespace as N

    lay, note = cp.item_layout(N(layout="split", cam_box=None, cam_pos="bottom", crop_x=0.5), sample_video,
                               {"width": 1280, "height": 720}, 0.0, 3.0)
    assert lay["mode"] == "fill" and "geen facecam" in note
    assert cp.item_layout(N(layout="fill", cam_box=None, cam_pos="bottom", crop_x=0.1), sample_video, {}, 0, 3)[0] == \
        {"mode": "fill", "crop_x": 0.1}
