import pytest

from tests.conftest import make_project_with_clip


@pytest.fixture
def data(two_users, settings):
    pa, ca = make_project_with_clip(settings, "alice")
    pb, cb = make_project_with_clip(settings, "bob")
    return two_users, (pa, ca[0]), (pb, cb[0])


def test_user_cannot_read_other_users_data(data):
    users, (pa, ca), (pb, cb) = data
    alice = users["alice"]
    for url in [
        f"/projects/{pb}", f"/projects/{pb}/zip", f"/projects/{pb}/zip-selection?ids={cb}", f"/projects/{pb}/thumb",
        f"/projects/{pb}/source", f"/clips/{cb}/video", f"/clips/{cb}/download", f"/clips/{cb}/download-raw",
        f"/clips/{cb}/subtitles.srt", f"/clips/{cb}/thumb", f"/api/projects/{pb}", f"/api/projects/{pb}/transcript",
    ]:
        r = alice.get(url)
        assert r.status_code == 404, url
        assert b"FAKE-MP4" not in r.content
    # eigen data werkt wel
    assert alice.get(f"/projects/{pa}").status_code == 200
    assert alice.get(f"/clips/{ca}/download").content.startswith(b"FAKE-MP4-FINAL")


def test_user_cannot_modify_other_users_data(data):
    users, (pa, ca), (pb, cb) = data
    alice = users["alice"]
    for url, form in [
        (f"/clips/{cb}/retitle", {"title": "gehackt"}),
        (f"/clips/{cb}/trim", {"start": "1", "end": "4"}),
        (f"/clips/{cb}/delete", {}),
        (f"/clips/{cb}/posted", {"platform": "TikTok"}),
        (f"/clips/{cb}/captions", {"enabled": "0"}),
        (f"/projects/{pb}/delete", {}),
        (f"/projects/{pb}/retry", {}),
        (f"/projects/{pb}/clips", {"start": "1", "end": "4"}),
    ]:
        assert alice.post(url, data=form).status_code == 404, url
    bob_project = users["bob"].get(f"/api/projects/{pb}").json()
    assert bob_project["clips"][0]["title"] == "Clip 1 titel"


def test_project_lists_are_scoped(data):
    users, (pa, _), (pb, _) = data
    ids = [p["id"] for p in users["alice"].get("/api/projects?scope=all").json()]
    assert ids == [pa]
    admin_all = [p["id"] for p in users["admin"].get("/api/projects?scope=all").json()]
    assert set(admin_all) == {pa, pb}
    assert users["admin"].get("/api/projects").json() == []  # standaard alleen eigen projecten


def test_admin_can_access_everything(data):
    users, (pa, ca), (pb, cb) = data
    admin = users["admin"]
    assert admin.get(f"/projects/{pb}").status_code == 200
    assert admin.get(f"/clips/{cb}/download").status_code == 200
    assert admin.get(f"/projects/{pb}/zip").status_code == 200
    assert admin.post(f"/clips/{cb}/retitle", data={"title": "Door admin"}).status_code == 303


def test_admin_only_pages(data):
    users, *_ = data
    alice = users["alice"]
    for url in ("/keys", "/users", "/api/keys"):
        assert alice.get(url).status_code == 403, url
    assert alice.post("/keys/llm", data={"api_key": "gsk_test123456789"}).status_code == 403
    assert alice.post("/settings/global", data={"max_active_per_user": "3"}).status_code == 403
    assert alice.post("/users/invite", data={"username": "x"}).status_code == 403
    assert users["admin"].get("/keys").status_code == 200
    assert users["admin"].post("/settings/global", data={"max_active_per_user": "3", "source_retention_days": "7",
                                                         "project_retention_days": "0"}).status_code == 303
    # eigen instellingen mag iedereen wijzigen
    assert alice.post("/settings", data={"channel_avg_views": "5000"}).status_code == 303


def test_not_logged_in_redirects(app, data):
    from tests.conftest import make_client
    c = make_client(app)
    _, (pa, ca), _ = data
    r = c.get(f"/clips/{ca}/download")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.get("/api/projects").status_code == 401


def test_no_static_media_dir(data, settings):
    users, (pa, ca), _ = data
    for url in (f"/data/output/{pa}/clip_01.mp4", f"/output/{pa}/clip_01.mp4", f"/static/../data/output/{pa}/clip_01.mp4"):
        assert users["alice"].get(url).status_code == 404
