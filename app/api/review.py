"""Nakijken: alle klare, nog niet geüploade clips op één plek, met aanbevolen uploadtijd en autopilot."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.common import redirect, render
from app.core import crypto
from app.core.auth import require_user
from app.core.db import get_db
from app.core.models import Clip, Project, TwitchChannel, User
from app.core.store import FRAMING_MODES, get_clip_for, user_settings
from app.estimation import timing
from app.estimation.trends import normalize_tags
from app.integrations import discord, twitch
from app.integrations import youtube_upload as yt
from app.pipeline import autopilot
from app.pipeline.copywriting import normalize_hashtags

router = APIRouter()
PAGE = 20


def review_query(user_id: int):
    return (select(Clip).join(Project, Clip.project_id == Project.id)
            .where(Clip.owner_id == user_id, Clip.status.in_(("ready", "pending", "rendering")), Clip.posted_at.is_(None),
                   (Clip.yt_status.is_(None)) | (Clip.yt_status == "failed"),
                   (Clip.review_state.is_(None)), Project.status == "done"))


def review_count(db: Session, user_id: int) -> int:
    return db.scalar(select(func.count()).select_from(review_query(user_id).subquery())) or 0


@router.get("/review")
def review_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from app.api.youtube import account_for

    clips = list(db.scalars(review_query(user.id).options(selectinload(Clip.project))
                            .order_by(Project.created_at.desc(), Clip.index).limit(PAGE)).all())
    total = review_count(db, user.id)
    ready = [c for c in clips if c.status == "ready"]
    slots, why = timing.next_slots(db, user.id, max(1, len(ready)))
    slot_for = dict(zip((c.id for c in ready), slots))  # clips die nog renderen krijgen (nog) geen moment
    ap = autopilot.autopilot_for(db, user.id)
    us = user_settings(db, user.id)
    db.commit()
    channels = list(db.scalars(select(TwitchChannel).where(TwitchChannel.owner_id == user.id)
                               .order_by(TwitchChannel.display_name)).all())
    return render(request, "review.html", {
        "nav": "review", "items": [{"clip": c, "slot": slot_for.get(c.id)} for c in clips],
        "total": total, "why": why, "ap": ap, "us": us, "channels": channels,
        "periods": twitch.PERIOD_LABELS, "every_choices": autopilot.EVERY_CHOICES,
        "framing_modes": FRAMING_MODES, "yt_account": account_for(db, user.id), "privacy": yt.PRIVACY,
        "has_twitch": twitch.credential(db) is not None,
    })


def _apply_text(clip: Clip, form) -> None:
    if form.get("title") is not None and str(form.get("title")).strip():
        clip.title = str(form.get("title")).strip()[:300]
    if form.get("description") is not None:
        clip.description = str(form.get("description")).strip()[:1000]
    if form.get("hashtags") is not None:
        clip.hashtags = normalize_hashtags(str(form.get("hashtags")))
    if form.get("yt_tags") is not None:
        clip.yt_tags = normalize_tags(str(form.get("yt_tags"))) or None


def _schedule(db: Session, user: User, clip: Clip, when_str: str, privacy: str = "private", now: bool = False) -> str:
    from datetime import datetime, timedelta

    from app.api.youtube import _check_uploadable, _parse_local, _queue_upload

    if clip.status != "ready":
        raise HTTPException(400, "Deze clip wordt nog (opnieuw) gerenderd; wacht tot hij klaar is.")
    _check_uploadable(db, user, clip)
    when = None if now else _parse_local(when_str)
    if not now and when is None:
        raise HTTPException(400, "Kies een publicatietijd.")
    if when is not None and when < datetime.now(when.tzinfo) + timedelta(minutes=15):
        raise HTTPException(400, "Kies een publicatietijd minstens 15 minuten in de toekomst.")
    if privacy not in yt.PRIVACY:
        privacy = "private"
    _queue_upload(db, clip, {
        "title": clip.title[: yt.TITLE_MAX],
        "description": clip.post_text[: yt.DESC_MAX],
        "privacy": "private" if when else privacy,
        "publish_at": yt.to_rfc3339(when) if when else None,
        "made_for_kids": False,
    })
    return f"ingepland voor {when:%d-%m %H:%M}" if when else "wordt nu geüpload"


@router.post("/review/{cid}")
async def review_action(request: Request, cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    form = await request.form()
    _apply_text(clip, form)
    action = str(form.get("action", "save"))
    if action == "skip":
        clip.review_state = "skipped"
        db.commit()
        return redirect("/review", f"'{clip.title[:50]}' overgeslagen. Je vindt hem nog gewoon in het project.")
    db.commit()
    if action == "save":
        return redirect(f"/review#r-{clip.id}", "Opgeslagen.")
    try:
        msg = _schedule(db, user, clip, str(form.get("publish_at", "")), str(form.get("privacy", "private")),
                        now=action == "now")
    except HTTPException as exc:
        return redirect(f"/review#r-{clip.id}", str(exc.detail), "error")
    return redirect("/review", f"'{clip.title[:50]}' {msg}.")


@router.post("/review-bulk")
async def review_bulk(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Geselecteerde clips na elkaar inplannen op de aanbevolen momenten."""
    form = await request.form()
    ids = []
    for v in form.getlist("clip_ids"):
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    clips = [c for c in db.scalars(review_query(user.id).where(Clip.id.in_(ids or [0]), Clip.status == "ready")
                                   .order_by(Project.created_at.desc(), Clip.index)).all()]
    if not clips:
        return redirect("/review", "Selecteer eerst een of meer clips.", "error")
    slots, _ = timing.next_slots(db, user.id, len(clips))
    done, skipped = 0, []
    for clip, when in zip(clips, slots):
        try:
            _schedule(db, user, clip, when.strftime("%Y-%m-%dT%H:%M"))
            done += 1
        except HTTPException as exc:
            skipped.append(f"{clip.title[:30]}: {exc.detail}")
    if len(slots) < len(clips):
        skipped.append(f"{len(clips) - len(slots)} clip(s): geen vrij moment gevonden")
    msg = f"{done} clip(s) ingepland op de aanbevolen momenten."
    if skipped:
        msg += " Niet gelukt: " + "; ".join(skipped)
    return redirect("/review", msg, "ok" if done else "error")


