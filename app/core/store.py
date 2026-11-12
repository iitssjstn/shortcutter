"""Hulpfuncties voor instellingen, auditlog en eigendomscontroles."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.models import AuditLog, Clip, GlobalSetting, Project, User, UserSettings

GLOBAL_DEFAULTS: dict[str, str] = {
    "whisper_model": "",  # leeg = WHISPER_MODEL uit de omgeving
    "llm_model_override": "",
    "max_active_per_user": "0",  # 0 = onbeperkt
    "source_retention_days": "7",  # 0 = nooit verwijderen
    "project_retention_days": "0",  # 0 = clips altijd bewaren
}

WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3"]
CAPTION_STYLES = {
    "geel": "Wit met gele highlight",
    "groen": "Wit met groene highlight",
    "wit": "Alleen wit (geen highlight)",
}


def get_global(db: Session, key: str) -> str:
    row = db.get(GlobalSetting, key)
    return row.value if row else GLOBAL_DEFAULTS.get(key, "")


def get_global_int(db: Session, key: str) -> int:
    try:
        return int(get_global(db, key))
    except ValueError:
        return int(GLOBAL_DEFAULTS.get(key, "0") or 0)


def set_global(db: Session, key: str, value: str) -> None:
    row = db.get(GlobalSetting, key)
    if row is None:
        db.add(GlobalSetting(key=key, value=value))
    else:
        row.value = value


def effective_whisper_model(db: Session) -> str:
    return get_global(db, "whisper_model") or get_settings().whisper_model


def user_settings(db: Session, user_id: int) -> UserSettings:
    us = db.get(UserSettings, user_id)
    if us is None:
        us = UserSettings(user_id=user_id)
        db.add(us)
        db.commit()
    return us


def audit(db: Session, user: User | None, action: str, target: str) -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action=action,
            target=target[:200],
        )
    )


def get_project_for(db: Session, user: User, project_id: int) -> Project:
    """Geeft 404 als het project niet bestaat of niet van deze gebruiker is (admin ziet alles)."""
    project = db.get(Project, project_id)
    if project is None or (project.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Project niet gevonden.")
    return project


def get_clip_for(db: Session, user: User, clip_id: int) -> Clip:
    clip = db.get(Clip, clip_id)
    if clip is None or (clip.owner_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Clip niet gevonden.")
    return clip


def active_admin_count(db: Session) -> int:
    return len(db.scalars(select(User.id).where(User.role == "admin", User.is_active.is_(True))).all())
