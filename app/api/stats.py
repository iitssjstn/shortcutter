"""Statistieken: schatting versus echte views, nauwkeurigheid en best presterende soorten."""
from __future__ import annotations

import statistics
from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import render
from app.core.auth import require_user
from app.core.db import get_db
from app.core.models import Clip, User
from app.estimation.calibration import MIN_POINTS_MEDIAN, MIN_POINTS_REGRESSION, Point, accuracy
from app.estimation.service import user_calibration
from app.core.store import user_settings

router = APIRouter()


def _dur_bucket(d: float) -> str:
    if d < 20:
        return "< 20 s"
    if d < 35:
        return "20–35 s"
    if d < 50:
        return "35–50 s"
    return "≥ 50 s"


def _hook_type(c: Clip) -> str:
    f = c.features
    if f.get("vraag_in_hook"):
        return "vraag in hook"
    if f.get("claim_in_hook"):
        return "sterke claim in hook"
    return "geen vraag/claim"


@router.get("/stats")
def stats_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    posted = list(
        db.scalars(
            select(Clip).where(Clip.owner_id == user.id, Clip.posted_at.is_not(None)).order_by(Clip.posted_at.desc())
        ).all()
    )
    with_views = [c for c in posted if c.actual_views is not None]
    baseline = user_settings(db, user.id).channel_avg_views
    pts = [Point(c.viral_score, baseline, c.actual_views, c.est_mid_at_post, c.est_low_at_post, c.est_high_at_post)
           for c in with_views]
    acc = accuracy(pts)
    cal = user_calibration(db, user.id)

    groups: dict[str, dict[str, list[Clip]]] = {
        "Categorie": defaultdict(list),
        "Duur": defaultdict(list),
        "Hook": defaultdict(list),
        "Framing": defaultdict(list),
        "Gekozen door": defaultdict(list),
    }
    for c in with_views:
        groups["Categorie"][c.category or "overig"].append(c)
        groups["Duur"][_dur_bucket(c.duration)].append(c)
        groups["Hook"][_hook_type(c)].append(c)
        groups["Framing"][{"crop": "crop rond gezicht", "blur": "wazige achtergrond", "vertical": "staand", "split": "gesplitst"}.get(c.framing_mode or "", "onbekend")].append(c)
        groups["Gekozen door"][c.source].append(c)
    breakdown = {}
    for name, g in groups.items():
        rows = []
        for key, clips in g.items():
            views = [c.actual_views for c in clips]
            ratios = [c.actual_views / c.est_mid_at_post for c in clips if c.est_mid_at_post]
            rows.append(
                {
                    "key": key,
                    "n": len(clips),
                    "median_views": int(statistics.median(views)),
                    "ratio": round(statistics.median(ratios), 2) if ratios else None,
                }
            )
        breakdown[name] = sorted(rows, key=lambda r: r["median_views"], reverse=True)

    return render(
        request,
        "stats.html",
        {
            "nav": "stats",
            "posted": posted,
            "with_views": with_views,
            "acc": acc,
            "cal": cal,
            "breakdown": breakdown,
            "baseline": baseline,
            "min_median": MIN_POINTS_MEDIAN,
            "min_regression": MIN_POINTS_REGRESSION,
        },
    )
