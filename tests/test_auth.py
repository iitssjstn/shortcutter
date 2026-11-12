from datetime import timedelta

from sqlalchemy import select

from app.core.db import new_session
from app.core.models import Invite, User, UserSession
from tests.conftest import PASSWORD, create_user, login, make_client, user_id


def test_login_and_generic_error(app):
    create_user("admin", "admin")
    c = make_client(app)
    r1 = login(c, "admin", "fout-wachtwoord")
    r2 = login(c, "bestaatniet", "fout-wachtwoord")
    assert r1.status_code == r2.status_code == 401
    assert "Onjuiste gebruikersnaam of wachtwoord" in r1.text
    assert "Onjuiste gebruikersnaam of wachtwoord" in r2.text
    r = login(c, "admin")
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "sc_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert c.get("/").status_code == 200


def test_rate_limit_per_username(app):
    create_user("admin", "admin")
    c = make_client(app)
    for _ in range(5):
        assert login(c, "admin", "fout").status_code == 401
    r = login(c, "admin")  # juist wachtwoord, maar geblokkeerd
    assert r.status_code == 429
    assert "Te veel" in r.text


def test_rate_limit_per_ip(app):
    create_user("admin", "admin")
    c = make_client(app)
    for i in range(20):
        login(c, f"onbekend{i}", "fout")
    assert login(c, "admin").status_code == 429


def test_csrf_required(admin_client):
    r = admin_client.post("/account/email", data={"email": "a@b.nl"}, csrf=False)
    assert r.status_code == 403
    r = admin_client.post("/account/email", data={"email": "a@b.nl"}, headers={"X-CSRF-Token": "verkeerd"})
    assert r.status_code == 403
    token = admin_client.csrf()
    r = admin_client.post("/account/email", data={"email": "a@b.nl", "csrf_token": token}, csrf=False)
    assert r.status_code == 303


def test_deactivated_user_cannot_login_and_sessions_dropped(two_users, app):
    admin, alice = two_users["admin"], two_users["alice"]
    assert alice.get("/").status_code == 200
    r = admin.post(f"/users/{user_id('alice')}/active", data={"active": "0"})
    assert r.status_code == 303
    assert alice.get("/").status_code == 303  # sessie ongeldig
    c = make_client(app)
    assert login(c, "alice").status_code == 401


def test_invite_once_and_expiry(admin_client, app):
    r = admin_client.post("/users/invite", data={"username": "nieuw", "role": "user"})
    assert r.status_code == 200
    import re
    link = re.search(r'value="(http://testserver/invite/[^"]+)"', r.text).group(1)
    path = link.replace("http://testserver", "")
    # token staat alleen gehasht in de database
    db = new_session()
    inv = db.scalar(select(Invite))
    assert path.rsplit("/", 1)[1] not in inv.token_hash
    db.close()
    c = make_client(app)
    assert c.get(path).status_code == 200
    r = c.post(path, data={"password": "nieuw-wachtwoord-12", "password2": "nieuw-wachtwoord-12"})
    assert r.status_code == 303
    assert c.get("/").status_code == 200
    c2 = make_client(app)
    assert c2.get(path).status_code == 404
    assert c2.post(path, data={"password": "ander-wachtwoord-12", "password2": "ander-wachtwoord-12"}).status_code == 404

    # verlopen uitnodiging
    r = admin_client.post("/users/invite", data={"username": "laat", "role": "user"})
    path = re.search(r'value="http://testserver(/invite/[^"]+)"', r.text).group(1)
    db = new_session()
    inv = db.scalar(select(Invite).where(Invite.username == "laat"))
    inv.expires_at = inv.created_at + timedelta(hours=48) - timedelta(hours=49)
    db.commit()
    db.close()
    assert make_client(app).get(path).status_code == 404


def test_invite_valid_48h(admin_client):
    admin_client.post("/users/invite", data={"username": "nieuw", "role": "user"})
    db = new_session()
    inv = db.scalar(select(Invite))
    assert timedelta(hours=47, minutes=59) < inv.expires_at - inv.created_at <= timedelta(hours=48, seconds=5)
    db.close()


def test_last_admin_protected(admin_client):
    aid = user_id("admin")
    for url, data in [
        (f"/users/{aid}/delete", {}),
        (f"/users/{aid}/active", {"active": "0"}),
        (f"/users/{aid}/role", {"role": "user"}),
    ]:
        r = admin_client.post(url, data=data)
        assert r.status_code == 303
    db = new_session()
    u = db.get(User, aid)
    assert u is not None and u.is_active and u.role == "admin"
    db.close()


def test_second_admin_allows_demotion(two_users):
    admin = two_users["admin"]
    admin.post(f"/users/{user_id('alice')}/role", data={"role": "admin"})
    r = admin.post(f"/users/{user_id('admin')}/role", data={"role": "user"})
    db = new_session()
    assert db.get(User, user_id("admin")).role == "user"
    db.close()


def test_password_change_invalidates_sessions(two_users, app):
    other = make_client(app)
    login(other, "alice")
    alice = two_users["alice"]
    r = alice.post("/account/password", data={"current": PASSWORD, "password": "heel-nieuw-wachtwoord", "password2": "heel-nieuw-wachtwoord"})
    assert r.status_code == 303
    assert other.get("/").status_code == 303  # andere sessie uitgelogd
    assert alice.get("/").status_code == 200  # huidige sessie vernieuwd
    assert login(make_client(app), "alice", "heel-nieuw-wachtwoord").status_code == 303


def test_logout_all(two_users):
    alice = two_users["alice"]
    alice.post("/account/logout-all")
    db = new_session()
    assert not db.scalars(select(UserSession).where(UserSession.user_id == user_id("alice"))).all()
    db.close()


def test_password_never_in_logs(app, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    create_user("admin", "admin")
    c = make_client(app)
    login(c, "admin", "geheim-fout-wachtwoord-xyz")
    login(c, "admin")
    assert "geheim-fout-wachtwoord-xyz" not in caplog.text
    assert PASSWORD not in caplog.text
