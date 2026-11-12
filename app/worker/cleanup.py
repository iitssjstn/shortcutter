"""Retentie: bronbestanden (en optioneel hele projecten) opruimen, plus verlopen sessies."""
from __future__ import annotations

import logging
import shutil
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.models import Invite, LoginAttempt, Project, UserSession, utcnow
from app.core.paths import project_input, project_output
from app.core.store import get_global_int
from app.pipeline.run import ACTIVE_STATUSES

log = logging.getLogger(__name__)


def run_cleanup(db: Session) -> dict:
    now = utcnow()
    stats = {"sources_removed": 0, "projects_removed": 0}
    src_days = get_global_int(db, "source_retention_days")
    proj_days = get_global_int(db, "project_retention_days")

    projects = db.scalars(select(Project).where(Project.status.not_in(ACTIVE_STATUSES))).all()
    for p in projects:
        ref = p.finished_at or p.updated_at or p.created_at
        age = now - ref
        if proj_days > 0 and age > timedelta(days=proj_days):
            shutil.rmtree(project_input(p.id), ignore_errors=True)
            shutil.rmtree(project_output(p.id), ignore_errors=True)
            db.delete(p)
            stats["projects_removed"] += 1
            continue
        if src_days > 0 and age > timedelta(days=src_days) and p.status == "done":
            d = project_input(p.id)
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                p.source_deleted = True
                stats["sources_removed"] += 1

    days = get_settings().session_days
    db.execute(delete(UserSession).where(UserSession.last_seen_at < now - timedelta(days=days)))
    db.execute(delete(Invite).where(Invite.expires_at < now - timedelta(days=7)))
    db.execute(
        delete(LoginAttempt).where(
            LoginAttempt.window_start < now - timedelta(days=1),
            (LoginAttempt.locked_until.is_(None)) | (LoginAttempt.locked_until < now),
        )
    )
    db.commit()
    if stats["sources_removed"] or stats["projects_removed"]:
        log.info("Opruimen: %s", stats)
    return stats
