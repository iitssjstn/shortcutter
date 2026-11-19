"""Aanbevolen uploadmomenten.

Met genoeg eigen data (geposte clips met views) kiest dit de uren waarop jouw clips het best liepen;
anders standaardtijden die passen bij de taal van je posts (Engels: Amerikaanse middag/avond,
Nederlands: lunch en avond). Daarbij: minstens `min_gap_hours` tussen uploads en hooguit
`uploads_per_day` per dag, ook rekening houdend met wat al ingepland staat.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import Clip, Compilation
from app.core.store import user_settings

MIN_POINTS = 10
# lokale tijd (Europe/Amsterdam): 18-21 u NL = 12-15 u ET (VS-lunch/middag), 23 u = 17 u ET
DEFAULT_HOURS = {"en": [18, 21, 23], "nl": [12, 17, 20], "auto": [17, 20, 22]}


def _tz():
    from app.api.youtube import local_tz

    return local_tz()


def _when_published(c) -> datetime | None:
    """Publicatiemoment (UTC, naïef): de geplande tijd als die er is, anders het moment van posten."""
    try:
        opts = json.loads(c.yt_options_json or "{}")
    except ValueError:
        opts = {}
    if opts.get("publish_at"):
        try:
            return datetime.strptime(opts["publish_at"], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return getattr(c, "posted_at", None)


def best_hours(db: Session, owner_id: int) -> tuple[list[int], str]:
    """(uren in lokale tijd, uitleg)."""
    tz = _tz()
    rows = db.scalars(select(Clip).where(Clip.owner_id == owner_id, Clip.posted_at.is_not(None),
                                         Clip.actual_views.is_not(None))).all()
    pts = []
    for c in rows:
        t = _when_published(c)
        if t is not None:
            pts.append((t.replace(tzinfo=timezone.utc).astimezone(tz).hour, c.actual_views or 0))
    if len(pts) >= MIN_POINTS:
        overall = statistics.median(v for _, v in pts) or 1
        buckets: dict[int, list[int]] = {}
        for h, v in pts:
            buckets.setdefault(h // 3, []).append(v)  # vensters van 3 uur
        scored = sorted(((statistics.median(vs) / overall, b) for b, vs in buckets.items() if len(vs) >= 2), reverse=True)
        if len(scored) >= 2:
            hours = [b * 3 + 1 for _, b in scored[:3]]  # midden van het venster
            best = scored[0]
            return sorted(hours), (f"Op basis van je eigen {len(pts)} clips: rond {best[1] * 3:02d}:00–{best[1] * 3 + 3:02d}:00 "
                                   f"liepen ze {best[0]:.1f}× zo goed als gemiddeld.")
    lang = user_settings(db, owner_id).post_language or "en"
    hours = DEFAULT_HOURS.get(lang, DEFAULT_HOURS["en"])
    why = ("Standaardtijden voor een Engelstalig publiek (vooral VS: middag en vroege avond daar)."
           if lang == "en" else "Standaardtijden voor een Nederlandstalig publiek (lunch en avond).")
    return hours, why + f" Vanaf {MIN_POINTS} geposte clips met views gebruik ik je eigen resultaten."


def taken_slots(db: Session, owner_id: int) -> list[datetime]:
    """Al geplande of recent geplaatste uploads (UTC, naïef)."""
    now = datetime.utcnow()
    out = []
    for c in db.scalars(select(Clip).where(Clip.owner_id == owner_id, Clip.yt_options_json.is_not(None))):
        if c.yt_status in ("queued", "uploading", "done"):
            t = _when_published(c)
            if t and t > now - timedelta(days=1):
                out.append(t)
    for comp in db.scalars(select(Compilation).where(Compilation.owner_id == owner_id,
                                                     Compilation.yt_options_json.is_not(None))):
        if comp.yt_status in ("queued", "uploading", "done"):
            t = _when_published(comp) or comp.updated_at
            if t and t > now - timedelta(days=1):
                out.append(t)
    return out


def next_slots(db: Session, owner_id: int, n: int, now_utc: datetime | None = None) -> tuple[list[datetime], str]:
    """De eerstvolgende `n` aanbevolen momenten (lokale tijd, tz-bewust) plus uitleg."""
    tz = _tz()
    us = user_settings(db, owner_id)
    per_day = max(1, min(10, us.uploads_per_day or 3))
    gap = timedelta(hours=max(1, min(24, us.min_gap_hours or 3)))
    hours, why = best_hours(db, owner_id)
    now = (now_utc or datetime.utcnow()).replace(tzinfo=timezone.utc).astimezone(tz)
    earliest = now + timedelta(minutes=30)  # YouTube wil minstens ±15 min vooruit
    taken = [t.replace(tzinfo=timezone.utc).astimezone(tz) for t in taken_slots(db, owner_id)]
    out: list[datetime] = []
    day = earliest.date()
    for _ in range(60):  # hooguit 60 dagen vooruit zoeken
        for h in sorted(set(hours)):
            cand = datetime(day.year, day.month, day.day, h, 0, tzinfo=tz)
            if cand < earliest:
                continue
            same_day = [t for t in taken + out if t.date() == cand.date()]
            if len(same_day) >= per_day:
                break
            if any(abs(cand - t) < gap for t in taken + out):
                continue
            out.append(cand)
            if len(out) >= n:
                return out, why
        day += timedelta(days=1)
    return out, why
