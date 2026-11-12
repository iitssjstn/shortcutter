"""Beheer van API-sleutels (alleen admin).

De volledige sleutel wordt na opslaan nooit meer naar de browser gestuurd: alleen een
masker, de provider en tijdstempels. Wijzigingen komen in de auditlog, zonder waarde.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import redirect, render
from app.core import crypto
from app.core.auth import require_admin
from app.core.db import get_db
from app.core.models import AuditLog, Credential, User, utcnow
from app.core.store import audit

router = APIRouter()

DEFAULT_LLM_BASE = "https://api.groq.com/openai/v1"
DEFAULT_LLM_MODEL = "llama-3.3-70b-versatile"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
MAX_COOKIES_BYTES = 1024 * 1024
PROVIDER_LABELS = {"llm": "LLM (OpenAI-compatibel)", "youtube": "YouTube Data API", "cookies": "yt-dlp cookies.txt"}


def public(c: Credential) -> dict:
    """Wat er van een sleutel naar buiten mag: nooit de waarde zelf."""
    return {
        "id": c.id,
        "provider": c.provider,
        "name": c.name,
        "base_url": c.base_url,
        "model": c.model,
        "mask": c.mask,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        "last_test_status": c.last_test_status,
    }


def _get(db: Session, kid: int) -> Credential:
    c = db.get(Credential, kid)
    if c is None:
        raise HTTPException(404, "Sleutel niet gevonden.")
    return c


def _clean_secret(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise HTTPException(400, "Vul een sleutel in.")
    if len(v) > 500 or any(ch.isspace() for ch in v):
        raise HTTPException(400, "Deze sleutel ziet er ongeldig uit.")
    return v


def _clean_base_url(v: str) -> str:
    v = (v or "").strip().rstrip("/") or DEFAULT_LLM_BASE
    u = urlparse(v)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HTTPException(400, "De base-URL moet met http:// of https:// beginnen.")
    return v


@router.get("/keys")
def keys_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    creds = db.scalars(select(Credential).order_by(Credential.provider, Credential.created_at)).all()
    logs = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(50)).all()
    return render(
        request,
        "keys.html",
        {
            "nav": "keys",
            "llm": [c for c in creds if c.provider == "llm"],
            "youtube": next((c for c in creds if c.provider == "youtube"), None),
            "cookies": next((c for c in creds if c.provider == "cookies"), None),
            "logs": logs,
            "default_base": DEFAULT_LLM_BASE,
            "default_model": DEFAULT_LLM_MODEL,
            "key_source": crypto.key_source(),
        },
    )


@router.get("/api/keys")
def api_keys(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    creds = db.scalars(select(Credential).order_by(Credential.id)).all()
    return JSONResponse([public(c) for c in creds])


@router.post("/keys/llm")
def add_llm(
    name: str = Form("Groq"),
    base_url: str = Form(DEFAULT_LLM_BASE),
    model: str = Form(DEFAULT_LLM_MODEL),
    api_key: str = Form(...),
    make_active: str = Form("1"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    secret = _clean_secret(api_key)
    has_active = db.scalar(select(Credential.id).where(Credential.provider == "llm", Credential.is_active.is_(True)))
    activate = make_active in ("1", "on", "true") or not has_active
    if activate:
        for c in db.scalars(select(Credential).where(Credential.provider == "llm")):
            c.is_active = False
    cred = Credential(
        provider="llm",
        name=(name.strip() or "LLM")[:100],
        base_url=_clean_base_url(base_url),
        model=(model.strip() or DEFAULT_LLM_MODEL)[:120],
        encrypted_value=crypto.encrypt(secret),
        mask=crypto.mask_secret(secret),
        is_active=activate,
        created_by=user.id,
    )
    db.add(cred)
    db.flush()
    audit(db, user, "sleutel_toegevoegd", f"llm #{cred.id} ({cred.name})")
    db.commit()
    return redirect("/keys", f"LLM-configuratie '{cred.name}' opgeslagen.")


@router.post("/keys/{kid}/edit")
def edit_llm(
    kid: int,
    name: str = Form(...),
    base_url: str = Form(...),
    model: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cred = _get(db, kid)
    if cred.provider != "llm":
        raise HTTPException(400, "Alleen LLM-configuraties hebben deze velden.")
    cred.name = (name.strip() or cred.name)[:100]
    cred.base_url = _clean_base_url(base_url)
    cred.model = (model.strip() or cred.model)[:120]
    cred.updated_at = utcnow()
    audit(db, user, "sleutel_gewijzigd", f"llm #{cred.id} ({cred.name}): instellingen")
    db.commit()
    return redirect("/keys", "Configuratie bijgewerkt.")


@router.post("/keys/{kid}/replace")
def replace_key(kid: int, api_key: str = Form(...), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    cred = _get(db, kid)
    if cred.provider == "cookies":
        raise HTTPException(400, "Upload een nieuw cookies.txt-bestand om te vervangen.")
    secret = _clean_secret(api_key)
    cred.encrypted_value = crypto.encrypt(secret)
    cred.mask = crypto.mask_secret(secret)
    cred.updated_at = utcnow()
    cred.last_test_status = None
    audit(db, user, "sleutel_vervangen", f"{cred.provider} #{cred.id} ({cred.name})")
    db.commit()
    return redirect("/keys", "Sleutel vervangen.")


@router.post("/keys/{kid}/activate")
def activate(kid: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    cred = _get(db, kid)
    if cred.provider != "llm":
        raise HTTPException(400, "Alleen LLM-configuraties kun je activeren.")
    for c in db.scalars(select(Credential).where(Credential.provider == "llm")):
        c.is_active = c.id == cred.id
    audit(db, user, "sleutel_geactiveerd", f"llm #{cred.id} ({cred.name})")
    db.commit()
    return redirect("/keys", f"'{cred.name}' is nu de actieve LLM-configuratie.")


@router.post("/keys/{kid}/delete")
def delete_key(kid: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    cred = _get(db, kid)
    was_active = cred.is_active
    audit(db, user, "sleutel_verwijderd", f"{cred.provider} #{cred.id} ({cred.name})")
    db.delete(cred)
    db.flush()
    if was_active:
        nxt = db.scalar(select(Credential).where(Credential.provider == "llm").order_by(Credential.created_at))
        if nxt:
            nxt.is_active = True
    db.commit()
    return redirect("/keys", "Sleutel verwijderd.")


@router.post("/keys/youtube")
def set_youtube(api_key: str = Form(...), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    secret = _clean_secret(api_key)
    cred = db.scalar(select(Credential).where(Credential.provider == "youtube"))
    if cred is None:
        cred = Credential(provider="youtube", name="YouTube Data API", created_by=user.id, encrypted_value=b"", mask="")
        db.add(cred)
        action = "sleutel_toegevoegd"
    else:
        action = "sleutel_vervangen"
    cred.encrypted_value = crypto.encrypt(secret)
    cred.mask = crypto.mask_secret(secret)
    cred.updated_at = utcnow()
    cred.is_active = True
    db.flush()
    audit(db, user, action, f"youtube #{cred.id}")
    db.commit()
    return redirect("/keys", "YouTube Data API-sleutel opgeslagen.")


@router.post("/keys/cookies")
def upload_cookies(file: UploadFile = File(...), user: User = Depends(require_admin), db: Session = Depends(get_db)):
    data = file.file.read(MAX_COOKIES_BYTES + 1)
    if len(data) > MAX_COOKIES_BYTES:
        raise HTTPException(400, "cookies.txt is te groot (max 1 MB).")
    text = data.decode("utf-8", "ignore")
    if "\t" not in text or not any(d in text for d in ("youtube.com", "google.com")):
        raise HTTPException(400, "Dit lijkt geen cookies.txt in Netscape-formaat met YouTube-cookies.")
    cred = db.scalar(select(Credential).where(Credential.provider == "cookies"))
    if cred is None:
        cred = Credential(provider="cookies", name="cookies.txt", created_by=user.id, encrypted_value=b"", mask="")
        db.add(cred)
        action = "sleutel_toegevoegd"
    else:
        action = "sleutel_vervangen"
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    cred.encrypted_value = crypto.encrypt(data)
    cred.mask = f"{len(lines)} cookies"
    cred.updated_at = utcnow()
    cred.is_active = True
    db.flush()
    audit(db, user, action, f"cookies #{cred.id}")
    db.commit()
    return redirect("/keys", "cookies.txt opgeslagen (versleuteld).")


# ---------------------------------------------------------------- testen
def test_llm(cred: Credential, client: httpx.Client) -> tuple[str, str]:
    key = crypto.decrypt_str(cred.encrypted_value)
    try:
        r = client.get(f"{cred.base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return "onbereikbaar", f"De API is niet bereikbaar ({type(exc).__name__})."
    if r.status_code == 200:
        try:
            models = {m.get("id") for m in r.json().get("data", [])}
        except (ValueError, AttributeError):
            models = set()
        if models and cred.model not in models:
            return "geldig", f"Sleutel werkt, maar model '{cred.model}' staat niet in de lijst van de provider."
        return "geldig", "Sleutel werkt."
    if r.status_code in (401, 403):
        return "ongeldig", "Sleutel ongeldig of zonder toegang."
    if r.status_code == 429:
        return "rate_limit", "Rate limit bereikt: de sleutel is waarschijnlijk geldig, probeer het later opnieuw."
    return "fout", f"Onverwacht antwoord van de API (HTTP {r.status_code})."


def test_youtube(cred: Credential, client: httpx.Client) -> tuple[str, str]:
    key = crypto.decrypt_str(cred.encrypted_value)
    try:
        r = client.get(f"{YOUTUBE_API}/videos", params={"part": "id", "id": "jNQXAC9IVRw"}, headers={"X-Goog-Api-Key": key})
    except httpx.HTTPError as exc:
        return "onbereikbaar", f"De API is niet bereikbaar ({type(exc).__name__})."
    if r.status_code == 200:
        return "geldig", "Sleutel werkt."
    body = r.text.lower()
    if r.status_code in (403, 429) and ("quota" in body or "ratelimit" in body.replace(" ", "")):
        return "rate_limit", "Quotum of rate limit bereikt."
    if r.status_code in (400, 401, 403):
        return "ongeldig", "Sleutel ongeldig, of de YouTube Data API v3 is niet ingeschakeld voor dit project."
    return "fout", f"Onverwacht antwoord van de API (HTTP {r.status_code})."


_test_client_factory = lambda: httpx.Client(timeout=15)  # noqa: E731  (vervangbaar in tests)


@router.post("/keys/{kid}/test")
def test_key(request: Request, kid: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    cred = _get(db, kid)
    if cred.provider == "cookies":
        status, msg = "geldig", "cookies.txt is opgeslagen; yt-dlp gebruikt hem bij de volgende download."
    else:
        try:
            with _test_client_factory() as client:
                status, msg = (test_llm if cred.provider == "llm" else test_youtube)(cred, client)
        except RuntimeError as exc:  # ontsleutelen mislukt
            status, msg = "fout", str(exc)
    cred.last_test_status = status
    cred.last_test_at = utcnow()
    audit(db, user, "sleutel_getest", f"{cred.provider} #{cred.id}: {status}")
    db.commit()
    if request.headers.get("hx-request"):
        css = {"geldig": "ok", "rate_limit": "warn"}.get(status, "error")
        return HTMLResponse(f'<span class="badge badge-{css}">{_esc(msg)}</span>')
    return redirect("/keys", msg, "ok" if status == "geldig" else "error")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
