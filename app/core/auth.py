"""Sessies, CSRF-bescherming, rate limiting en rechten-dependencies."""
from __future__ import annotations

import hmac
import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import token_hash
from app.core.db import get_db
from app.core.models import LoginAttempt, User, UserSession, utcnow

SESSION_COOKIE = "sc_session"
CSRF_COOKIE = "sc_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"

# Rate limiting
USER_MAX_FAILURES = 5
IP_MAX_FAILURES = 20
WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)


# ---------------------------------------------------------------- sessies
def create_session(db: Session, user: User, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            token_hash=token_hash(token),
            user_id=user.id,
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:250],
        )
    )
    user.last_active_at = utcnow()
    db.commit()
    return token


def set_session_cookie(response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=s.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=s.cookie_secure,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def invalidate_user_sessions(db: Session, user_id: int) -> None:
    db.execute(delete(UserSession).where(UserSession.user_id == user_id))


def delete_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))
        db.commit()


def lookup_session(db: Session, token: str | None) -> UserSession | None:
    if not token or len(token) > 200:
        return None
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
    if sess is None:
        return None
    now = utcnow()
    if now - sess.last_seen_at > timedelta(days=get_settings().session_days):
        db.delete(sess)
        db.commit()
        return None
    user = sess.user
    if user is None or not user.is_active:
        db.delete(sess)
        db.commit()
        return None
    if now - sess.last_seen_at > timedelta(seconds=60):
        sess.last_seen_at = now
        user.last_active_at = now
        db.commit()
    return sess


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "onbekend"


# ---------------------------------------------------------------- CSRF
def ensure_csrf_cookie(request: Request) -> str:
    tok = request.cookies.get(CSRF_COOKIE)
    if not tok or len(tok) < 20:
        tok = getattr(request.state, "new_csrf", None) or secrets.token_urlsafe(32)
        request.state.new_csrf = tok
    return tok


async def csrf_protect(request: Request) -> None:
    """App-brede dependency: weigert POST/PUT/PATCH/DELETE zonder geldig CSRF-token."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    sent = request.headers.get(CSRF_HEADER)
    if not sent:
        ctype = request.headers.get("content-type", "")
        if "form" in ctype:
            form = await request.form()
            val = form.get(CSRF_FIELD)
            sent = val if isinstance(val, str) else None
    if not cookie or not sent or not hmac.compare_digest(cookie.encode(), sent.encode()):
        raise HTTPException(status_code=403, detail="Ongeldig of ontbrekend CSRF-token. Herlaad de pagina.")


# ---------------------------------------------------------------- rate limiting
def _attempt(db: Session, key: str) -> LoginAttempt:
    att = db.scalar(select(LoginAttempt).where(LoginAttempt.key == key))
    if att is None:
        att = LoginAttempt(key=key, failures=0, window_start=utcnow())
        db.add(att)
        db.flush()
    return att


def is_locked(db: Session, keys: list[str]) -> bool:
    now = utcnow()
    for key in keys:
        att = db.scalar(select(LoginAttempt).where(LoginAttempt.key == key))
        if att and att.locked_until and att.locked_until > now:
            return True
    return False


def register_failure(db: Session, keys: list[str]) -> None:
    now = utcnow()
    for key in keys:
        att = _attempt(db, key)
        if now - att.window_start > WINDOW:
            att.failures = 0
            att.window_start = now
        att.failures += 1
        limit = IP_MAX_FAILURES if key.startswith("ip:") else USER_MAX_FAILURES
        if att.failures >= limit:
            att.locked_until = now + LOCKOUT
            att.failures = 0
            att.window_start = now
    db.commit()


def reset_failures(db: Session, key: str) -> None:
    db.execute(delete(LoginAttempt).where(LoginAttempt.key == key))
    db.commit()


def rate_keys(request: Request, username: str | None, scope: str = "login") -> list[str]:
    keys = [f"ip:{scope}:{client_ip(request)}"]
    if username:
        keys.append(f"user:{scope}:{username.strip().lower()[:64]}")
    return keys


# ---------------------------------------------------------------- dependencies
class LoginRequired(Exception):
    pass


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    if hasattr(request.state, "user"):
        return request.state.user
    sess = lookup_session(db, request.cookies.get(SESSION_COOKIE))
    user = sess.user if sess else None
    request.state.user = user
    return user


def require_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Alleen een beheerder kan dit doen.")
    return user
