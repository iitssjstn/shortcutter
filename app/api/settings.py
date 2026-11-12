"""Persoonlijke en globale instellingen, inclusief schijfgebruik en retentie."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.common import redirect, render
from app.core.auth import require_admin, require_user
from app.core.db import get_db
from app.core.keys import active_llm, youtube_key
from app.core.models import Credential, Project, User
from app.core.paths import dir_size, project_input, project_output
from app.core.store import (
    CAPTION_STYLES,
    GLOBAL_DEFAULTS,
    WHISPER_MODELS,
    audit,
    effective_whisper_model,
    get_global,
    set_global,
    user_settings,
)
from app.core.config import get_settings
from app.estimation.youtube import ChannelError, fetch_channel_average

router = APIRouter()


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    ctx = {
        "nav": "settings",
        "us": user_settings(db, user.id),
        "caption_styles": CAPTION_STYLES,
        "has_youtube_key": db.scalar(select(Credential.id).where(Credential.provider == "youtube")) is not None,
    }
    if user.is_admin:
        s = get_settings()
        projects = db.scalars(select(Project).order_by(Project.created_at.desc())).all()
        disk = []
        total = 0
        for p in projects:
            i, o = dir_size(project_input(p.id)), dir_size(project_output(p.id))
            total += i + o
            disk.append({"project": p, "input": i, "output": o})
        llm = active_llm(db)
        ctx.update(
            {
                "g": {k: get_global(db, k) for k in GLOBAL_DEFAULTS},
                "whisper_models": WHISPER_MODELS,
                "effective_whisper": effective_whisper_model(db),
                "env_whisper": s.whisper_model,
                "whisper_device": s.whisper_device,
                "whisper_compute": s.whisper_compute,
                "active_llm_model": llm.model if llm else None,
                "disk": disk,
                "disk_total": total,
            }
        )
    return render(request, "settings.html", ctx)


@router.post("/settings")
def save_user_settings(
    channel_avg_views: int = Form(1000),
    default_num_clips: int = Form(5),
    default_min_dur: int = Form(20),
    default_max_dur: int = Form(55),
    caption_style: str = Form("geel"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    us = user_settings(db, user.id)
    us.channel_avg_views = _clamp(channel_avg_views, 1, 1_000_000_000)
    us.default_num_clips = _clamp(default_num_clips, 1, 20)
    us.default_min_dur = _clamp(default_min_dur, 5, 120)
    us.default_max_dur = _clamp(default_max_dur, us.default_min_dur + 5, 180)
    us.caption_style = caption_style if caption_style in CAPTION_STYLES else "geel"
    db.commit()
    return redirect("/settings", "Instellingen opgeslagen.")


@router.post("/settings/channel-average")
def fetch_channel_avg(channel: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    key = youtube_key(db)
    if not key:
        return redirect("/settings", "Er is geen YouTube Data API-sleutel ingesteld (zie API-sleutels).", "error")
    try:
        avg, n = fetch_channel_average(channel.strip(), key)
    except ChannelError as exc:
        return redirect("/settings", str(exc), "error")
    except httpx.HTTPError:
        return redirect("/settings", "YouTube Data API is niet bereikbaar. Probeer het later opnieuw.", "error")
    us = user_settings(db, user.id)
    us.channel_avg_views = max(1, avg)
    us.channel_ref = channel.strip()[:120]
    db.commit()
    return redirect("/settings", f"Kanaalgemiddelde bijgewerkt naar {avg} views (mediaan van {n} recente shorts).")


@router.post("/settings/global")
def save_global(
    whisper_model: str = Form(""),
    llm_model_override: str = Form(""),
    max_active_per_user: int = Form(0),
    source_retention_days: int = Form(7),
    project_retention_days: int = Form(0),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if whisper_model and whisper_model not in WHISPER_MODELS:
        return redirect("/settings", "Onbekend Whisper-model.", "error")
    set_global(db, "whisper_model", whisper_model)
    set_global(db, "llm_model_override", llm_model_override.strip()[:120])
    set_global(db, "max_active_per_user", str(_clamp(max_active_per_user, 0, 1000)))
    set_global(db, "source_retention_days", str(_clamp(source_retention_days, 0, 3650)))
    set_global(db, "project_retention_days", str(_clamp(project_retention_days, 0, 3650)))
    audit(db, user, "globale_instellingen_gewijzigd", "instellingen")
    db.commit()
    return redirect("/settings", "Globale instellingen opgeslagen.")


@router.post("/settings/cleanup-now")
def cleanup_now(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    from app.worker.cleanup import run_cleanup

    st = run_cleanup(db)
    return redirect(
        "/settings",
        f"Opgeruimd: {st['sources_removed']} bronbestand(en), {st['projects_removed']} project(en).",
    )
