"""Versleuteling van API-sleutels met Fernet.

De master key komt uit MASTER_KEY_FILE (standaard /run/secrets/app_master_key, een
Docker secret). Bestaat dat bestand niet, dan wordt bij de eerste start een sleutel
gegenereerd in /data/.master_key (chmod 600) en daarna hergebruikt.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings

log = logging.getLogger(__name__)

_fernet: Fernet | None = None
_key_source: str = ""


def _normalize_key(raw: bytes) -> bytes:
    raw = raw.strip()
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except (ValueError, TypeError):
        pass
    # Willekeurige tekst als secret: leid er een geldige Fernet-sleutel van af.
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def load_master_key(settings: Settings) -> Fernet:
    global _fernet, _key_source
    secret = settings.master_key_file
    if secret.is_file() and secret.read_bytes().strip():
        key = _normalize_key(secret.read_bytes())
        _key_source = "docker-secret"
    else:
        path = settings.generated_master_key
        if path.is_file() and path.read_bytes().strip():
            key = _normalize_key(path.read_bytes())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
            log.info("Nieuwe master key gegenereerd in %s", path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _key_source = "bestand"
    _fernet = Fernet(key)
    return _fernet


def key_source() -> str:
    return _key_source


def _f() -> Fernet:
    if _fernet is None:
        raise RuntimeError("Master key is niet geladen")
    return _fernet


def encrypt(value: str | bytes) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return _f().encrypt(data)


def decrypt(token: bytes) -> bytes:
    try:
        return _f().decrypt(token)
    except InvalidToken as exc:  # bv. andere master key
        raise RuntimeError(
            "Kan de opgeslagen sleutel niet ontsleutelen (is de master key gewijzigd?)"
        ) from exc


def decrypt_str(token: bytes) -> str:
    return decrypt(token).decode()


def mask_secret(value: str) -> str:
    """Bijv. 'gsk_abcdef…a1b2' -> 'gsk_…a1b2'. Toont nooit meer dan prefix + 4 tekens."""
    value = value.strip()
    if len(value) <= 8:
        return "…" + value[-2:] if len(value) > 4 else "…"
    prefix = ""
    for sep in ("_", "-"):
        head, s, _ = value.partition(sep)
        if s and 1 <= len(head) <= 5:
            prefix = head + sep
            break
    return f"{prefix}…{value[-4:]}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
