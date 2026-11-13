"""Ophalen van actieve sleutels voor de worker. Ontsleutelt alleen in het geheugen."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.models import Credential, utcnow
from app.core.store import get_global
from app.pipeline.pick import LLMConfig


def active_llm(db: Session) -> Credential | None:
    return db.scalar(
        select(Credential).where(Credential.provider == "llm", Credential.is_active.is_(True))
    )


def parse_models(text: str | None) -> list[str]:
    return [m.strip() for m in (text or "").replace("\n", ",").split(",") if m.strip()][:5]


def load_llm_config(db: Session, task: str = "pick") -> LLMConfig | None:
    """task: 'pick' (clipkeuze) of 'copy' (posttekst). Elke taak kan eigen modellen hebben."""
    cred = active_llm(db)
    if cred is None:
        return None
    try:
        key = crypto.decrypt_str(cred.encrypted_value)
    except RuntimeError:
        return None
    cred.last_used_at = utcnow()
    db.commit()
    models = (
        parse_models(get_global(db, f"llm_model_{task}"))
        or parse_models(get_global(db, "llm_model_override"))  # oude instelling
        or parse_models(cred.model)
        or ["llama-3.3-70b-versatile"]
    )
    return LLMConfig(base_url=cred.base_url or "https://api.groq.com/openai/v1", model=models[0], api_key=key,
                     fallbacks=tuple(models[1:]))


def youtube_key(db: Session) -> str | None:
    cred = db.scalar(select(Credential).where(Credential.provider == "youtube"))
    if cred is None:
        return None
    cred.last_used_at = utcnow()
    db.commit()
    return crypto.decrypt_str(cred.encrypted_value)


@contextmanager
def cookies_file(db: Session, workdir: Path):
    """Schrijf de cookies.txt tijdelijk (chmod 600) naar schijf en verwijder hem daarna."""
    cred = db.scalar(select(Credential).where(Credential.provider == "cookies"))
    if cred is None:
        yield None
        return
    data = crypto.decrypt(cred.encrypted_value)
    cred.last_used_at = utcnow()
    db.commit()
    workdir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".cookies-", suffix=".txt", dir=str(workdir))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        yield Path(name)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
