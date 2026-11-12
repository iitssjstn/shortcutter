import logging

import httpx
from sqlalchemy import select

from app.api import keys as keys_api
from app.core import crypto
from app.core.config import Settings
from app.core.db import new_session
from app.core.models import AuditLog, Credential

SECRET = "gsk_SUPERGEHEIM1234567890abcdefA1b2"


def _add(admin, secret=SECRET, **kw):
    data = {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile",
            "api_key": secret, "make_active": "1"}
    data.update(kw)
    return admin.post("/keys/llm", data=data)


def test_key_encrypted_and_never_exposed(admin_client, caplog):
    caplog.set_level(logging.DEBUG)
    assert _add(admin_client).status_code == 303
    db = new_session()
    cred = db.scalar(select(Credential))
    assert SECRET.encode() not in cred.encrypted_value
    assert crypto.decrypt_str(cred.encrypted_value) == SECRET
    assert cred.mask == "gsk_…A1b2"
    db.close()
    for url in ("/keys", "/api/keys", "/settings", "/", "/api/projects"):
        body = admin_client.get(url).text
        assert SECRET not in body, url
        assert SECRET[4:-4] not in body
    assert "gsk_…A1b2" in admin_client.get("/keys").text
    js = admin_client.get("/api/keys").json()
    assert js[0]["mask"] == "gsk_…A1b2" and "encrypted_value" not in js[0] and "api_key" not in js[0]
    assert SECRET not in caplog.text
    # de rauwe database-bytes bevatten de sleutel ook niet
    from app.core.config import get_settings
    assert SECRET.encode() not in get_settings().db_path.read_bytes()


def test_replace_delete_and_audit(admin_client):
    _add(admin_client)
    db = new_session()
    kid = db.scalar(select(Credential.id))
    db.close()
    new = "gsk_ANDERESLEUTEL0000000000000Zz99"
    assert admin_client.post(f"/keys/{kid}/replace", data={"api_key": new}).status_code == 303
    page = admin_client.get("/keys").text
    assert new not in page and "gsk_…Zz99" in page
    # het vervang-veld toont nooit de oude waarde
    assert 'value="gsk_' not in page
    assert admin_client.post(f"/keys/{kid}/delete").status_code == 303
    db = new_session()
    actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
    targets = " ".join(a.target for a in db.scalars(select(AuditLog)))
    assert actions == ["sleutel_toegevoegd", "sleutel_vervangen", "sleutel_verwijderd"]
    assert SECRET not in targets and new not in targets
    assert db.scalar(select(Credential)) is None
    db.close()


def test_multiple_llm_one_active(admin_client):
    _add(admin_client, name="Een")
    _add(admin_client, secret="sk-tweede-sleutel-00000000001111", name="Twee",
         base_url="https://api.openai.com/v1", model="gpt-4o-mini")
    db = new_session()
    creds = db.scalars(select(Credential).order_by(Credential.id)).all()
    assert [c.is_active for c in creds] == [False, True]
    db.close()
    admin_client.post(f"/keys/{creds[0].id}/activate")
    db = new_session()
    assert [c.is_active for c in db.scalars(select(Credential).order_by(Credential.id))] == [True, False]
    db.close()


def test_key_test_button(admin_client, monkeypatch):
    _add(admin_client)
    db = new_session()
    kid = db.scalar(select(Credential.id))
    db.close()
    seen = {}

    def handler(status):
        def h(request: httpx.Request):
            seen["auth"] = request.headers.get("authorization")
            if status == 200:
                return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"}]})
            return httpx.Response(status, json={"error": "x"})
        return h

    for status, expect in [(200, "Sleutel werkt"), (401, "ongeldig"), (429, "Rate limit")]:
        monkeypatch.setattr(keys_api, "_test_client_factory",
                            lambda s=status: httpx.Client(transport=httpx.MockTransport(handler(s))))
        r = admin_client.post(f"/keys/{kid}/test", headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert expect.lower() in r.text.lower()
        assert SECRET not in r.text
    assert seen["auth"] == f"Bearer {SECRET}"


def test_master_key_reused_after_restart(tmp_path):
    s = Settings()
    s.data_dir = tmp_path
    s.master_key_file = tmp_path / "bestaat-niet"
    crypto.load_master_key(s)
    token = crypto.encrypt("waarde")
    key_file = tmp_path / ".master_key"
    assert key_file.exists() and (key_file.stat().st_mode & 0o777) == 0o600
    first = key_file.read_bytes()
    crypto.load_master_key(s)  # "herstart"
    assert key_file.read_bytes() == first
    assert crypto.decrypt_str(token) == "waarde"


def test_master_key_from_docker_secret(tmp_path):
    secret = tmp_path / "app_master_key"
    secret.write_text("mijn eigen geheime master key tekst\n")
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.master_key_file = secret
    crypto.load_master_key(s)
    tok = crypto.encrypt("x")
    assert not (tmp_path / "data" / ".master_key").exists()
    assert crypto.key_source() == "docker-secret"
    crypto.load_master_key(s)
    assert crypto.decrypt_str(tok) == "x"


def test_mask():
    assert crypto.mask_secret("gsk_abcdefghijklmnopa1b2") == "gsk_…a1b2"
    assert crypto.mask_secret("AIzaSyAbcdefghijklmnop12") == "…op12"
    assert crypto.mask_secret("sk-proj-abcdefghij9876") == "sk-…9876"


def test_no_key_shows_warning_on_dashboard(admin_client):
    assert "Geen LLM-sleutel ingesteld" in admin_client.get("/").text
    _add(admin_client)
    assert "Geen LLM-sleutel ingesteld" not in admin_client.get("/").text


def test_cookies_upload(admin_client):
    content = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tgeheimecookiewaarde\n"
    r = admin_client.post("/keys/cookies", files={"file": ("cookies.txt", content, "text/plain")})
    assert r.status_code == 303
    page = admin_client.get("/keys").text
    assert "geheimecookiewaarde" not in page and "1 cookies" in page
    db = new_session()
    cred = db.scalar(select(Credential).where(Credential.provider == "cookies"))
    assert b"geheimecookiewaarde" not in cred.encrypted_value
    db.close()
