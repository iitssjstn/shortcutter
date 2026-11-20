"""Compilaties: de beste Twitch-clips achter elkaar als liggende 16:9-video voor YouTube."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
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
from app.pipeline import weekly
from app.pipeline.download import twitch_clip_slug

router = APIRouter()
BUSY = ("queued", "rendering")


def _comp(db: Session, user: User, cid: int) -> Compilation:
    comp = db.get(Compilation, cid)
    if comp is None or comp.owner_id != user.id:  # persoonlijk, net als Twitch-kanalen
        raise HTTPException(404, "Compilatie niet gevonden.")
    return comp


def _channels(db: Session, user: User) -> list[TwitchChannel]:
    return list(db.scalars(select(TwitchChannel).where(TwitchChannel.owner_id == user.id)
                           .order_by(TwitchChannel.display_name)).all())


@router.get("/compilations")
def list_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    comps = list(db.scalars(select(Compilation).where(Compilation.owner_id == user.id)
                            .order_by(Compilation.created_at.desc())).all())
    sched = weekly.schedule_for(db, user.id)
    db.commit()
    return render(request, "compilations.html", {
        "nav": "compilations", "comps": comps, "channels": _channels(db, user),
        "sched": sched, "weekdays": weekly.WEEKDAYS, "next_run": weekly.next_slot(sched) if sched.enabled else None,
        "has_key": twitch.credential(db) is not None, "periods": twitch.PERIOD_LABELS,
        "max_items": cp.MAX_ITEMS, "rankings": cp.RANKINGS, "formats": cp.FORMATS,
        "max_short": cp.MAX_SHORT_ITEMS,
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
    try:
        comp = weekly.build_draft(db, user.id, picked, period, count, ranking, str(form.get("format", "landscape")))
    except weekly.DraftError as exc:
        return redirect("/compilations", str(exc), "error")
    top = comp.items
    audit(db, user, "compilatie_aangemaakt", f"#{comp.id} ({len(top)} clips)")
    db.commit()
    return redirect(f"/compilations/{comp.id}", f"{len(top)} clips klaargezet. Pas volgorde en selectie aan en klik op Renderen.")


@router.post("/compilations/schedule")
async def save_schedule(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    s = weekly.schedule_for(db, user.id)
    own = {c.id for c in _channels(db, user)}
    ids = []
    for v in form.getlist("channel_ids"):
        try:
            if int(v) in own:
                ids.append(int(v))
        except (TypeError, ValueError):
            continue
    enabled = form.get("enabled") in ("1", "on")
    if enabled and not ids:
        return redirect("/compilations#weekly", "Kies minstens één kanaal voor de wekelijkse compilatie.", "error")
    def _int(name, lo, hi, default):
        try:
            return max(lo, min(hi, int(form.get(name, default))))
        except (TypeError, ValueError):
            return default
    was_enabled = s.enabled
    s.enabled = enabled
    s.channel_ids_json = json.dumps(ids)
    s.count = _int("count", 2, cp.MAX_ITEMS, 10)
    r = str(form.get("ranking", "countdown"))
    s.ranking = r if r in cp.RANKINGS else "countdown"
    s.weekday = _int("weekday", 0, 6, 0)
    s.hour = _int("hour", 0, 23, 9)
    s.intro = form.get("intro") in ("1", "on")
    s.outro = form.get("outro") in ("1", "on")
    s.auto_render = form.get("auto_render") in ("1", "on")
    f = str(form.get("format", "landscape"))
    s.format = f if f in cp.FORMATS else "landscape"
    if enabled and not was_enabled:
        s.last_run_at = utcnow()  # niet meteen alsnog een gemist moment inhalen; eerstvolgende moment telt
    db.commit()
    if form.get("run_now"):
        if not ids:
            return redirect("/compilations#weekly", "Kies minstens één kanaal.", "error")
        comp = weekly.run_schedule(db, s)
        if comp is None:
            return redirect("/compilations#weekly", s.last_result or "Geen compilatie gemaakt.", "error")
        return redirect(f"/compilations/{comp.id}", s.last_result)
    if enabled:
        nxt = weekly.next_slot(s)
        return redirect("/compilations#weekly", f"Wekelijkse compilatie staat aan. Volgende: {weekly.WEEKDAYS[nxt.weekday()]} {nxt:%d-%m} om {nxt:%H:%M}.")
    return redirect("/compilations#weekly", "Wekelijkse compilatie staat uit.")


@router.get("/compilations/{cid}")
def detail(request: Request, cid: int, open: int = 0, user: User = Depends(require_user), db: Session = Depends(get_db)):
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
        "busy": comp.status in BUSY, "formats": cp.FORMATS, "short_max": cp.SHORT_MAX_SECONDS, "fmt_ts": cp.fmt_ts, "has_video": bool(safe_child(cp.comp_dir(cid), comp.file_path)),
        "yt_account": account_for(db, user.id), "privacy": yt.PRIVACY, "max_items": cp.MAX_ITEMS,
        "open_item": open,
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
        if comp.format == "short":
            _read_layout(it, form)
    msg = "Opgeslagen."
    anchor_id = None
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
    if form.get("apply_layout"):
        try:
            src_id = int(str(form.get("apply_layout")))
        except ValueError:
            src_id = 0
        model = next((x for x in comp.items if x.id == src_id), None)
        if model is not None:
            for x in comp.items:
                x.layout, x.crop_x, x.cam_box, x.cam_pos = model.layout, model.crop_x, model.cam_box, model.cam_pos
            msg = "Indeling op alle clips toegepast."
            anchor_id = model.id
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
    anchor = f"#item-{m.group(1)}" if m else (f"?open={anchor_id}#item-{anchor_id}" if anchor_id else "")
    if form.get("render"):
        return _start_render(db, comp)
    return redirect(f"/compilations/{cid}{anchor}", msg) if msg else redirect(f"/compilations/{cid}{anchor}")


def _read_layout(it: CompilationItem, form) -> None:
    """Indeling van één clip in een short uit het formulier (alleen als de velden er zijn)."""
    from app.pipeline.facecam import format_box, parse_box

    lay = form.get(f"layout_{it.id}")
    if lay is None:
        return
    it.layout = "split" if lay == "split" else "fill"
    pos = form.get(f"campos_{it.id}")
    it.cam_pos = "top" if pos == "top" else "bottom"
    try:
        it.crop_x = max(0.0, min(1.0, float(form.get(f"cropx_{it.id}", 50)) / 100))
    except (TypeError, ValueError):
        pass
    box = parse_box(str(form.get(f"cam_{it.id}", "")).strip())
    it.cam_box = format_box(box) if box else None


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
    comp.items.append(weekly.item_from_clip(clip, ch, game, len(comp.items)))
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
    if comp.format == "short":
        if len(comp.included) > cp.MAX_SHORT_ITEMS:
            return redirect(f"/compilations/{comp.id}", f"Een short-top-lijst kan maximaal {cp.MAX_SHORT_ITEMS} clips hebben.", "error")
        total = sum(i.length for i in comp.included)
        if total > cp.SHORT_MAX_SECONDS + 0.5:
            return redirect(f"/compilations/{comp.id}", f"Samen {total:.0f} s: een short mag maximaal {cp.SHORT_MAX_SECONDS // 60} minuten duren. Kort clips in (Eind) of vink er een uit.", "error")
    comp.status = "queued"
    comp.attempts = 0
    comp.progress = 0
    comp.stage_message = "In wachtrij"
    comp.error = None
    comp.description = None  # opnieuw schrijven met de nieuwe hoofdstukken
    comp.yt_title = None
    comp.hashtags = None
    comp.yt_tags = None
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
              yt_tags: str = Form(""), user: User = Depends(require_user), db: Session = Depends(get_db)):
    from app.estimation.trends import normalize_tags

    comp = _comp(db, user, cid)
    comp.yt_tags = normalize_tags(yt_tags) or None
    comp.yt_title = title.strip()[: yt.TITLE_MAX] or comp.yt_title
    comp.description = description.strip()[: yt.DESC_MAX]
    comp.hashtags = cp._hashtags(hashtags, comp.format == "short") or None
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
    yt_tags: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from app.api.youtube import _parse_local, account_for
    from app.estimation.trends import normalize_tags

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
    comp.hashtags = cp._hashtags(hashtags, comp.format == "short") or comp.hashtags
    comp.yt_tags = normalize_tags(yt_tags) or comp.yt_tags
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


# ---------------------------------------------------------------- editor per clip (short)
def _item(db: Session, user: User, cid: int, iid: int) -> tuple[Compilation, CompilationItem]:
    comp = _comp(db, user, cid)
    it = next((i for i in comp.items if i.id == iid), None)
    if it is None:
        raise HTTPException(404, "Clip niet gevonden.")
    return comp, it


def _initial_layout(it: CompilationItem, src_w: int, src_h: int) -> tuple[str, dict | None]:
    """Startpunt voor de editor: eigen indeling, anders omgezet uit de eenvoudige instellingen."""
    from app.pipeline import framing
    from app.pipeline.facecam import parse_box

    if it.layout_json:
        try:
            lay = framing.validate_layout(json.loads(it.layout_json))
        except ValueError:
            lay = None
        if lay:
            return lay["type"], lay
    fw, fh = cp.SHORT_W - 2 * cp.SHORT_MARGIN, cp.SHORT_H - 2 * cp.SHORT_MARGIN
    gx = it.crop_x if it.crop_x is not None else 0.5
    if (it.layout or "fill") == "split":
        box = parse_box(it.cam_box)
        if not box:
            return "auto", None
        top = max(0.28, min(0.45, fw * (box[3] * src_h) / (box[2] * src_w) / fh))
        gw = min(1.0, (fw / (fh * (1 - top))) / (src_w / src_h))
        return "split", {"type": "split", "cam": list(box), "game": [round((1 - gw) * gx, 4), 0.0, round(gw, 4), 1.0],
                         "top": round(top, 3)}
    w = min(1.0, (fw / fh) / (src_w / src_h))
    return "crop", {"type": "crop", "rect": [round((1 - w) * gx, 4), 0.0, round(w, 4), 1.0]}


@router.get("/compilations/{cid}/items/{iid}/edit")
def item_editor(request: Request, cid: int, iid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from app.pipeline import media

    comp, it = _item(db, user, cid, iid)
    src = cp.item_source(cid, it)
    if src is None:
        cp.fetch_in_background(cid, iid)
        return render(request, "comp_item_edit.html", {"nav": "compilations", "comp": comp, "it": it, "loading": True})
    info = media.probe(src)
    mode, lay = _initial_layout(it, info["width"], info["height"])
    end = it.trim_end if it.trim_end is not None else (info["duration"] or it.duration)
    return render(request, "comp_item_edit.html", {
        "nav": "compilations", "comp": comp, "it": it, "loading": False,
        "src_w": info["width"], "src_h": info["height"], "start": it.trim_start or 0.0, "end": end,
        "dur": max(0.1, end - (it.trim_start or 0.0)),
        "layout_mode": mode, "layout_json": json.dumps(lay) if lay else "",
    })


@router.get("/compilations/{cid}/items/{iid}/ready")
def item_ready(cid: int, iid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    comp, it = _item(db, user, cid, iid)
    if cp.item_source(cid, it) is not None:
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    if not cp.is_fetching(cid, iid):
        return Response('<div class="flash flash-error">Ophalen mislukt. Probeer het later opnieuw.</div>',
                        media_type="text/html", headers={"HX-Reswap": "outerHTML"})
    return Response(status_code=204)


@router.get("/compilations/{cid}/items/{iid}/source")
def item_source_file(cid: int, iid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    comp, it = _item(db, user, cid, iid)
    src = cp.item_source(cid, it)
    if src is None:
        raise HTTPException(404, "Nog niet opgehaald.")
    return FileResponse(src, media_type="video/mp4")


@router.get("/compilations/{cid}/items/{iid}/overlay.png")
def item_overlay(cid: int, iid: int, top: float = 0.0, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Titel en lijstje van de short als doorzichtige laag, voor over het voorbeeld in de editor."""
    import tempfile

    comp, it = _item(db, user, cid, iid)
    items = comp.included or comp.items
    k = items.index(it) if it in items else 0
    area = None
    if top > 0:
        fh = cp.SHORT_H - 2 * cp.SHORT_MARGIN
        area = (cp.SHORT_MARGIN + int(fh * max(0.2, min(0.6, top))) + 30, int(cp.SHORT_H * 0.82))
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "o.png"
        cp.short_overlay(p, comp.title or "Top clips", cp.short_list(comp.ranking, items, k),
                         f"twitch.tv/{it.channel_login}" if it.channel_login else it.streamer, area)
        data = p.read_bytes()
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, no-cache"})


