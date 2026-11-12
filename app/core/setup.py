"""Eerste setup: eenmalige setupcode die in de containerlogs verschijnt."""
from __future__ import annotations

import hmac
import logging
import secrets
import sys

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.models import SetupLock, User, UserSettings
from app.core.passwords import hash_password

log = logging.getLogger("shortcutter.setup")

_setup_code: str | None = None
_has_users_cache = False


def has_users(db: Session) -> bool:
    global _has_users_cache
    if _has_users_cache:
        return True
    count = db.scalar(select(func.count()).select_from(User)) or 0
    if count:
        _has_users_cache = True
    return bool(count)


def reset_cache() -> None:
    global _has_users_cache, _setup_code
    _has_users_cache = False
    _setup_code = None


def ensure_setup_code(db: Session) -> str | None:
    """Genereer en print de setupcode als er nog geen gebruiker bestaat."""
    global _setup_code
    if has_users(db):
        _setup_code = None
        return None
    if _setup_code is None:
        _setup_code = secrets.token_urlsafe(16)
    banner = (
        "\n" + "=" * 64 + "\n"
        "  SHORTCUTTER EERSTE SETUP\n"
        f"  Setupcode: {_setup_code}\n"
        "  Open http://<server>:8000/setup en vul deze code in.\n"
        "  De code vervalt zodra het eerste account bestaat.\n"
        + "=" * 64 + "\n"
    )
    print(banner, file=sys.stderr, flush=True)
    return _setup_code


def current_code() -> str | None:
    return _setup_code


def check_code(code: str) -> bool:
    if not _setup_code or not code:
        return False
    return hmac.compare_digest(_setup_code.encode(), code.strip().encode())


class SetupError(Exception):
    pass


def create_first_admin(db: Session, username: str, email: str | None, password: str) -> User:
    """Maakt het eerste account aan in één transactie met de SetupLock-rij.

    Twee gelijktijdige pogingen kunnen nooit allebei slagen: de tweede faalt op het
    primaire-sleutel-constraint van setup_lock (id=1).
    """
    global _setup_code, _has_users_cache
    try:
        if db.scalar(select(func.count()).select_from(User)):
            raise SetupError("Er bestaat al een account.")
        db.add(SetupLock(id=1))
        user = User(username=username, email=email or None, password_hash=hash_password(password), role="admin")
        db.add(user)
        db.flush()
        db.add(UserSettings(user_id=user.id))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SetupError("De setup is al voltooid.") from exc
    _setup_code = None
    _has_users_cache = True
    log.info("Eerste account aangemaakt: %s", username)
    return user
