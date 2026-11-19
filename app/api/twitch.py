"""Twitch: kanalen beheren (eigen of met toestemming) en top-clips omzetten in shorts."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.common import redirect, render
from app.core.auth import require_user
from app.core.db import get_db
from app.core.models import Project, TwitchChannel, User
from app.core.store import FRAMING_MODES, audit, get_global_int, user_settings
from app.integrations import twitch
from app.pipeline.download import twitch_clip_slug
from app.pipeline.run import ACTIVE_STATUSES
from app.worker import queue

router = APIRouter()
MAX_IMPORT = 20


def create_twitch_project(db: Session, owner_id: int, clip: dict, ch: TwitchChannel, framing: str,
                          caption_style: str) -> Project:
    """Project van één Twitch-clip (van een toegestaan kanaal) aanmaken en in de wachtrij zetten."""
    project = Project(
        owner_id=owner_id,
        source_type="twitch",
        source_url=clip["url"],
        title=clip["title"][:300],
        credit=f"twitch.tv/{ch.login}",
        num_clips=1,
        min_dur=3,
        max_dur=max(60, int(clip["duration"]) + 1),
        caption_style=caption_style,
        framing=framing,
        status="queued",
        source_meta_json=json.dumps({
            "platform": "twitch",
            "original_title": clip["title"],
            "title_unreliable": True,  # Twitch-cliptitels worden door kijkers bedacht
            "streamer": clip.get("broadcaster_name") or ch.display_name,
            "game": twitch.get_game_name(db, clip.get("game_id", "")),
            "clip_creator": clip.get("creator"),
        }, ensure_ascii=False),
    )
    db.add(project)
    db.commit()
    queue.enqueue(db, project)
    return project


def _channel(db: Session, user: User, cid: int) -> TwitchChannel:
    ch = db.get(TwitchChannel, cid)
    if ch is None or ch.owner_id != user.id:  # kanalen zijn persoonlijk, ook voor admins
        raise HTTPException(404, "Kanaal niet gevonden.")
    return ch


@router.get("/twitch")
def twitch_page(
    request: Request,
    channel: int | None = None,
    period: str = "week",
    view: str = "kanalen",
    q: str = "",
    game: str = "",
    add: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    channels = list(db.scalars(select(TwitchChannel).where(TwitchChannel.owner_id == user.id)
                               .order_by(TwitchChannel.display_name)).all())
    has_key = twitch.credential(db) is not None
    period = period if period in twitch.PERIODS else "week"
    view = "zoeken" if view == "zoeken" else "kanalen"
    selected = next((c for c in channels if c.id == channel), channels[0] if channels else None)
    clips, error, categories, game_name = [], None, [], ""
    if has_key:
        try:
            if view == "zoeken":
                if q.strip():
                    categories = twitch.search_categories(db, q)
                if game.isdigit():
                    clips = twitch.get_clips(db, "", period, first=50, game_id=game)
                    game_name = twitch.get_game_name(db, game) or ""
            elif selected is not None:
                clips = twitch.get_clips(db, selected.broadcaster_id, period)
        except twitch.TwitchError as exc:
            error = str(exc)
    imported = set(db.scalars(select(Project.source_url).where(Project.owner_id == user.id,
                                                              Project.source_type == "twitch")).all())
    return render(request, "twitch.html", {
        "nav": "twitch",
        "channels": channels,
        "selected": selected,
        "clips": clips,
        "error": error,
        "has_key": has_key,
        "period": period,
        "periods": twitch.PERIOD_LABELS,
        "imported": imported,
        "framing_default": "split",
        "view": view,
        "q": q.strip()[:100],
        "game": game if game.isdigit() else "",
        "game_name": game_name,
        "categories": categories,
        "permitted": {c.broadcaster_id: c for c in channels},
        "add_login": add.strip().lstrip("@")[:40],
    })


@router.post("/twitch/channels")
def add_channel(
    login: str = Form(...),
    relation: str = Form("toestemming"),
    note: str = Form(""),
    confirm: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    login = login.strip().lstrip("@").lower()
    if "twitch.tv/" in login:
        login = login.split("twitch.tv/", 1)[1].split("/")[0]
    if not twitch.LOGIN_RE.match(login):
        return redirect("/twitch", "Vul een geldige Twitch-kanaalnaam in.", "error")
    if confirm not in ("1", "on", "true"):
        return redirect("/twitch", "Bevestig dat het je eigen kanaal is of dat je toestemming van de streamer hebt.", "error")
    relation = relation if relation in ("eigen", "toestemming") else "toestemming"
    try:
        info = twitch.get_user(db, login)
    except twitch.TwitchError as exc:
        return redirect("/twitch", str(exc), "error")
    if info is None:
        return redirect("/twitch", f"Twitch-kanaal '{login}' niet gevonden.", "error")
    ch = TwitchChannel(
        owner_id=user.id,
        login=info.get("login", login),
        display_name=info.get("display_name") or login,
        broadcaster_id=str(info.get("id", "")),
        profile_image_url=(info.get("profile_image_url") or None),
        relation=relation,
        permission_note=note.strip()[:1000],
    )
    db.add(ch)
    try:
        audit(db, user, "twitch_kanaal_toegevoegd", f"{ch.login} ({relation})")
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect("/twitch", "Dit kanaal staat al in je lijst.", "error")
    return redirect(f"/twitch?channel={ch.id}", f"Kanaal {ch.display_name} toegevoegd.")


@router.post("/twitch/channels/{cid}/delete")
def delete_channel(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    ch = _channel(db, user, cid)
    audit(db, user, "twitch_kanaal_verwijderd", ch.login)
    db.delete(ch)
    db.commit()
    return redirect("/twitch", "Kanaal verwijderd. Bestaande projecten blijven staan.")


@router.post("/twitch/import")
async def import_clips(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    form = await request.form()
    fixed: TwitchChannel | None = None
    if form.get("channel_id"):
        try:
            fixed = _channel(db, user, int(form.get("channel_id", "0")))
        except ValueError as exc:
            raise HTTPException(400, "Ongeldig kanaal.") from exc
    # zonder vast kanaal (zoeken op game): alleen clips van kanalen in je eigen lijst
    allowed = {c.broadcaster_id: c for c in db.scalars(select(TwitchChannel).where(TwitchChannel.owner_id == user.id))}
    urls = [u for u in form.getlist("urls") if isinstance(u, str)][:MAX_IMPORT]
    framing = form.get("framing", "split")
    framing = framing if framing in FRAMING_MODES else "split"
    back_raw = str(form.get("back", ""))
    back = back_raw if back_raw.startswith("/twitch") else (
        f"/twitch?channel={fixed.id}&period={form.get('period', 'week')}" if fixed else "/twitch")
    if not urls:
        return redirect(back, "Selecteer eerst een of meer clips.", "error")
    limit = get_global_int(db, "max_active_per_user")
    active = db.scalar(select(func.count()).select_from(Project).where(
        Project.owner_id == user.id, Project.status.in_(ACTIVE_STATUSES))) or 0
    us = user_settings(db, user.id)
    made, skipped = [], []
    for url in dict.fromkeys(urls):
        slug = twitch_clip_slug(url)
        if not slug:
            skipped.append("ongeldige link")
            continue
        try:
            clip = twitch.get_clip(db, slug)
        except twitch.TwitchError as exc:
            return redirect(back, str(exc), "error")
        if clip is None:
            skipped.append(f"{slug}: niet gevonden")
            continue
        # alleen clips van een kanaal waarvoor je toestemming hebt (of je eigen kanaal)
        ch = fixed if fixed is not None else allowed.get(clip["broadcaster_id"])
        if ch is None or clip["broadcaster_id"] != ch.broadcaster_id:
            skipped.append(f"{clip.get('broadcaster_name') or slug}: geen toestemming (voeg het kanaal eerst toe)")
            continue
        if limit > 0 and active >= limit:
            skipped.append("maximum actieve projecten bereikt")
            break
        project = create_twitch_project(db, user.id, clip, ch, framing, us.caption_style)
        active += 1
        made.append(project.id)
    if not made:
        return redirect(back, "Geen clips toegevoegd" + (f" ({'; '.join(skipped)})" if skipped else "") + ".", "error")
    msg = f"{len(made)} clip(s) in de wachtrij gezet."
    if skipped:
        msg += f" Overgeslagen: {'; '.join(skipped)}."
    return redirect("/" if len(made) > 1 else f"/projects/{made[0]}", msg)