@router.post("/compilations/{cid}/items/{iid}/editor")
async def item_editor_save(request: Request, cid: int, iid: int, user: User = Depends(require_user),
                           db: Session = Depends(get_db)):
    from app.pipeline import framing

    comp, it = _item(db, user, cid, iid)
    form = await request.form()
    mode = form.get("layout_mode", "auto")
    if mode == "auto":
        lay_json, layout, cam_box = None, "split", None  # facecam automatisch zoeken, anders vol
    elif mode in ("split", "crop"):
        try:
            lay = framing.validate_layout(json.loads(form.get("layout_json") or "null"))
        except ValueError:
            lay = None
        if lay is None or lay["type"] != mode:
            raise HTTPException(400, "Zet eerst de vakken op het beeld.")
        lay_json, layout, cam_box = json.dumps(lay), it.layout, it.cam_box
    else:
        raise HTTPException(400, "Onbekende indeling.")
    targets = comp.items if form.get("apply_all") in ("1", "on") else [it]
    for x in targets:
        x.layout_json = lay_json
        if mode == "auto":
            x.layout, x.cam_box = layout, cam_box
    comp.updated_at = utcnow()
    db.commit()
    msg = "Indeling opgeslagen" + (" voor alle clips" if len(targets) > 1 else "") + ". Klik op Opnieuw renderen om het resultaat te zien."
    return redirect(f"/compilations/{cid}#item-{iid}", msg)
