"""Clip-acties: bekijken, trimmen, ondertitels, hertitelen, posten en views invullen."""
from __future__ import annotations

import json
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.common import redirect
from app.core.auth import require_user
from app.core.db import get_db
from app.core.models import Clip, User, utcnow
from app.core.paths import find_source, project_output, safe_child
from app.core.store import get_clip_for, get_project_for
from app.estimation.estimator import viral_score
from app.estimation.features import compute_features
from app.estimation.service import clip_estimate
from app.pipeline.run import ACTIVE_STATUSES, load_transcript, write_clips_json
from app.pipeline.transcribe import all_words
from app.worker import queue

router = APIRouter()

PLATFORMS = ["YouTube Shorts", "TikTok", "Instagram Reels", "Facebook", "X", "Anders"]


def _back(clip: Clip, anchor: bool = True) -> str:
    return f"/projects/{clip.project_id}" + (f"#clip-{clip.id}" if anchor else "")


def _clip_file(clip: Clip, which: str = "final"):
    base = project_output(clip.project_id)
    if which == "raw":
        return safe_child(base, clip.raw_path)
    return safe_child(base, clip.file_path) or safe_child(base, clip.raw_path)


@router.get("/clips/{cid}/video")
def clip_video(cid: int, raw: bool = False, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    path = _clip_file(clip, "raw" if raw else "final")
    if path is None:
        raise HTTPException(404, "Deze clip is (nog) niet gerenderd.")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-cache"})


@router.get("/clips/{cid}/thumb")
def clip_thumb(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    path = safe_child(project_output(clip.project_id), clip.thumb_path)
    if path is None:
        raise HTTPException(404, "Geen thumbnail.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})


def _rescore(clip: Clip) -> None:
    t = load_transcript(clip.project_id) or {"segments": []}
    feats = compute_features(clip.start, clip.end, clip.subscores, all_words(t),
                             1.0 if clip.framing_mode == "crop" else None)
    clip.features_json = json.dumps(feats)
    clip.viral_score = viral_score(feats)


def _queue_render(db: Session, clip: Clip) -> str:
    if find_source(clip.project_id) is None:
        clip.status = "failed"
        clip.render_error = "Het bronbestand is verwijderd. Upload de bron opnieuw om te renderen."
        db.commit()
        return "Bronbestand ontbreekt; upload het opnieuw om te renderen."
    clip.status = "pending"
    db.commit()
    queue.enqueue(db, clip.project, "render_clip", clip.id)
    return "Clip staat in de wachtrij om opnieuw te renderen."


def _parse_time(val: str) -> float:
    val = (val or "").strip().replace(",", ".")
    try:
        if ":" in val:
            parts = [float(p) for p in val.split(":")]
            total = 0.0
            for p in parts:
                total = total * 60 + p
            return total
        return float(val)
    except ValueError as exc:
        raise HTTPException(400, f"Ongeldige tijd: {val!r}. Gebruik seconden of m:ss.") from exc


def _validate_range(project, start: float, end: float) -> None:
    if start < 0 or end <= start:
        raise HTTPException(400, "De eindtijd moet na de starttijd liggen.")
    if end - start < 1:
        raise HTTPException(400, "Een clip moet minstens 1 seconde duren.")
    if end - start > 180:
        raise HTTPException(400, "Een clip mag maximaal 180 seconden duren.")
    if project.duration and end > project.duration + 0.5:
        raise HTTPException(400, "De eindtijd ligt na het einde van de video.")


@router.post("/clips/{cid}/trim")
def trim(cid: int, start: str = Form(...), end: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    s, e = _parse_time(start), _parse_time(end)
    _validate_range(clip.project, s, e)
    clip.start, clip.end = round(s, 2), round(e, 2)
    _rescore(clip)
    msg = _queue_render(db, clip)
    return redirect(_back(clip), msg)


@router.post("/clips/{cid}/rerender")
def rerender(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    return redirect(_back(clip), _queue_render(db, clip))


@router.post("/clips/{cid}/captions")
def toggle_captions(cid: int, enabled: str = Form("0"), user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    clip.captions_enabled = enabled in ("1", "true", "on")
    base = project_output(clip.project_id)
    if not clip.captions_enabled:
        if safe_child(base, clip.raw_path):
            clip.file_path = clip.raw_path
            clip.status = "ready"
            clip.render_error = None
        db.commit()
        return redirect(_back(clip), "Ondertitels uitgezet.")
    final = f"clip_{clip.index:02d}.mp4"
    if safe_child(base, final):
        clip.file_path = final
        clip.status = "ready"
        db.commit()
        return redirect(_back(clip), "Ondertitels aangezet.")
    return redirect(_back(clip), _queue_render(db, clip))


@router.post("/clips/{cid}/retitle")
def retitle(cid: int, title: str = Form(...), hook: str = Form(None), user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    title = title.strip()
    if not title:
        raise HTTPException(400, "De titel mag niet leeg zijn.")
    clip.title = title[:300]
    if hook is not None:
        clip.hook = hook.strip()[:1000]
    db.commit()
    write_clips_json(db, clip.project)
    return redirect(_back(clip), "Titel bijgewerkt.")


@router.post("/clips/{cid}/delete")
def delete_clip(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    if clip.status == "rendering":
        return redirect(_back(clip), "Deze clip wordt nu gerenderd; probeer het zo opnieuw.", "error")
    project = clip.project
    base = project_output(clip.project_id)
    for rel in {clip.file_path, clip.raw_path, clip.ass_path, clip.thumb_path, f"clip_{clip.index:02d}.mp4"}:
        p = safe_child(base, rel)
        if p:
            p.unlink(missing_ok=True)
    db.delete(clip)
    db.commit()
    db.refresh(project)
    write_clips_json(db, project)
    return redirect(f"/projects/{project.id}", "Clip verwijderd.")


@router.post("/clips/{cid}/posted")
def mark_posted(
    cid: int,
    platform: str = Form(...),
    url: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    clip = get_clip_for(db, user, cid)
    if platform not in PLATFORMS:
        raise HTTPException(400, "Onbekend platform.")
    url = url.strip()
    if url and urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(400, "De link moet met http:// of https:// beginnen.")
    est = clip_estimate(db, clip)
    clip.posted_platform = platform
    clip.posted_url = url[:500] or None
    if clip.posted_at is None:
        clip.posted_at = utcnow()
        clip.est_mid_at_post, clip.est_low_at_post, clip.est_high_at_post = est["mid"], est["low"], est["high"]
    db.commit()
    return redirect(_back(clip), "Gemarkeerd als gepost. Vul later de echte views in.")


@router.post("/clips/{cid}/unpost")
def unpost(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    clip.posted_platform = clip.posted_url = None
    clip.posted_at = None
    clip.actual_views = None
    clip.est_mid_at_post = clip.est_low_at_post = clip.est_high_at_post = None
    db.commit()
    return redirect(_back(clip), "Markering 'gepost' verwijderd.")


@router.post("/clips/{cid}/views")
def set_views(cid: int, views: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    if clip.posted_at is None:
        raise HTTPException(400, "Markeer de clip eerst als gepost.")
    try:
        n = int(views.replace(".", "").replace(",", "").replace(" ", "").strip())
    except ValueError as exc:
        raise HTTPException(400, "Vul een geheel aantal views in.") from exc
    if n < 0 or n > 10_000_000_000:
        raise HTTPException(400, "Ongeldig aantal views.")
    clip.actual_views = n
    clip.views_updated_at = utcnow()
    db.commit()
    return redirect(_back(clip), "Views opgeslagen; de schatting wordt hiermee gekalibreerd.")


@router.post("/projects/{pid}/clips")
def add_manual_clip(
    pid: int,
    start: str = Form(...),
    end: str = Form(...),
    title: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    project = get_project_for(db, user, pid)
    if project.status in ACTIVE_STATUSES:
        return redirect(f"/projects/{pid}", "Wacht tot de verwerking klaar is.", "error")
    s, e = _parse_time(start), _parse_time(end)
    _validate_range(project, s, e)
    next_index = (db.scalar(select(func.max(Clip.index)).where(Clip.project_id == pid)) or 0) + 1
    subs = {"hook_sterkte": 5, "emotie_of_verrassing": 5, "begrijpelijkheid_zonder_context": 5, "afronding": 5}
    clip = Clip(
        project_id=pid,
        owner_id=project.owner_id,
        index=next_index,
        start=round(s, 2),
        end=round(e, 2),
        title=title.strip()[:300] or f"Handmatige clip {next_index}",
        hook="",
        reason="Handmatig toegevoegd.",
        source="handmatig",
        subscores_json=json.dumps(subs),
        status="pending",
    )
    db.add(clip)
    db.commit()
    _rescore(clip)
    msg = _queue_render(db, clip)
    return redirect(f"/projects/{pid}#clip-{clip.id}", "Clip toegevoegd. " + msg)
