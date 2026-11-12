import io
import json
import zipfile

import httpx

from app.pipeline import pick
from tests.conftest import make_project_with_clip


def test_zip_contains_expected_files(admin_client, settings):
    pid, cids = make_project_with_clip(settings, "admin", n_clips=2)
    r = admin_client.get(f"/projects/{pid}/zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = sorted(zf.namelist())
    assert names == sorted(["clips.json", "titels-en-beschrijvingen.txt",
                            "clips/01-clip-1-titel.mp4", "clips/02-clip-2-titel.mp4"])
    assert zf.read("clips/01-clip-1-titel.mp4").startswith(b"FAKE-MP4-FINAL-1")
    meta = json.loads(zf.read("clips.json"))
    assert meta["clips"][0]["titel"] == "Clip 1 titel"
    assert {"laag", "midden", "hoog"} <= set(meta["clips"][0]["geschatte_views"])
    assert "Clip 2 titel" in zf.read("titels-en-beschrijvingen.txt").decode()
    assert zf.testzip() is None


def test_selection_zip(admin_client, settings):
    pid, cids = make_project_with_clip(settings, "admin", n_clips=3)
    r = admin_client.get(f"/projects/{pid}/zip-selection?ids={cids[1]}")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert [n for n in zf.namelist() if n.endswith(".mp4")] == ["clips/02-clip-2-titel.mp4"]
    assert admin_client.get(f"/projects/{pid}/zip-selection").status_code == 400


def test_clip_downloads(admin_client, settings):
    pid, (cid,) = make_project_with_clip(settings, "admin")
    r = admin_client.get(f"/clips/{cid}/download")
    assert r.status_code == 200 and r.content.startswith(b"FAKE-MP4-FINAL")
    assert 'filename="01-clip-1-titel.mp4"' in r.headers["content-disposition"]
    r = admin_client.get(f"/clips/{cid}/download-raw")
    assert r.content.startswith(b"FAKE-MP4-RAW") and "zonder-ondertitels" in r.headers["content-disposition"]
    srt = admin_client.get(f"/clips/{cid}/subtitles.srt")
    assert srt.status_code == 200 and "00:00:00,000 --> " in srt.text and "Hallo" in srt.text
    ass = admin_client.get(f"/clips/{cid}/subtitles.ass")
    assert "[Events]" in ass.text and "\\k" in ass.text
    # range-requests voor de videospeler
    r = admin_client.get(f"/clips/{cid}/video", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == b"FAKE-MP4-F"


def test_downloads_work_while_llm_api_fails(admin_client, settings, monkeypatch):
    """Downloads gebruiken geen internet: ook met een kapotte LLM-API werken ze."""
    def boom(*a, **kw):
        raise httpx.ConnectError("LLM-API onbereikbaar")

    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(pick, "chat_json", boom)
    admin_client.post("/keys/llm", data={"name": "x", "base_url": "https://api.groq.com/openai/v1",
                                         "model": "m", "api_key": "gsk_test_1234567890", "make_active": "1"})
    pid, (cid,) = make_project_with_clip(settings, "admin")
    assert admin_client.get(f"/projects/{pid}").status_code == 200
    assert admin_client.get(f"/clips/{cid}/download").status_code == 200
    assert admin_client.get(f"/projects/{pid}/zip").status_code == 200
    assert admin_client.post(f"/clips/{cid}/posted", data={"platform": "TikTok", "url": ""}).status_code == 303
    assert admin_client.post(f"/clips/{cid}/views", data={"views": "1.234"}).status_code == 303
    assert admin_client.get("/stats").status_code == 200
