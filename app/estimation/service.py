"""Koppelt de schatting aan de database (instellingen en kalibratie per gebruiker)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import Clip
from app.core.store import user_settings
from app.estimation.calibration import Calibration, Point, fit
from app.estimation.estimator import estimate



def user_points(db: Session, owner_id: int) -> list[Point]:
    baseline = user_settings(db, owner_id).channel_avg_views
    rows = db.scalars(
        select(Clip).where(Clip.owner_id == owner_id, Clip.actual_views.is_not(None), Clip.posted_at.is_not(None))
    ).all()
    return [
        Point(c.viral_score, baseline, c.actual_views, c.est_mid_at_post, c.est_low_at_post, c.est_high_at_post)
        for c in rows
    ]


def user_calibration(db: Session, owner_id: int) -> Calibration:
    return fit(user_points(db, owner_id))


def clip_estimate(db: Session, clip: Clip, cal: Calibration | None = None) -> dict:
    baseline = user_settings(db, clip.owner_id).channel_avg_views
    if cal is None:
        cal = user_calibration(db, clip.owner_id)
    return estimate(clip.viral_score, baseline, cal if cal.method != "standaard" else None)
