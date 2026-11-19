"""Compilatie-concepten maken uit Twitch-clips, handmatig of wekelijks automatisch.

De planning maakt elke week op een vast moment een concept met de best bekeken clips
van de gekozen kanalen en rendert dat (optioneel) meteen. Uploaden gebeurt nooit
automatisch: je kijkt de video eerst na.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import Compilation, CompilationItem, CompilationSchedule, TwitchChannel, utcnow
from app.integrations import twitch
from app.pipeline import compile as cp
from app.pipeline.download import twitch_clip_slug

log = logging.getLogger(__name__)
WEEKDAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


class DraftError(Exception):
    pass


def item_from_clip(clip: dict, ch: TwitchChannel, game: str | None, pos: int) -> CompilationItem:
    return CompilationItem(
        position=pos, clip_url=clip["url"], slug=clip["id"] or twitch_clip_slug(clip["url"]) or "",
        title=(clip.get("title") or "Twitch-clip")[:200], streamer=(clip.get("broadcaster_name") or ch.display_name)[:80],
        channel_login=ch.login, broadcaster_id=ch.broadcaster_id, game=(game or None),
        views=int(clip.get("views") or 0), duration=float(clip.get("duration") or 0),
        thumbnail_url=(clip.get("thumbnail") or None),
    )


def build_draft(db: Session, owner_id: int, channels: list[TwitchChannel], period: str, count: int,
                ranking: str, fmt: str = "landscape") -> Compilation:
    """Zoek de best bekeken clips van deze (toegestane) kanalen en maak er een concept van."""
    fmt = fmt if fmt in cp.FORMATS else "landscape"
    if fmt == "short":
        count = min(count, cp.MAX_SHORT_ITEMS)
    if not channels:
        raise DraftError("Kies minstens één kanaal uit je lijst.")
    if any(ch.owner_id != owner_id for ch in channels):
        raise DraftError("Kies minstens één kanaal uit je lijst.")
    found: list[tuple[dict, TwitchChannel]] = []
    try:
        for ch in channels:
            for clip in twitch.get_clips(db, ch.broadcaster_id, period, first=max(20, count * 2)):
                if clip.get("broadcaster_id") == ch.broadcaster_id:  # alleen clips van dit (toegestane) kanaal
                    found.append((clip, ch))
    except twitch.TwitchError as exc:
        raise DraftError(str(exc)) from exc
    found.sort(key=lambda x: x[0]["views"], reverse=True)
    seen, top = set(), []
    for clip, ch in found:
        if clip["id"] not in seen and clip.get("duration", 0) >= 3:
            seen.add(clip["id"])
            top.append((clip, ch))
        if len(top) >= count:
            break
    if len(top) < 2:
        raise DraftError("Te weinig clips gevonden in deze periode. Kies een langere periode of meer kanalen.")
    if ranking == "countdown":
        top.reverse()  # minst bekeken eerst, #1 als laatste
    lang = cp._lang(db, owner_id)
    comp = Compilation(owner_id=owner_id, period=period, ranking=ranking, format=fmt,
                       intro=fmt != "short", outro=fmt != "short",  # een short begint meteen met de eerste clip
                       title=cp.default_title(period, [c.get("broadcaster_name", "") for c, _ in top], lang, fmt))
    games: dict[str, str] = {}
    for k, (clip, ch) in enumerate(top):
        gid = clip.get("game_id") or ""
        if gid and gid not in games:
            try:
                games[gid] = twitch.get_game_name(db, gid) or ""
            except twitch.TwitchError:
                games[gid] = ""
        comp.items.append(item_from_clip(clip, ch, games.get(gid), k))
    db.add(comp)
    db.commit()
    return comp


# ---------------------------------------------------------------- planning
def schedule_for(db: Session, owner_id: int) -> CompilationSchedule:
    s = db.scalar(select(CompilationSchedule).where(CompilationSchedule.owner_id == owner_id))
    if s is None:
        s = CompilationSchedule(owner_id=owner_id)
        db.add(s)
        db.flush()
    return s


def _tz():
    from app.api.youtube import local_tz

    return local_tz()


def last_slot(s: CompilationSchedule, now_utc: datetime | None = None) -> datetime:
    """Meest recente geplande moment (UTC, naïef) op of vóór nu."""
    tz = _tz()
    now = (now_utc or utcnow()).replace(tzinfo=timezone.utc).astimezone(tz)
    slot = now.replace(hour=s.hour, minute=0, second=0, microsecond=0) - timedelta(days=(now.weekday() - s.weekday) % 7)
    if slot > now:
        slot -= timedelta(days=7)
    return slot.astimezone(timezone.utc).replace(tzinfo=None)


def next_slot(s: CompilationSchedule, now_utc: datetime | None = None) -> datetime:
    """Volgende geplande moment in lokale tijd (voor op de pagina)."""
    return (last_slot(s, now_utc) + timedelta(days=7)).replace(tzinfo=timezone.utc).astimezone(_tz())


def is_due(s: CompilationSchedule, now_utc: datetime | None = None) -> bool:
    if not s.enabled:
        return False
    slot = last_slot(s, now_utc)
    return s.last_run_at is None or s.last_run_at < slot


def run_schedule(db: Session, s: CompilationSchedule, now_utc: datetime | None = None) -> Compilation | None:
    """Maak (en render eventueel) het concept van deze week. Legt het resultaat vast in de planning."""
    s.last_run_at = now_utc or utcnow()
    chans = list(db.scalars(select(TwitchChannel).where(TwitchChannel.owner_id == s.owner_id,
                                                        TwitchChannel.id.in_(s.channel_ids or [0]))).all())
    try:
        comp = build_draft(db, s.owner_id, chans, "week", s.count, s.ranking, s.format or "landscape")
    except DraftError as exc:
        s.last_result = f"Geen compilatie gemaakt: {exc}"
        db.commit()
        return None
    comp.auto = True
    if comp.format != "short":
        comp.intro, comp.outro = s.intro, s.outro
    if s.auto_render:
        comp.status = "queued"
        comp.stage_message = "In wachtrij"
    s.last_compilation_id = comp.id
    s.last_result = f"Concept gemaakt met {len(comp.items)} clips" + (" en in de wachtrij gezet om te renderen." if s.auto_render else ".")
    db.commit()
    return comp


def run_due(db: Session, now_utc: datetime | None = None) -> int:
    """Worker: voer alle planningen uit die aan de beurt zijn. Geeft het aantal uitgevoerde terug."""
    n = 0
    for s in db.scalars(select(CompilationSchedule).where(CompilationSchedule.enabled.is_(True))).all():
        if not is_due(s, now_utc):
            continue
        try:
            run_schedule(db, s, now_utc)
            n += 1
        except Exception:  # noqa: BLE001 - een planning mag de worker nooit stoppen
            db.rollback()
            log.exception("Wekelijkse compilatie voor gebruiker %s mislukt", s.owner_id)
            s = db.get(CompilationSchedule, s.id)
            if s is not None:
                s.last_run_at = utcnow()
                s.last_result = "Onverwachte fout bij het maken van de compilatie."
                db.commit()
    return n
