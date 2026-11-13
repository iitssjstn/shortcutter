"""Dashboard, projecten aanmaken/bekijken en de bijbehorende bestanden."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.common import redirect, render
from app.api.uploads import check_upload, save_upload
from app.core.auth import require_user
from app.core.db import get_db
from app.core.keys import active_llm
from app.core.models import Job, Project, User
from app.core.paths import dir_size, find_source, project_input, project_output, safe_child
from app.core.store import FRAMING_MODES, get_global_int, get_project_for, user_settings
from app.estimation.service import clip_estimate, user_calibration
from app.pipeline.download import is_valid_url
from app.pipeline.run import ACTIVE_STATUSES, load_transcript
from app.worker import queue

router = APIRouter()

PLACEHOLDER = Path(__file__).resolve().parent.parent / "web" / "static" / "placeholder.svg"


def _visible_projects(db: Session, user: User, scope: str):
    q = select(Project).order_by(Project.created_at.desc())
    if not (user.is_admin and scope == "all"):
        q = q.where(Project.owner_id == user.id)
    return list(db.scalars(q).all())


def _dashboard_ctx(db: Session, user: User, scope: str) -> dict:
    projects = _visible_projects(db, user, scope)
    positions = {p.id: queue.position(db, p.id) for p in projects if p.status in ACTIVE_STATUSES}
    return {
        "projects": projects,
        "positions": positions,
        "scope": scope,
        "any_active": any(p.status in ACTIVE_STATUSES for p in projects),
    }


@router.get("/")
def dashboard(request: Request, scope: str = "mine", user: User = Depends(require_user), db: Session = Depends(get_db)):
    ctx = _dashboard_ctx(db, user, scope)
    full_queue = []
    if user.is_admin:
        for job in queue.queue(db):
            p = db.get(Project, job.project_id)
            full_queue.append({"job": job, "project": p, "owner": p.owner.username if p else "?"})
    ctx.update(
        {
            "settings": user_settings(db, user.id),
            "no_llm_key": active_llm(db) is None,
            "full_queue": full_queue,
            "nav": "dashboard",
        }
    )
    return render(request, "dashboard.html", ctx)


@router.get("/partials/projects")
def projects_partial(request: Request, scope: str = "mine", user: User = Depends(require_user), db: Session = Depends(get_db)):
    return render(request, "partials/project_list.html", _dashboard_ctx(db, user, scope))


@router.post("/projects")
def create_project(
    request: Request,
    url: str = Form(""),
    file: UploadFile | None = File(None),
    num_clips: int = Form(5),
    min_dur: int = Form(20),
    max_dur: int = Form(55),
    framing: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    url = url.strip()
    has_file = file is not None and bool(file.filename)
    if not url and not has_file:
        return redirect("/", "Plak een YouTube-link of kies een videobestand.", "error")
    if url and not has_file and not is_valid_url(url):
        return redirect("/", "Dat lijkt geen geldige YouTube-link.", "error")
    if has_file:
        check_upload(file)
    num_clips = max(1, min(20, num_clips))
    min_dur = max(5, min(120, min_dur))
    max_dur = max(min_dur + 5, min(180, max_dur))
    limit = get_global_int(db, "max_active_per_user")
    if limit > 0:
        active = db.scalar(
            select(func.count()).select_from(Project).where(
                Project.owner_id == user.id, Project.status.in_(ACTIVE_STATUSES)
            )
        ) or 0
        if active >= limit:
            return redirect("/", f"Je hebt al {active} actieve project(en); het maximum is {limit}. Wacht tot er een klaar is.", "error")
    us = user_settings(db, user.id)
    project = Project(
        owner_id=user.id,
        source_type="upload" if has_file else "youtube",
        source_url=None if has_file else url,
        source_filename=file.filename[:255] if has_file else None,
        title=(file.filename.rsplit(".", 1)[0][:300] if has_file else ""),
        num_clips=num_clips,
        min_dur=min_dur,
        max_dur=max_dur,
        caption_style=us.caption_style,
        framing=framing if framing in FRAMING_MODES else (us.framing_default or "auto"),
        status="queued",
    )
    db.add(project)
    db.commit()
    if has_file:
        try:
            save_upload(file, project.id)
        except HTTPException:
            db.delete(project)
            db.commit()
            raise
    queue.enqueue(db, project)
    return redirect(f"/projects/{project.id}", "Project aangemaakt en in de wachtrij gezet.")


def _project_ctx(db: Session, user: User, project: Project) -> dict:
    cal = user_calibration(db, project.owner_id)
    clips = [{"clip": c, "est": clip_estimate(db, c, cal)} for c in project.clips]
    transcript = load_transcript(project.id)
    rendering = any(c.status in ("pending", "rendering") for c in project.clips) or bool(
        db.scalar(select(Job.id).where(Job.project_id == project.id, Job.status.in_(("queued", "running"))))
    )
    out_size = dir_size(project_output(project.id))
    in_size = dir_size(project_input(project.id))
    return {
        "project": project,
        "clips": clips,
        "calibration": cal,
        "position": queue.position(db, project.id),
        "busy": project.status in ACTIVE_STATUSES or rendering,
        "has_source": find_source(project.id) is not None,
        "has_thumb": (project_output(project.id) / "thumb.jpg").exists(),
        "segments": (transcript or {}).get("segments", []),
        "disk": {"output": out_size, "input": in_size},
        "baseline": user_settings(db, project.owner_id).channel_avg_views,
    }


@router.get("/projects/{pid}")
def project_page(
    request: Request, pid: int, frame_t: float | None = None,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    from app.pipeline.facecam import parse_box

    project = get_project_for(db, user, pid)
    ctx = _project_ctx(db, user, project)
    ctx["nav"] = "dashboard"
    box = parse_box(project.facecam_box)
    ctx["facecam_pct"] = ",".join(f"{v * 100:.2f}" for v in box) if box else ""
    if frame_t is None:
        frame_t = (project.clips[0].start + 1.0) if project.clips else (project.duration or 10) * 0.3
    ctx["frame_t"] = round(max(0.0, frame_t), 1)
    return render(request, "project.html", ctx)


@router.get("/projects/{pid}/status")
def project_status(request: Request, pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    project = get_project_for(db, user, pid)
    ctx = _project_ctx(db, user, project)
    resp = render(request, "partials/project_status.html", ctx)
    if not ctx["busy"]:
        resp.headers["HX-Refresh"] = "true"
    return resp


@router.post("/projects/{pid}/retry")
def retry_project(pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    project = get_project_for(db, user, pid)
    if project.status in ACTIVE_STATUSES:
        return redirect(f"/projects/{pid}", "Dit project wordt al verwerkt.")
    queue.enqueue(db, project)
    return redirect(f"/projects/{pid}", "Project opnieuw in de wachtrij gezet.")


@router.post("/projects/{pid}/upload-source")
def upload_source(
    pid: int, file: UploadFile = File(...), user: User = Depends(require_user), db: Session = Depends(get_db)
):
    project = get_project_for(db, user, pid)
    if project.status in ACTIVE_STATUSES:
        return redirect(f"/projects/{pid}", "Wacht tot de huidige verwerking klaar is.", "error")
    save_upload(file, project.id)
    project.source_filename = file.filename[:255] if file.filename else None
    project.source_deleted = False
    db.commit()
    if project.status == "done":
        return redirect(f"/projects/{pid}", "Bronbestand geüpload. Je kunt clips nu opnieuw renderen.")
    queue.enqueue(db, project)
    return redirect(f"/projects/{pid}", "Bronbestand geüpload; het project gaat verder waar het gebleven was.")


@router.post("/projects/{pid}/reframe")
def reframe_project(pid: int, framing: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Stel de beeldvulling voor het hele project in en render alle clips opnieuw."""
    project = get_project_for(db, user, pid)
    if framing not in FRAMING_MODES:
        raise HTTPException(400, "Onbekende beeldvulling.")
    if project.status in ACTIVE_STATUSES:
        return redirect(f"/projects/{pid}", "Wacht tot de verwerking klaar is.", "error")
    project.framing = framing
    for c in project.clips:
        c.framing_choice = None
        c.crop_x = None
    db.commit()
    if find_source(pid) is None:
        return redirect(f"/projects/{pid}", "Instelling opgeslagen, maar het bronbestand is verwijderd. "
                        "Upload de bron opnieuw om de clips opnieuw te renderen.", "error")
    for c in project.clips:
        c.status = "pending"
    db.commit()
    for c in project.clips:
        queue.enqueue(db, project, "render_clip", c.id)
    return redirect(f"/projects/{pid}", f"Alle {len(project.clips)} clips worden opnieuw gerenderd.")


