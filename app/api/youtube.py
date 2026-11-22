"""YouTube-koppeling per gebruiker en uploaden/inplannen van clips."""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import redirect
from app.core.auth import require_admin, require_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.models import Clip, Credential, User, YoutubeAccount, utcnow
from app.core.paths import project_output, safe_child
from app.core.store import audit, get_clip_for, get_project_for
from app.core import crypto
from app.integrations import youtube_upload as yt
from app.worker import queue

router = APIRouter()
STATE_COOKIE = "sc_yt_state"


def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.environ.get("TZ") or "Europe/Amsterdam")
    except Exception:  # noqa: BLE001
        return ZoneInfo("Europe/Amsterdam")


def account_for(db: Session, user_id: int) -> YoutubeAccount | None:
    return db.scalar(select(YoutubeAccount).where(YoutubeAccount.user_id == user_id))


# ---------------------------------------------------------------- beheer (admin)
@router.post("/keys/google-oauth")
def set_google_oauth(
    client_id: str = Form(...),
    client_secret: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    cid, secret = client_id.strip(), client_secret.strip()
    if not cid.endswith(".apps.googleusercontent.com") or not secret or len(secret) > 200:
        return redirect("/keys#google", "Dat lijkt geen geldige Google OAuth-client (client-ID eindigt op .apps.googleusercontent.com).", "error")
    cred = yt.oauth_credential(db)
    action = "sleutel_vervangen" if cred else "sleutel_toegevoegd"
    if cred is None:
        cred = Credential(provider="google_oauth", name="Google OAuth (YouTube-upload)", created_by=user.id,
                          encrypted_value=b"", mask="")
        db.add(cred)
    cred.encrypted_value = crypto.encrypt(json.dumps({"client_id": cid, "client_secret": secret}))
    cred.mask = f"ID {cid[:6]}…{cid.split('.')[0][-4:]} · secret {crypto.mask_secret(secret)}"
    cred.updated_at = utcnow()
    cred.is_active = True
    db.flush()
    audit(db, user, action, f"google_oauth #{cred.id}")
    db.commit()
    return redirect("/keys#google", "Google-koppeling opgeslagen. Gebruikers kunnen nu hun kanaal koppelen bij Mijn account.")


# ---------------------------------------------------------------- koppelen (iedere gebruiker)
@router.get("/youtube/connect")
def connect_start(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    client_cfg = yt.oauth_client(db)
    if not client_cfg:
        return redirect("/account", "De beheerder heeft nog geen Google-koppeling ingesteld (API-sleutels).", "error")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(yt.auth_url(client_cfg[0], yt.redirect_uri(str(request.base_url)), state), status_code=303)
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
                    secure=get_settings().cookie_secure, path="/youtube")
    return resp


@router.get("/youtube/callback")
def connect_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    expected = request.cookies.get(STATE_COOKIE)
    if error:
        resp = redirect("/account", "Koppelen geannuleerd of geweigerd bij Google.", "error")
    elif not expected or not state or not secrets.compare_digest(expected, state) or not code:
        resp = redirect("/account", "Ongeldige of verlopen koppelpoging. Probeer het opnieuw.", "error")
    else:
        try:
            tokens = yt.exchange_code(db, code, yt.redirect_uri(str(request.base_url)))
            acc = yt.connect(db, user.id, tokens)
            audit(db, user, "youtube_gekoppeld", acc.channel_title or acc.channel_id)
            db.commit()
            resp = redirect("/account", f"YouTube-kanaal '{acc.channel_title}' gekoppeld.")
        except yt.YoutubeError as exc:
            resp = redirect("/account", str(exc), "error")
    resp.delete_cookie(STATE_COOKIE, path="/youtube")
    return resp


@router.post("/youtube/disconnect")
def connect_remove(user: User = Depends(require_user), db: Session = Depends(get_db)):
    yt.disconnect(db, user.id)
    audit(db, user, "youtube_ontkoppeld", user.username)
    db.commit()
    return redirect("/account", "YouTube-kanaal ontkoppeld.")


# ---------------------------------------------------------------- uploaden
def _parse_local(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "Ongeldige datum/tijd.") from exc
    return dt.replace(tzinfo=local_tz()) if dt.tzinfo is None else dt


def _queue_upload(db: Session, clip: Clip, opts: dict) -> None:
    clip.yt_options_json = json.dumps(opts, ensure_ascii=False)
    clip.yt_status = "queued"
    clip.yt_error = None
    db.commit()
    queue.enqueue(db, clip.project, "upload", clip.id)


def _check_uploadable(db: Session, user: User, clip: Clip) -> None:
    if clip.owner_id != user.id:  # nooit op andermans YouTube-kanaal publiceren, ook niet als beheerder
        raise HTTPException(403, "Alleen de eigenaar van deze clip kan hem naar YouTube uploaden.")
    if account_for(db, clip.owner_id) is None:
        raise HTTPException(400, "Koppel eerst een YouTube-kanaal bij Mijn account.")
    base = project_output(clip.project_id)
    if not (safe_child(base, clip.file_path) or safe_child(base, clip.raw_path)):
        raise HTTPException(400, "Deze clip is nog niet gerenderd.")
    if clip.yt_status in ("queued", "uploading"):
        raise HTTPException(400, "Deze clip staat al in de wachtrij om te uploaden.")
    if clip.status in ("pending", "rendering"):
        raise HTTPException(400, "Deze clip wordt nog (opnieuw) gerenderd; wacht tot hij klaar is.")


@router.post("/clips/{cid}/youtube")
def upload_clip(
    cid: int,
    title: str = Form(...),
    description: str = Form(""),
    privacy: str = Form("private"),
    publish_at: str = Form(""),
    made_for_kids: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    clip = get_clip_for(db, user, cid)
    back = f"/projects/{clip.project_id}#clip-{clip.id}"
    try:  # een melding op de pagina in plaats van een kale foutpagina
        _check_uploadable(db, user, clip)
        if privacy not in yt.PRIVACY:
            raise HTTPException(400, "Onbekende zichtbaarheid.")
        when = _parse_local(publish_at)
        if when is not None and when < datetime.now(when.tzinfo) + timedelta(minutes=15):
            raise HTTPException(400, "Kies een publicatietijd minstens 15 minuten in de toekomst.")
    except HTTPException as exc:
        return redirect(back, str(exc.detail), "error")
    opts = {
        "title": title.strip()[: yt.TITLE_MAX],
        "description": description.strip()[: yt.DESC_MAX],
        "privacy": privacy,
        "publish_at": yt.to_rfc3339(when) if when else None,
        "made_for_kids": made_for_kids in ("1", "on", "true"),
    }
    if not opts["title"]:
        return redirect(back, "De titel mag niet leeg zijn.", "error")
    _queue_upload(db, clip, opts)
    return redirect(f"/projects/{clip.project_id}#clip-{clip.id}", "Clip staat in de wachtrij om naar YouTube te uploaden.")


@router.post("/projects/{pid}/youtube-schedule")
async def schedule_project(request: Request, pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Plan de geselecteerde clips na elkaar in (bijv. elke dag om 18:00)."""
    project = get_project_for(db, user, pid)
    form = await request.form()
    ids = []
    for v in form.getlist("clip_ids"):
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    clips = [c for c in project.clips if c.id in set(ids)]
    if not clips:
        return redirect(f"/projects/{pid}#youtube", "Selecteer eerst een of meer clips.", "error")
    start = _parse_local(str(form.get("start", "")))
    if start is None or start < datetime.now(start.tzinfo) + timedelta(minutes=15):
        return redirect(f"/projects/{pid}#youtube", "Kies een eerste publicatietijd minstens 15 minuten in de toekomst.", "error")
    try:
        hours = max(1, min(24 * 14, int(str(form.get("interval_hours", 24)))))
    except ValueError:
        hours = 24
    made_for_kids = form.get("made_for_kids") in ("1", "on", "true")
    done, skipped = 0, []
    for i, clip in enumerate(sorted(clips, key=lambda c: c.index)):
        try:
            _check_uploadable(db, user, clip)
        except HTTPException as exc:
            skipped.append(f"clip {clip.index}: {exc.detail}")
            continue
        when = start + timedelta(hours=hours * i)
        _queue_upload(db, clip, {
            "title": clip.title[: yt.TITLE_MAX],
            "description": clip.post_text[: yt.DESC_MAX],
            "privacy": "private",
            "publish_at": yt.to_rfc3339(when),
            "made_for_kids": made_for_kids,
        })
        done += 1
    msg = f"{done} clip(s) ingepland voor YouTube."
    if skipped:
        msg += " Overgeslagen: " + "; ".join(skipped)
    return redirect(f"/projects/{pid}#youtube", msg, "ok" if done else "error")


def run_upload(db: Session, clip_id: int) -> bool:
    """Worker: voer één upload uit. Geeft False bij een fout (staat dan in clip.yt_error)."""
    from app.estimation.service import clip_estimate

    clip = db.get(Clip, clip_id)
    if clip is None or clip.yt_status == "done":
        return True  # al geüpload (bv. dubbele job na een herstart): niet nog een keer
    acc = account_for(db, clip.owner_id)
    base = project_output(clip.project_id)
    path = safe_child(base, clip.file_path) or safe_child(base, clip.raw_path)
    opts = json.loads(clip.yt_options_json or "{}")
    if acc is None or path is None or not opts:
        clip.yt_status = "failed"
        clip.yt_error = "Geen gekoppeld kanaal of geen gerenderde video."
        db.commit()
        return False
    clip.yt_status = "uploading"
    db.commit()
    note = None
    if opts.get("publish_at"):
        try:
            when = datetime.strptime(opts["publish_at"], "%Y-%m-%dT%H:%M:%SZ")
            if when <= datetime.utcnow() + timedelta(minutes=2):
                opts["publish_at"] = None
                opts["privacy"] = "private"
                note = "De geplande publicatietijd was al voorbij; de video is privé geüpload. Zet hem in YouTube Studio op openbaar."
        except ValueError:
            opts["publish_at"] = None
        clip.yt_options_json = json.dumps(opts, ensure_ascii=False)  # zodat statistieken de echte tijd zien
        db.commit()
    from app.estimation.trends import upload_tags

    tags = upload_tags(clip.yt_tags, clip.hashtags)
    try:
        res = yt.upload(
            db, acc, path,
            title=opts.get("title") or clip.title,
            description=opts.get("description") or "",
            privacy=opts.get("privacy", "private"),
            publish_at=opts.get("publish_at"),
            made_for_kids=bool(opts.get("made_for_kids")),
            category_id="20" if clip.project.source_type == "twitch" else "22",
            tags=tags,
        )
    except yt.YoutubeError as exc:
        db.rollback()
        clip.yt_status = "failed"
        clip.yt_error = str(exc)
        db.commit()
        return False
    except Exception as exc:  # onverwachte fout: niet eeuwig op 'uploading' blijven staan
        import logging

        logging.getLogger(__name__).warning("Upload van clip %s mislukt: %s", clip_id, type(exc).__name__)
        db.rollback()
        clip.yt_status = "failed"
        clip.yt_error = f"Upload mislukt door een onverwachte fout ({type(exc).__name__}). Probeer het opnieuw."
        db.commit()
        return False
    clip.yt_status = "done"
    clip.yt_video_id = res["id"]
    db.commit()  # direct vastleggen: na een herstart niet nog een keer uploaden
    clip.yt_error = note
    if res.get("privacy") == "private" and opts.get("privacy") != "private" and not opts.get("publish_at"):
        clip.yt_error = ("YouTube heeft de video privé gezet. Dat gebeurt bij API-projecten die (nog) niet door Google "
                         "zijn geaudit; zet hem in YouTube Studio op openbaar.")
    clip.posted_platform = "YouTube Shorts"
    clip.posted_url = res["url"]
    if clip.posted_at is None:
        clip.posted_at = utcnow()
        est = clip_estimate(db, clip)
        clip.est_mid_at_post, clip.est_low_at_post, clip.est_high_at_post = est["mid"], est["low"], est["high"]
    db.commit()
    return True
