"""Mijn account: wachtwoord, e-mail, uitloggen (op alle apparaten)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.api.common import redirect, render
from app.core import auth as A
from app.core.db import get_db
from app.core.models import User
from app.core.passwords import hash_password, validate_new_password, verify_password

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/account")
def account_page(request: Request, user: User = Depends(A.require_user)):
    return render(request, "account.html", {"nav": "account"})


@router.post("/account/password")
def change_password(
    request: Request,
    current: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    user: User = Depends(A.require_user),
    db: Session = Depends(get_db),
):
    keys = A.rate_keys(request, user.username, scope="pwchange")
    if A.is_locked(db, keys):
        return redirect("/account", "Te veel mislukte pogingen. Probeer het later opnieuw.", "error")
    if not verify_password(user.password_hash, current):
        A.register_failure(db, keys)
        return redirect("/account", "Je huidige wachtwoord klopt niet.", "error")
    err = validate_new_password(password, password2)
    if err:
        return redirect("/account", err, "error")
    user = db.merge(user)
    user.password_hash = hash_password(password)
    A.invalidate_user_sessions(db, user.id)
    db.commit()
    token = A.create_session(db, user, request)
    resp = redirect("/account", "Wachtwoord gewijzigd. Alle andere sessies zijn uitgelogd.")
    A.set_session_cookie(resp, token)
    return resp


@router.post("/account/email")
def change_email(email: str = Form(""), user: User = Depends(A.require_user), db: Session = Depends(get_db)):
    email = email.strip()
    if email and not EMAIL_RE.match(email):
        return redirect("/account", "Dat is geen geldig e-mailadres.", "error")
    user = db.merge(user)
    user.email = email[:255] or None
    db.commit()
    return redirect("/account", "E-mailadres opgeslagen.")


@router.post("/account/logout-all")
def logout_all(user: User = Depends(A.require_user), db: Session = Depends(get_db)):
    A.invalidate_user_sessions(db, user.id)
    db.commit()
    resp = redirect("/login", "Je bent op alle apparaten uitgelogd.")
    A.clear_session_cookie(resp)
    return resp
