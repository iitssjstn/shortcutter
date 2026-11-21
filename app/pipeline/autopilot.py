"""Autopilot: periodiek nieuwe top-clips van gekozen (toegestane) Twitch-kanalen ophalen en er shorts van maken.

Alleen kanalen uit je eigen lijst (eigen kanaal of met toestemming). Clips die al eens zijn omgezet
worden overgeslagen. De shorts komen daarna in de nakijk-lijst; uploaden doe je zelf (of met één klik).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.models import AutoPilot, Project, TwitchChannel, utcnow
from app.integrations import twitch

log = logging.getLogger(__name__)
EVERY_CHOICES = (3, 6, 12, 24, 48)


def autopilot_for(db: Session, owner_id: int) -> AutoPilot:
    ap = db.scalar(select(AutoPilot).where(AutoPilot.owner_id == owner_id))
    if ap is None:
        ap = AutoPilot(owner_id=owner_id)
        db.add(ap)
        db.flush()
    return ap


def is_due(ap: AutoPilot, now: datetime | None = None) -> bool:
    if not ap.enabled:
        return False
    now = now or utcnow()
    return ap.last_run_at is None or now - ap.last_run_at >= timedelta(hours=max(1, ap.every_hours or 12))


def run(db: Session, ap: AutoPilot, now: datetime | None = None) -> list[int]:
    """Eén ronde: geeft de ids van de nieuwe projecten terug en legt het resultaat vast."""
    from app.api.twitch import create_twitch_project
    from app.core.store import get_global_int, user_settings
    from app.pipeline.run import ACTIVE_STATUSES

    ap.last_run_at = now or utcnow()
    chans = list(db.scalars(select(TwitchChannel).where(TwitchChannel.owner_id == ap.owner_id,
                                                        TwitchChannel.id.in_(ap.channel_ids or [0]))).all())
    if not chans:
        ap.last_result = "Geen kanalen gekozen."
        db.commit()
        return []
    done_urls = twitch.imported_urls(db, ap.owner_id)
    found = []
    try:
        for ch in chans:
            for clip in twitch.get_clips(db, ch.broadcaster_id, ap.period, first=30):
                if (clip.get("broadcaster_id") == ch.broadcaster_id and clip["url"] not in done_urls
                        and clip["views"] >= (ap.min_views or 0) and clip.get("duration", 0) >= 3):
                    found.append((clip, ch))
    except twitch.TwitchError as exc:
        ap.last_result = f"Twitch: {exc}"
        db.commit()
        return []
    found.sort(key=lambda x: x[0]["views"], reverse=True)
    limit = get_global_int(db, "max_active_per_user")
    active = db.scalar(select(func.count()).select_from(Project).where(
        Project.owner_id == ap.owner_id, Project.status.in_(ACTIVE_STATUSES))) or 0
    style = user_settings(db, ap.owner_id).caption_style
    made: list[int] = []
    seen = set()
    for clip, ch in found:
        if len(made) >= max(1, ap.max_per_run or 5):
            break
        if clip["url"] in seen:
            continue
        if limit > 0 and active >= limit:
            break
        seen.add(clip["url"])
        made.append(create_twitch_project(db, ap.owner_id, clip, ch, ap.framing or "split", style).id)
        active += 1
    ap.last_result = (f"{len(made)} nieuwe clip(s) in de wachtrij gezet." if made
                      else "Geen nieuwe clips gevonden die aan de voorwaarden voldoen.")
    db.commit()
    if made:
        from app.integrations import discord

        discord.notify(db, ap.owner_id, f"Autopilot: {len(made)} nieuwe clip(s) opgehaald",
                       "Ze worden nu verwerkt; je krijgt een melding als ze klaarstaan om na te kijken.", "info")
    return made


def run_due(db: Session, now: datetime | None = None) -> int:
    n = 0
    for ap in db.scalars(select(AutoPilot).where(AutoPilot.enabled.is_(True))).all():
        if not is_due(ap, now):
            continue
        try:
            run(db, ap, now)
            n += 1
        except Exception:  # noqa: BLE001 - de worker mag nooit stoppen
            db.rollback()
            log.exception("Autopilot voor gebruiker %s mislukt", ap.owner_id)
            ap = db.get(AutoPilot, ap.id)
            if ap is not None:
                ap.last_run_at = now or utcnow()
                ap.last_result = "Onverwachte fout."
                db.commit()
    return n
