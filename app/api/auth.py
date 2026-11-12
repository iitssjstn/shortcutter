"""Inloggen, uitloggen, eerste setup en uitnodigingen."""
from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import redirect, render, safe_next
from app.core import auth as A
from app.core.crypto import token_hash
from app.core.db import get_db
from app.core.models import Invite, User, UserSettings, utcnow
from app.core.passwords import hash_password, needs_rehash, validate_new_password, verify_password
from app.core.setup import SetupError, check_code, create_first_admin, has_users

router = APIRouter()

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
LOGIN_ERROR = "Onjuiste gebruikersnaam of wachtwoord."
LOCKED_ERROR = "Te veel mislukte pogingen. Probeer het over 15 minuten opnieuw."


def valid_username(name: str) -> str | None:
    if not USERNAME_RE.match(name or ""):
        return "Gebruikersnaam: 3-32 tekens, alleen letters, cijfers, punt, streepje of underscore."
    return None


# ---------------------------------------------------------------- login
@router.get("/login")
def login_page(request: Request, next: str = "/", user=Depends(A.optional_user)):
    if user:
        return redirect(safe_next(next))
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    username = username.strip()
    keys = A.rate_keys(request, username)
    ctx = {"next": safe_next(next), "username": username}
    if A.is_locked(db, keys):
        return render(request, "login.html", {**ctx, "error": LOCKED_ERROR}, status_code=429)
    user = db.scalar(select(User).where(User.username == username)) if username else None
    ok = verify_password(user.password_hash if user else None, password)
    if not ok or user is None or not user.is_active:
        A.register_failure(db, keys)
        return render(request, "login.html", {**ctx, "error": LOGIN_ERROR}, status_code=401)
    A.reset_failures(db, keys[1])
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    token = A.create_session(db, user, request)
    resp = redirect(safe_next(next))
    A.set_session_cookie(resp, token)
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    A.delete_session(db, request.cookies.get(A.SESSION_COOKIE))
    resp = redirect("/login", "Je bent uitgelogd.")
    A.clear_session_cookie(resp)
    return resp


# ---------------------------------------------------------------- setup
@router.get("/setup")
def setup_page(request: Request, db: Session = Depends(get_db)):
    if has_users(db):
        raise HTTPException(404, "Niet gevonden.")
    return render(request, "setup.html", {})


@router.post("/setup")
def setup_submit(
    request: Request,
    setup_code: str = Form(""),
    username: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    db: Session = Depends(get_db),
):
    if has_users(db):
        raise HTTPException(404, "Niet gevonden.")
    keys = A.rate_keys(request, None, scope="setup")
    username = username.strip()
    ctx = {"username": username, "email": email}
    if A.is_locked(db, keys):
        return render(request, "setup.html", {**ctx, "error": LOCKED_ERROR}, status_code=429)
    if not check_code(setup_code):
        A.register_failure(db, keys)
        return render(
            request, "setup.html",
            {**ctx, "error": "Ongeldige setupcode. Je vindt hem met: docker compose logs shortcutter"},
            status_code=403,
        )
    err = valid_username(username) or validate_new_password(password, password2)
    if err:
        return render(request, "setup.html", {**ctx, "error": err}, status_code=400)
    try:
        user = create_first_admin(db, username, email.strip() or None, password)
    except SetupError:
        raise HTTPException(404, "Niet gevonden.")
    token = A.create_session(db, user, request)
    resp = redirect("/keys", "Welkom! Je account is aangemaakt. Voeg nu een LLM-sleutel toe.")
    A.set_session_cookie(resp, token)
    return resp


# ---------------------------------------------------------------- uitnodigingen
def _valid_invite(db: Session, token: str) -> Invite | None:
    if not token or len(token) > 200:
        return None
    inv = db.scalar(select(Invite).where(Invite.token_hash == token_hash(token)))
    if inv is None or inv.used_at is not None or inv.expires_at < utcnow():
        return None
    return inv


@router.get("/invite/{token}")
def invite_page(request: Request, token: str, db: Session = Depends(get_db)):
    inv = _valid_invite(db, token)
    if inv is None:
        return render(request, "invite.html", {"invalid": True}, status_code=404)
    return render(request, "invite.html", {"invite": inv, "token": token, "reset": inv.user_id is not None})


@router.post("/invite/{token}")
def invite_submit(
    request: Request,
    token: str,
    password: str = Form(""),
    password2: str = Form(""),
    email: str = Form(""),
    db: Session = Depends(get_db),
):
    inv = _valid_invite(db, token)
    if inv is None:
        return render(request, "invite.html", {"invalid": True}, status_code=404)
    err = validate_new_password(password, password2)
    if err:
        return render(
            request, "invite.html",
            {"invite": inv, "token": token, "reset": inv.user_id is not None, "error": err}, status_code=400,
        )
    if inv.user_id is not None:  # wachtwoord-reset
        user = db.get(User, inv.user_id)
        if user is None:
            return render(request, "invite.html", {"invalid": True}, status_code=404)
        user.password_hash = hash_password(password)
        A.invalidate_user_sessions(db, user.id)
    else:
        if db.scalar(select(User).where(User.username == inv.username)):
            return render(request, "invite.html", {"invalid": True}, status_code=409)
        user = User(
            username=inv.username, email=email.strip() or None, password_hash=hash_password(password), role=inv.role
        )
        db.add(user)
        db.flush()
        db.add(UserSettings(user_id=user.id))
    inv.used_at = utcnow()
    # alle andere openstaande links voor deze gebruiker vervallen
    for other in db.scalars(select(Invite).where(Invite.username == inv.username, Invite.used_at.is_(None))):
        other.used_at = utcnow()
    db.commit()
    if not user.is_active:
        return redirect("/login", "Wachtwoord ingesteld, maar je account is gedeactiveerd.", "error")
    sess_token = A.create_session(db, user, request)
    resp = redirect("/", "Je wachtwoord is ingesteld. Welkom!")
    A.set_session_cookie(resp, sess_token)
    return resp


INVITE_VALID = timedelta(hours=48)
