"""Eén achtergrondworker (thread) die de wachtrij in de database afwerkt."""
from __future__ import annotations

import logging
import threading
import time

from app.core.config import get_settings
from app.core.db import new_session
from app.core.models import Project
from app.pipeline.run import process_project, rerender_clip
from app.worker import queue
from app.worker.cleanup import run_cleanup

log = logging.getLogger("shortcutter.worker")

POLL_SECONDS = 2.0
CLEANUP_EVERY = 3600.0
STATS_EVERY = 6 * 3600.0


def sync_all_stats() -> None:
    """Views van geposte YouTube-clips ophalen en de lessen van elk kanaal bijwerken."""
    from sqlalchemy import select

    from app.core.models import Clip
    from app.estimation.insights import refresh_lessons, sync_user_stats

    db = new_session()
    try:
        owners = set(db.scalars(select(Clip.owner_id).where(Clip.posted_at.is_not(None))).all())
        for uid in owners:
            try:
                sync_user_stats(db, uid)
                refresh_lessons(db, uid)
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("Statistieken bijwerken voor gebruiker %s mislukt", uid)
    finally:
        db.close()


UPLOAD_KINDS = ("upload",)


def run_job_once(kinds: tuple[str, ...] | None = None, exclude: tuple[str, ...] | None = None) -> bool:
    """Verwerk de volgende job. Geeft False als de wachtrij leeg is."""
    db = new_session()
    try:
        job = queue.claim_next(db, kinds=kinds, exclude=exclude)
        if job is None:
            return False
        log.info("Job %s (%s) voor project %s gestart", job.id, job.kind, job.project_id)
        error = None
        try:
            if job.kind == "process":
                process_project(db, job.project_id)
                p = db.get(Project, job.project_id)
                if p is not None and p.status == "failed":
                    error = p.error_kind
            elif job.kind == "render_clip" and job.clip_id:
                rerender_clip(db, job.clip_id)
            elif job.kind == "upload" and job.clip_id:
                from app.api.youtube import run_upload

                if not run_upload(db, job.clip_id):
                    error = "upload"
        except Exception as exc:  # vangnet: de worker mag nooit stoppen
            db.rollback()
            log.exception("Job %s faalde", job.id)
            error = type(exc).__name__
        job = db.merge(job)
        queue.finish(db, job, error)
        return True
    finally:
        db.close()


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cleanup = 0.0
        self._last_stats = time.monotonic() - STATS_EVERY + 600  # eerste keer ±10 min na de start

    def start(self) -> None:
        db = new_session()
        try:
            n = queue.recover(db)
            if n:
                log.info("%s onderbroken job(s) opnieuw in de wachtrij gezet", n)
        finally:
            db.close()
        self._thread = threading.Thread(target=self._loop, name="shortcutter-worker", daemon=True)
        self._thread.start()
        # uploads lopen los van het zware werk, en meerdere tegelijk (vooral netwerk, weinig CPU)
        n_up = max(1, get_settings().upload_concurrency)
        self._uploaders = [threading.Thread(target=self._upload_loop, name=f"shortcutter-upload-{i}", daemon=True)
                           for i in range(n_up)]
        for t in self._uploaders:
            t.start()

    def _upload_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if run_job_once(kinds=UPLOAD_KINDS):
                    continue
            except Exception:
                log.exception("Fout in de upload-lus")
            self._stop.wait(POLL_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if time.monotonic() - self._last_cleanup > CLEANUP_EVERY:
                    self._last_cleanup = time.monotonic()
                    db = new_session()
                    try:
                        run_cleanup(db)
                    finally:
                        db.close()
                if time.monotonic() - self._last_stats > STATS_EVERY:
                    self._last_stats = time.monotonic()
                    sync_all_stats()
                if run_job_once(exclude=UPLOAD_KINDS):
                    continue
            except Exception:
                log.exception("Fout in de worker-lus")
            self._stop.wait(POLL_SECONDS)
