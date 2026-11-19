"""Compilaties: de beste Twitch-clips achter elkaar als liggende 16:9-video voor YouTube."""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import redirect, render
from app.core.auth import require_user
from app.core.db import get_db
from app.core.models import Compilation, CompilationItem, TwitchChannel, User, utcnow
from app.core.paths import safe_child
from app.core.store import audit
from app.integrations import twitch
from app.integrations import youtube_upload as yt
from app.pipeline import compile as cp
from app.pipeline.download import twitch_clip_slug

router = APIRouter()
COUNTS = (5, 8, 10, 15, 20)
BUSY = ("queued", "rendering")


def _comp(db: Session, user: User, cid: int) -> Compilation:
    comp = db.get(Compilation, cid)
    if comp is None or comp.owner_id != user.id:  # persoonlijk, net als Twitch-kanalen
        raise HTTPException(404, "Compilatie niet gevonden.")
    return comp


def _channels(db: Session, user: User) -> list[TwitchChannel]:
    return list(db.scalars(select(TwitchChannel).where(TwitchChannel.owner_id == user.id)
                           .order_by(TwitchChannel.display_name)).all())


def _item_from_clip(clip: dict, ch: TwitchChannel, game: str | None, pos: int) -> CompilationItem:
    return CompilationItem(
        position=pos, clip_url=clip["url"], slug=clip["id"] or twitch_clip_slug(clip["url"]) or "",
        title=(clip.get("title") or "Twitch-clip")[:200], streamer=(clip.get("broadcaster_name") or ch.display_name)[:80],
        channel_login=ch.login, broadcaster_id=ch.broadcaster_id, game=(game or None),
        views=int(clip.get("views") or 0), duration=float(clip.get("duration") or 0),
        thumbnail_url=(clip.get("thumbnail") or None),
    )


@router.get("/compilations")
def list_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    comps = list(db.scalars(select(Compilation).where(Compilation.owner_id == user.id)
                            .order_by(Compilation.created_at.desc())).all())
    return render(request, "compilations.html", {
        "nav": "compilations", "comps": comps, "channels": _channels(db, user),
        "has_key": twitch.credential(db) is not None, "periods": twitch.PERIOD_LABELS,
        "counts": COUNTS, "rankings": cp.RANKINGS,
    })


