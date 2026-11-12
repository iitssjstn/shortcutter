from sqlalchemy import func, select

from app.core import setup as setup_mod
from app.core.db import new_session
from app.core.models import User
from app.core.setup import SetupError, create_first_admin


def _setup_data(code, **kw):
    data = {"setup_code": code, "username": "baas", "email": "", "password": "een-lang-wachtwoord", "password2": "een-lang-wachtwoord"}
    data.update(kw)
    return data


def test_redirect_to_setup_without_users(client):
    for path in ("/", "/login", "/settings", "/projects/1"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/setup", path
    assert client.get("/setup").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_wrong_setup_code_rejected(client):
    r = client.post("/setup", data=_setup_data("fout"))
    assert r.status_code == 403
    assert "Ongeldige setupcode" in r.text
    db = new_session()
    assert db.scalar(select(func.count()).select_from(User)) == 0
    db.close()


def test_setup_without_csrf_rejected(client):
    r = client.post("/setup", data=_setup_data(setup_mod.current_code()), csrf=False)
    assert r.status_code == 403


def test_setup_creates_admin_then_404(client):
    code = setup_mod.current_code()
    assert code
    r = client.post("/setup", data=_setup_data(code, password2="iets-anders-123"))
    assert r.status_code == 400
    r = client.post("/setup", data=_setup_data(code, password="kort", password2="kort"))
    assert r.status_code == 400
    r = client.post("/setup", data=_setup_data(code))
    assert r.status_code == 303
    db = new_session()
    u = db.scalar(select(User))
    assert u.username == "baas" and u.role == "admin"
    db.close()
    # ingelogd en /setup is weg; de oude code werkt niet meer
    assert client.get("/").status_code == 200
    assert client.get("/setup").status_code == 404
    assert client.post("/setup", data=_setup_data(code, username="tweede")).status_code == 404
    assert setup_mod.current_code() is None


def test_setup_race_unique_constraint(app):
    db1, db2 = new_session(), new_session()
    create_first_admin(db1, "een", None, "een-lang-wachtwoord")
    setup_mod._has_users_cache = False
    try:
        create_first_admin(db2, "twee", None, "een-lang-wachtwoord")
        raised = False
    except SetupError:
        raised = True
    assert raised
    # zelfs als de telling omzeild wordt, voorkomt de setup_lock-rij een tweede admin
    from app.core.models import SetupLock
    db3 = new_session()
    db3.add(SetupLock(id=1))
    import pytest
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db3.commit()
    for d in (db1, db2, db3):
        d.close()
