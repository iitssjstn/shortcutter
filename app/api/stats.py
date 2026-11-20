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
STATS_ROWS = 50  # rijen in de tabel; de rest via 'alles tonen'


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


@router.post("/stats/sync")
def stats_sync(user: User = Depends(require_user)):
    from app.api.common import redirect
    from app.estimation.insights import start_background

    if not start_background(user.id, sync=True):
        return redirect("/stats#lessen", "Er loopt al een update; even geduld.")
    return redirect("/stats#lessen", "Views worden op de achtergrond opgehaald. De pagina ververst vanzelf.")


@router.post("/stats/insights")
def stats_insights(user: User = Depends(require_user)):
    from app.api.common import redirect
    from app.estimation.insights import start_background

    if not start_background(user.id, sync=False, force=True):
        return redirect("/stats#lessen", "Er loopt al een update; even geduld.")
    return redirect("/stats#lessen", "De AI analyseert je resultaten op de achtergrond. De pagina ververst vanzelf.")


@router.get("/stats/insights-status")
def stats_insights_status(user: User = Depends(require_user)):
    """HTMX-poll: zolang er een run bezig is 204, daarna een volledige verversing."""
    from fastapi.responses import Response

    from app.estimation.insights import is_running

    if is_running(user.id):
        return Response(status_code=204)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.get("/stats")
def stats_page(request: Request, all: int = 0, user: User = Depends(require_user), db: Session = Depends(get_db)):
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
            "posted_shown": posted if all else posted[:STATS_ROWS],
            "with_views": with_views,
            "acc": acc,
            "cal": cal,
            "breakdown": breakdown,
            "baseline": baseline,
            "min_median": MIN_POINTS_MEDIAN,
            "min_regression": MIN_POINTS_REGRESSION,
            "insights": _insights_ctx(db, user.id),
            **_analytics_ctx(db, user.id, posted),
        },
    )


def _analytics_ctx(db: Session, user_id: int, posted: list) -> dict:
    from app.core.models import YoutubeAccount
    from app.estimation import analytics

    acc = db.scalar(select(YoutubeAccount).where(YoutubeAccount.user_id == user_id))
    return {
        "yt_connected": acc is not None,
        "yt_analytics": bool(acc and acc.has_analytics),
        "ana": analytics.summary(posted),
        "diagnose": {c.id: analytics.diagnose(c) for c in posted},
    }


def _insights_ctx(db: Session, user_id: int) -> dict:
    from app.estimation.insights import MIN_POINTS, is_running, last_result, performance_rows, stored

    st = stored(db, user_id)
    us = user_settings(db, user_id)
    return {
        "lessons": st.get("lessons") or [],
        "based_on": st.get("n"),
        "generated_at": (st.get("generated_at") or "").replace("T", " "),
        "points": len(performance_rows(db, user_id)),
        "min_points": MIN_POINTS,
        "synced_at": us.stats_synced_at,
        "running": is_running(user_id),
        "result": last_result(user_id),
    }
