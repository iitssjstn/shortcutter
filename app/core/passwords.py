"""Wachtwoord-hashing met argon2id. Wachtwoorden worden nooit gelogd."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

MIN_LENGTH = 12

_hasher = PasswordHasher(type=Type.ID)

# Een vaste dummy-hash zodat een onbekende gebruiker even lang duurt als een fout wachtwoord.
_DUMMY_HASH = _hasher.hash("dummy-wachtwoord-voor-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        try:
            _hasher.verify(_DUMMY_HASH, password + "x")
        except (VerificationError, InvalidHashError):
            pass
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_new_password(password: str, repeat: str) -> str | None:
    """Geeft een foutmelding terug, of None als het wachtwoord goed is."""
    if password != repeat:
        return "De wachtwoorden komen niet overeen."
    if len(password) < MIN_LENGTH:
        return f"Het wachtwoord moet minimaal {MIN_LENGTH} tekens lang zijn."
    if len(password) > 1024:
        return "Het wachtwoord is te lang."
    return None