@router.post("/compilations")
async def create(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    period = str(form.get("period", "week"))
    period = period if period in twitch.PERIODS else "week"
    try:
        count = max(2, min(cp.MAX_ITEMS, int(form.get("count", 10))))
    except ValueError:
        count = 10
    ranking = str(form.get("ranking", "countdown"))
    ranking = ranking if ranking in cp.RANKINGS else "countdown"
    chans = {c.id: c for c in _channels(db, user)}
    picked = []
    for v in form.getlist("channel_ids"):
        try:
            if int(v) in chans:
                picked.append(chans[int(v)])
        except (TypeError, ValueError):
            continue
    if not picked:
        return redirect("/compilations", "Kies minstens één kanaal uit je lijst.", "error")
    found: list[tuple[dict, TwitchChannel]] = []
    try:
        for ch in picked:
            for clip in twitch.get_clips(db, ch.broadcaster_id, period, first=max(20, count * 2)):
                if clip.get("broadcaster_id") == ch.broadcaster_id:  # alleen clips van dit (toegestane) kanaal
                    found.append((clip, ch))
    except twitch.TwitchError as exc:
        return redirect("/compilations", str(exc), "error")
    found.sort(key=lambda x: x[0]["views"], reverse=True)
    seen, top = set(), []
    for clip, ch in found:
        if clip["id"] not in seen and clip.get("duration", 0) >= 3:
            seen.add(clip["id"])
            top.append((clip, ch))
        if len(top) >= count:
            break
    if len(top) < 2:
        return redirect("/compilations", "Te weinig clips gevonden in deze periode. Kies een langere periode of meer kanalen.", "error")
    if ranking == "countdown":
        top.reverse()  # minst bekeken eerst, #1 als laatste
    lang = cp._lang(db, user.id)
    comp = Compilation(owner_id=user.id, period=period, ranking=ranking,
                       title=cp.default_title(period, [c.get("broadcaster_name", "") for c, _ in top], lang))
    games: dict[str, str] = {}
    for k, (clip, ch) in enumerate(top):
        gid = clip.get("game_id") or ""
        if gid and gid not in games:
            try:
                games[gid] = twitch.get_game_name(db, gid) or ""
            except twitch.TwitchError:
                games[gid] = ""
        comp.items.append(_item_from_clip(clip, ch, games.get(gid), k))
    db.add(comp)
    db.commit()
    audit(db, user, "compilatie_aangemaakt", f"#{comp.id} ({len(top)} clips)")
    db.commit()
    return redirect(f"/compilations/{comp.id}", f"{len(top)} clips klaargezet. Pas volgorde en selectie aan en klik op Renderen.")


@router.get("/compilations/{cid}")
def detail(request: Request, cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from app.api.youtube import account_for

    comp = _comp(db, user, cid)
    n = len(comp.included)
    ranks, k = {}, 0
    for it in comp.items:
        if it.included:
            ranks[it.id] = cp.rank_of(comp.ranking, k, n)
            k += 1
    total = sum(i.length for i in comp.included) + (cp.INTRO_SECONDS if comp.intro else 0) + (cp.OUTRO_SECONDS if comp.outro else 0)
    return render(request, "compilation.html", {
        "nav": "compilations", "comp": comp, "ranks": ranks, "total": total, "rankings": cp.RANKINGS,
        "busy": comp.status in BUSY, "fmt_ts": cp.fmt_ts, "has_video": bool(safe_child(cp.comp_dir(cid), comp.file_path)),
        "yt_account": account_for(db, user.id), "privacy": yt.PRIVACY, "max_items": cp.MAX_ITEMS,
    })


def _float(v, default=None):
    try:
        return round(float(str(v).replace(",", ".")), 2) if str(v).strip() != "" else default
    except ValueError:
        return default


@router.post("/compilations/{cid}")
async def save(request: Request, cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    comp = _comp(db, user, cid)
    if comp.status in BUSY:
        return redirect(f"/compilations/{cid}", "Wacht tot het renderen klaar is.", "error")
    form = await request.form()
    comp.title = str(form.get("title", comp.title)).strip()[:200] or comp.title
    r = str(form.get("ranking", comp.ranking))
    comp.ranking = r if r in cp.RANKINGS else comp.ranking
    comp.intro = form.get("intro") in ("1", "on")
    comp.outro = form.get("outro") in ("1", "on")
    for it in comp.items:
        it.included = form.get(f"inc_{it.id}") in ("1", "on")
        it.title = str(form.get(f"title_{it.id}", it.title)).strip()[:200] or it.title
        start = _float(form.get(f"start_{it.id}"), 0.0) or 0.0
        end = _float(form.get(f"end_{it.id}"), None)
        dur = it.duration or 0
        start = max(0.0, min(start, max(0.0, dur - 1)))
        if end is not None and (end >= dur - 0.05 or end <= start + 0.5):
            end = None
        it.trim_start, it.trim_end = start, end
    msg = "Opgeslagen."
    move = str(form.get("move", ""))
    m = re.match(r"^(\d+):(up|down|top|bottom)$", move)
    if m:
        items = list(comp.items)
        idx = next((i for i, x in enumerate(items) if x.id == int(m.group(1))), None)
        if idx is not None:
            it = items.pop(idx)
            new = {"up": idx - 1, "down": idx + 1, "top": 0, "bottom": len(items)}[m.group(2)]
            items.insert(max(0, min(len(items), new)), it)
            for k, x in enumerate(items):
                x.position = k
            msg = None
    if form.get("remove"):
        try:
            rid = int(str(form.get("remove")))
        except ValueError:
            rid = 0
        for it in list(comp.items):
            if it.id == rid:
                comp.items.remove(it)
        msg = "Clip verwijderd."
    comp.updated_at = utcnow()
    db.commit()
    anchor = f"#item-{m.group(1)}" if m else ""
    if form.get("render"):
        return _start_render(db, comp)
    return redirect(f"/compilations/{cid}{anchor}", msg) if msg else redirect(f"/compilations/{cid}{anchor}")


@router.post("/compilations/{cid}/add")
def add_clip(cid: int, url: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    comp = _comp(db, user, cid)
    slug = twitch_clip_slug(url.strip())
    if not slug:
        return redirect(f"/compilations/{cid}#add", "Plak een Twitch-cliplink (clips.twitch.tv/… of twitch.tv/naam/clip/…).", "error")
    if len(comp.items) >= cp.MAX_ITEMS:
        return redirect(f"/compilations/{cid}#add", f"Maximaal {cp.MAX_ITEMS} clips per compilatie.", "error")
    if any(i.slug == slug for i in comp.items):
        return redirect(f"/compilations/{cid}#add", "Deze clip staat er al in.", "error")
    try:
        clip = twitch.get_clip(db, slug)
    except twitch.TwitchError as exc:
        return redirect(f"/compilations/{cid}#add", str(exc), "error")
    if clip is None:
        return redirect(f"/compilations/{cid}#add", "Clip niet gevonden.", "error")
    ch = next((c for c in _channels(db, user) if c.broadcaster_id == clip["broadcaster_id"]), None)
    if ch is None:
        return redirect(f"/compilations/{cid}#add", f"Geen toestemming voor {clip.get('broadcaster_name') or 'dit kanaal'}: voeg het kanaal eerst toe bij Twitch.", "error")
    game = None
    if clip.get("game_id"):
        try:
            game = twitch.get_game_name(db, clip["game_id"])
        except twitch.TwitchError:
            game = None
    comp.items.append(_item_from_clip(clip, ch, game, len(comp.items)))
    comp.updated_at = utcnow()
    db.commit()
    return redirect(f"/compilations/{cid}", "Clip toegevoegd (onderaan).")


def _start_render(db: Session, comp: Compilation):
    if comp.status in BUSY:
        return redirect(f"/compilations/{comp.id}", "Deze compilatie wordt al gerenderd.")
    if len(comp.included) < 2:
        return redirect(f"/compilations/{comp.id}", "Vink minstens 2 clips aan.", "error")
    if len(comp.included) > cp.MAX_ITEMS:
        return redirect(f"/compilations/{comp.id}", f"Maximaal {cp.MAX_ITEMS} clips.", "error")
    comp.status = "queued"
    comp.attempts = 0
    comp.progress = 0
    comp.stage_message = "In wachtrij"
    comp.error = None
    comp.description = None  # opnieuw schrijven met de nieuwe hoofdstukken
    comp.yt_title = None
    comp.hashtags = None
    comp.updated_at = utcnow()
    db.commit()
    return redirect(f"/compilations/{comp.id}", "Compilatie staat in de wachtrij om te renderen.")


@router.post("/compilations/{cid}/render")
def render_now(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _start_render(db, _comp(db, user, cid))


@router.get("/compilations/{cid}/status")
def status(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    comp = _comp(db, user, cid)
    if comp.status in BUSY or comp.yt_status in ("queued", "uploading"):
        return Response(status_code=204)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.get("/compilations/{cid}/video")
def video(cid: int, download: int = 0, user: User = Depends(require_user), db: Session = Depends(get_db)):
    comp = _comp(db, user, cid)
    path = safe_child(cp.comp_dir(cid), comp.file_path)
    if path is None:
        raise HTTPException(404, "Nog geen video.")
    name = re.sub(r"[^\w\- ]+", "", comp.title or "compilatie").strip().replace(" ", "-")[:80] or "compilatie"
    headers = {"Content-Disposition": f'attachment; filename="{name}.mp4"'} if download else None
    return FileResponse(path, media_type="video/mp4", headers=headers)


@router.get("/compilations/{cid}/thumb")
def thumb(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    comp = _comp(db, user, cid)
    path = safe_child(cp.comp_dir(cid), comp.thumb_path)
    if path is None:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@router.post("/compilations/{cid}/text")
def save_text(cid: int, title: str = Form(""), description: str = Form(""), hashtags: str = Form(""),
              user: User = Depends(require_user), db: Session = Depends(get_db)):
    comp = _comp(db, user, cid)
    comp.yt_title = title.strip()[: yt.TITLE_MAX] or comp.yt_title
    comp.description = description.strip()[: yt.DESC_MAX]
    comp.hashtags = cp._hashtags(hashtags) or None
    db.commit()
    return redirect(f"/compilations/{cid}#youtube", "Titel en beschrijving opgeslagen.")


@router.post("/compilations/{cid}/youtube")
def upload(
    cid: int,
    title: str = Form(...),
    description: str = Form(""),
    privacy: str = Form("private"),
    publish_at: str = Form(""),
    made_for_kids: str = Form(""),
    hashtags: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from app.api.youtube import _parse_local, account_for

    comp = _comp(db, user, cid)
    if account_for(db, user.id) is None:
        raise HTTPException(400, "Koppel eerst een YouTube-kanaal bij Mijn account.")
    if safe_child(cp.comp_dir(cid), comp.file_path) is None or comp.status != "done":
        raise HTTPException(400, "Render de compilatie eerst.")
    if comp.yt_status in ("queued", "uploading"):
        raise HTTPException(400, "Deze compilatie wordt al geüpload.")
    if privacy not in yt.PRIVACY:
        raise HTTPException(400, "Onbekende zichtbaarheid.")
    when = _parse_local(publish_at)
    if when is not None and when < datetime.now(when.tzinfo) + timedelta(minutes=15):
        raise HTTPException(400, "Kies een publicatietijd minstens 15 minuten in de toekomst.")
    t = title.strip()[: yt.TITLE_MAX]
    if not t:
        raise HTTPException(400, "De titel mag niet leeg zijn.")
    comp.yt_title = t
    comp.description = description.strip()[: yt.DESC_MAX]
    comp.hashtags = cp._hashtags(hashtags) or comp.hashtags
    comp.yt_options_json = json.dumps({
        "title": t, "description": comp.description, "privacy": "private" if when else privacy,
        "publish_at": yt.to_rfc3339(when) if when else None, "made_for_kids": made_for_kids in ("1", "on", "true"),
    }, ensure_ascii=False)
    comp.yt_status = "queued"
    comp.yt_error = None
    comp.updated_at = utcnow()
    db.commit()
    return redirect(f"/compilations/{cid}#youtube", "Compilatie staat in de wachtrij om naar YouTube te uploaden.")


@router.post("/compilations/{cid}/delete")
def delete(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    comp = _comp(db, user, cid)
    if comp.status == "rendering":
        return redirect(f"/compilations/{cid}", "Wacht tot het renderen klaar is.", "error")
    shutil.rmtree(cp.comp_dir(cid), ignore_errors=True)
    db.delete(comp)
    db.commit()
    return redirect("/compilations", "Compilatie verwijderd. Een eventuele YouTube-video blijft staan.")