@router.post("/review-settings")
async def review_settings(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    us = user_settings(db, user.id)
    try:
        us.uploads_per_day = max(1, min(10, int(str(form.get("uploads_per_day", 3)))))
        us.min_gap_hours = max(1, min(24, int(str(form.get("min_gap_hours", 3)))))
    except ValueError:
        return redirect("/review#momenten", "Ongeldig getal.", "error")
    db.commit()
    return redirect("/review#momenten", "Uploadmomenten opgeslagen.")


@router.post("/autopilot")
async def save_autopilot(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from app.core.models import utcnow

    form = await request.form()
    ap = autopilot.autopilot_for(db, user.id)
    own = {c.id for c in db.scalars(select(TwitchChannel).where(TwitchChannel.owner_id == user.id))}
    ids = []
    for v in form.getlist("channel_ids"):
        try:
            if int(v) in own:
                ids.append(int(v))
        except (TypeError, ValueError):
            continue
    enabled = form.get("enabled") in ("1", "on")
    if (enabled or form.get("run_now")) and not ids:
        return redirect("/review#autopilot", "Kies minstens één kanaal.", "error")

    def _int(name, lo, hi, default):
        try:
            return max(lo, min(hi, int(str(form.get(name, default)))))
        except (TypeError, ValueError):
            return default

    was = ap.enabled
    ap.enabled = enabled
    ap.channel_ids_json = json.dumps(ids)
    p = str(form.get("period", "dag"))
    ap.period = p if p in twitch.PERIODS else "dag"
    ap.min_views = _int("min_views", 0, 10_000_000, 0)
    ap.max_per_run = _int("max_per_run", 1, 20, 5)
    e = _int("every_hours", 1, 168, 12)
    ap.every_hours = e if e in autopilot.EVERY_CHOICES else 12
    f = str(form.get("framing", "split"))
    ap.framing = f if f in FRAMING_MODES else "split"
    if enabled and not was:
        ap.last_run_at = None  # meteen een eerste ronde bij de volgende controle
    db.commit()
    if form.get("run_now"):
        made = autopilot.run(db, ap, utcnow())
        return redirect("/review#autopilot", ap.last_result or "", "ok" if made else "error")
    return redirect("/review#autopilot", "Autopilot staat aan." if enabled else "Autopilot staat uit.")


# ---------------------------------------------------------------- Discord (Mijn account)
@router.post("/account/discord")
async def save_discord(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    url = str(form.get("webhook", "")).strip()
    us = user_settings(db, user.id)
    if form.get("remove"):
        us.discord_webhook_enc = None
        db.commit()
        return redirect("/account#discord", "Discord-meldingen uitgezet.")
    if not discord.valid_webhook(url):
        return redirect("/account#discord", "Dat is geen geldige Discord-webhook-URL (https://discord.com/api/webhooks/…).", "error")
    us.discord_webhook_enc = crypto.encrypt(url)
    discord.mark_all_notified(db, user.id)  # bestaande clips niet alsnog melden
    db.commit()
    ok = discord.send_to(url, "Shortcutter is gekoppeld", "Je krijgt hier meldingen als er iets klaarstaat.", "ok")
    return redirect("/account#discord", "Discord gekoppeld en een testbericht gestuurd." if ok
                    else "Opgeslagen, maar het testbericht kwam niet aan. Controleer de webhook.", "ok" if ok else "error")
