"""Gedeelde wachtrij in de database. De worker verwerkt één job tegelijk."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.models import Job, Project, utcnow


def enqueue(db: Session, project: Project, kind: str = "process", clip_id: int | None = None) -> Job:
    existing = db.scalar(
        select(Job).where(
            Job.project_id == project.id,
            Job.kind == kind,
            Job.clip_id.is_(None) if clip_id is None else Job.clip_id == clip_id,
            Job.status.in_(("queued", "running")),
        )
    )
    if existing:
        return existing
    job = Job(project_id=project.id, clip_id=clip_id, owner_id=project.owner_id, kind=kind)
    db.add(job)
    if kind == "process":
        project.status = "queued"
        project.progress = 0
        project.stage_message = "In wachtrij"
        project.error = None
        project.error_kind = None
        project.updated_at = utcnow()
    db.commit()
    return job


def claim_next(db: Session) -> Job | None:
    job = db.scalar(select(Job).where(Job.status == "queued").order_by(Job.created_at, Job.id).limit(1))
    if job is None:
        return None
    job.status = "running"
    job.started_at = utcnow()
    db.commit()
    return job


def finish(db: Session, job: Job, error: str | None = None) -> None:
    job.status = "failed" if error else "done"
    job.error = error
    job.finished_at = utcnow()
    db.commit()


MAX_ATTEMPTS = 2


def recover(db: Session) -> int:
    """Na een herstart: zet jobs die 'running' waren terug in de wachtrij.

    Een job die al MAX_ATTEMPTS keer onderbroken is (bv. omdat de container steeds
    crasht op dezelfde taak) wordt als mislukt gemarkeerd in plaats van opnieuw gestart.
    """
    jobs = db.scalars(select(Job).where(Job.status == "running")).all()
    for j in jobs:
        j.attempts = (j.attempts or 0) + 1
        if j.attempts >= MAX_ATTEMPTS:
            j.status = "failed"
            j.error = "onderbroken"
            j.finished_at = utcnow()
            project = db.get(Project, j.project_id)
            msg = ("De server stopte onverwacht tijdens deze taak, ook bij een tweede poging "
                   "(vaak: geheugen vol). Kies een kleiner Whisper-model bij Instellingen en probeer opnieuw.")
            if project is not None and j.kind == "process":
                project.status = "failed"
                project.error = msg
                project.error_kind = "onderbroken"
            elif j.clip_id:
                from app.core.models import Clip

                clip = db.get(Clip, j.clip_id)
                if clip is not None:
                    clip.status = "failed"
                    clip.render_error = msg
        else:
            j.status = "queued"
            j.started_at = None
    db.commit()
    return len(jobs)


def queue(db: Session) -> list[Job]:
    return list(
        db.scalars(
            select(Job).where(Job.status.in_(("queued", "running"))).order_by(Job.status.desc(), Job.created_at, Job.id)
        ).all()
    )


def position(db: Session, project_id: int) -> int | None:
    """1 = als volgende aan de beurt; 0 = wordt nu verwerkt; None = niet in de wachtrij."""
    job = db.scalar(
        select(Job).where(Job.project_id == project_id, Job.status.in_(("queued", "running"))).order_by(Job.id)
    )
    if job is None:
        return None
    if job.status == "running":
        return 0
    ahead = db.scalar(
        select(func.count()).select_from(Job).where(
            Job.status == "queued", (Job.created_at < job.created_at) | ((Job.created_at == job.created_at) & (Job.id < job.id))
        )
    ) or 0
    return ahead + 1