@router.get("/projects/{pid}/frame.jpg")
def project_frame(pid: int, t: float = 0.0, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Stilstaand beeld uit de bron (voor het aanwijzen van de facecam)."""
    import subprocess

    project = get_project_for(db, user, pid)
    src = find_source(pid)
    if src is None:
        raise HTTPException(404, "Bronbestand niet (meer) beschikbaar.")
    t = max(0.0, min(float(t), (project.duration or t + 1) - 0.5))
    res = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
         "-vf", "scale=960:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "-"],
        capture_output=True, timeout=60,
    )
    if res.returncode != 0 or not res.stdout:
        raise HTTPException(404, "Kon geen beeld uit de video halen.")
    return Response(res.stdout, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.post("/projects/{pid}/facecam")
def set_facecam(
    pid: int,
    x: str = Form(""),
    y: str = Form(""),
    w: str = Form(""),
    h: str = Form(""),
    clear: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Sla het facecam-kader op (in procenten van het beeld) en render de gesplitste clips opnieuw."""
    from app.pipeline.facecam import format_box, parse_box

    project = get_project_for(db, user, pid)
    if project.status in ACTIVE_STATUSES:
        return redirect(f"/projects/{pid}", "Wacht tot de verwerking klaar is.", "error")
    if clear:
        project.facecam_box = None
        msg = "Facecam wordt weer automatisch herkend."
    else:
        try:
            box = parse_box(",".join(str(float(v) / 100) for v in (x, y, w, h)))
        except ValueError:
            box = None
        if box is None:
            return redirect(f"/projects/{pid}#facecam", "Sleep eerst een rechthoek om de facecam.", "error")
        project.facecam_box = format_box(box)
        msg = "Facecam opgeslagen."
    if project.framing != "split":
        project.framing = "split"
    targets = [c for c in project.clips if (c.framing_choice or project.framing) == "split"]
    db.commit()
    if find_source(pid) is None:
        return redirect(f"/projects/{pid}", msg + " Upload de bron opnieuw om de clips opnieuw te renderen.", "error")
    for c in targets:
        c.status = "pending"
    db.commit()
    for c in targets:
        queue.enqueue(db, project, "render_clip", c.id)
    return redirect(f"/projects/{pid}", f"{msg} {len(targets)} clip(s) worden opnieuw gerenderd (gesplitst).")


@router.post("/projects/{pid}/delete")
def delete_project(pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    project = get_project_for(db, user, pid)
    if any(j.status == "running" for j in db.scalars(select(Job).where(Job.project_id == pid))):
        return redirect(f"/projects/{pid}", "Dit project wordt nu verwerkt; verwijder het als de verwerking klaar is.", "error")
    db.delete(project)
    db.commit()
    shutil.rmtree(project_input(pid), ignore_errors=True)
    shutil.rmtree(project_output(pid), ignore_errors=True)
    return redirect("/", "Project verwijderd.")


@router.get("/projects/{pid}/thumb")
def project_thumb(pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    get_project_for(db, user, pid)
    path = safe_child(project_output(pid), "thumb.jpg")
    if path is None:
        return FileResponse(PLACEHOLDER, media_type="image/svg+xml")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.get("/projects/{pid}/source")
def project_source(pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    get_project_for(db, user, pid)
    src = find_source(pid)
    if src is None:
        raise HTTPException(404, "Bronbestand niet (meer) beschikbaar.")
    return FileResponse(src, media_type="video/mp4")


# ---------------------------------------------------------------- JSON
def project_json(db: Session, project: Project) -> dict:
    cal = user_calibration(db, project.owner_id)
    return {
        "id": project.id,
        "title": project.title,
        "status": project.status,
        "progress": project.progress,
        "error": project.error,
        "error_kind": project.error_kind,
        "warnings": project.warnings,
        "picker": project.picker_used,
        "owner_id": project.owner_id,
        "clips": [
            {
                "id": c.id,
                "index": c.index,
                "start": c.start,
                "end": c.end,
                "title": c.title,
                "hook": c.hook,
                "viral_score": c.viral_score,
                "status": c.status,
                "estimate": {**clip_estimate(db, c, cal), "label": "schatting"},
            }
            for c in project.clips
        ],
    }


@router.get("/api/projects")
def api_projects(scope: str = "mine", user: User = Depends(require_user), db: Session = Depends(get_db)):
    return JSONResponse([project_json(db, p) for p in _visible_projects(db, user, scope)])


@router.get("/api/projects/{pid}")
def api_project(pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return JSONResponse(project_json(db, get_project_for(db, user, pid)))


@router.get("/api/projects/{pid}/transcript")
def api_transcript(pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    get_project_for(db, user, pid)
    t = load_transcript(pid)
    if t is None:
        return Response(status_code=404)
    return JSONResponse(t)
