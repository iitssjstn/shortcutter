"""YouTube Analytics per short: waarom wordt hij wel of niet verspreid?

Per geplaatste short (via de gekoppelde account, met het recht yt-analytics.readonly):
* gemiddeld bekeken deel en kijkduur;
* welk deel van de weergaven uit de Shorts-feed kwam (laag = YouTube heeft hem nauwelijks getest);
* hoeveel kijkers na ±3 seconden nog kijken (laag = het begin pakt niet; benadering van 'weggeswiped').

Daaruit volgt een korte diagnose per short. De cijfers gaan ook mee in de lessen voor de AI.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import Clip, YoutubeAccount, utcnow

log = logging.getLogger(__name__)

REPORTS = "https://youtubeanalytics.googleapis.com/v2/reports"
MAX_VIDEOS = 40  # per keer
DAYS_BACK = 90
HOOK_SECONDS = 3.0


def _get(client, headers: dict, params: dict) -> list[list] | None:
    r = client.get(REPORTS, params={"ids": "channel==MINE", **params}, headers=headers)
    if r.status_code != 200:
        log.warning("YouTube Analytics: HTTP %s", r.status_code)
        return None
    return r.json().get("rows") or []


def sync(db: Session, user_id: int) -> int:
    """Haal analytics op voor recente geplaatste shorts. Geeft het aantal bijgewerkte clips."""
    from app.estimation.insights import youtube_id
    from app.integrations import youtube_upload as yt

    acc = db.scalar(select(YoutubeAccount).where(YoutubeAccount.user_id == user_id))
    if acc is None or not acc.has_analytics:
        return 0
    since = utcnow() - timedelta(days=DAYS_BACK)
    clips = [c for c in db.scalars(select(Clip).where(Clip.owner_id == user_id, Clip.posted_at.is_not(None),
                                                      Clip.posted_at >= since)
                                   .order_by(Clip.posted_at.desc())).all() if youtube_id(c)][:MAX_VIDEOS]
    if not clips:
        return 0
    by_id = {youtube_id(c): c for c in clips}
    start = min(c.posted_at for c in clips).strftime("%Y-%m-%d")
    end = (utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    updated = 0
    with yt._client_factory() as client:
        headers = {"Authorization": f"Bearer {yt.access_token(db, acc, client)}"}
        base = {"startDate": start, "endDate": end}
        rows = _get(client, headers, {**base, "dimensions": "video", "sort": "-views", "maxResults": 200,
                                      "metrics": "views,averageViewDuration,averageViewPercentage",
                                      "filters": "video==" + ",".join(by_id)})
        if rows is None:
            return 0
        views: dict[str, int] = {}
        for vid, v, dur, pct in rows:
            c = by_id.get(vid)
            if c is None:
                continue
            views[vid] = int(v or 0)
            c.avg_view_dur = round(float(dur or 0), 1)
            c.avg_view_pct = round(float(pct or 0), 1)
            c.analytics_at = utcnow()
            updated += 1
        for vid, c in by_id.items():
            if views.get(vid, 0) < 1:
                continue
            src = _get(client, headers, {**base, "dimensions": "insightTrafficSourceType", "metrics": "views",
                                         "filters": f"video=={vid}"})
            if src:
                total = sum(int(r[1] or 0) for r in src) or 1
                shorts = sum(int(r[1] or 0) for r in src if str(r[0]).upper() == "SHORTS")
                c.shorts_feed_share = round(shorts / total, 3)
            ret = _get(client, headers, {**base, "dimensions": "elapsedVideoTimeRatio", "metrics": "audienceWatchRatio",
                                         "filters": f"video=={vid}"})
            if ret:
                length = max(1.0, (c.duration or 0) + (c.teaser_len or 0))
                target = min(0.99, HOOK_SECONDS / length)
                best = min(ret, key=lambda r: abs(float(r[0]) - target))
                c.hook_retention = round(min(1.5, float(best[1] or 0)), 3)
    db.commit()
    return updated


def diagnose(c: Clip) -> list[tuple[str, str]]:
    """Korte diagnose: [(soort, tekst)] met soort 'ok' | 'warn' | 'info'."""
    out: list[tuple[str, str]] = []
    views = c.actual_views or 0
    if c.analytics_at is None:
        return out
    if views < 20:
        return [("info", "nog te weinig weergaven om iets te zeggen")]
    if c.shorts_feed_share is not None and c.shorts_feed_share < 0.3:
        out.append(("warn", "nauwelijks getest in de Shorts-feed"))
    if c.hook_retention is not None and c.hook_retention < 0.65:
        out.append(("warn", "begin pakt niet: veel kijkers haken in de eerste seconden af"))
    if c.avg_view_pct is not None:
        if c.avg_view_pct >= 100:
            out.append(("ok", "wordt herbekeken"))
        elif c.avg_view_pct < 60:
            out.append(("warn", "kijkers haken halverwege af (te lang of geen afronding)"))
    if not out:
        out.append(("ok", "geen duidelijke zwakke plek"))
    return out


def summary(clips: list[Clip]) -> dict:
    """Overzicht over alle shorts met analytics."""
    rows = [c for c in clips if c.analytics_at is not None and (c.actual_views or 0) >= 20]

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "n": len(rows),
        "feed": avg(c.shorts_feed_share for c in rows),
        "hook": avg(c.hook_retention for c in rows),
        "pct": avg(c.avg_view_pct for c in rows),
        "low_feed": sum(1 for c in rows if c.shorts_feed_share is not None and c.shorts_feed_share < 0.3),
        "weak_hook": sum(1 for c in rows if c.hook_retention is not None and c.hook_retention < 0.65),
        "drop_mid": sum(1 for c in rows if c.avg_view_pct is not None and c.avg_view_pct < 60),
    }
