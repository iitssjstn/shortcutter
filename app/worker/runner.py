"""Eén achtergrondworker (thread) die de wachtrij in de database afwerkt."""
from __future__ import annotations

import logging
import threading
import time

from app.core.db import new_session
from app.core.models import Project
from app.pipeline.run import process_project, rerender_clip
from app.worker import queue
from app.worker.cleanup import run_cleanup

log = logging.getLogger("shortcutter.worker")

POLL_SECONDS = 2.0
CLEANUP_EVERY = 3600.0


def run_job_once() -> bool:
    """Verwerk de volgende job. Geeft False als de wachtrij leeg is."""
    db = new_session()
    try:
        job = queue.claim_next(db)
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
                if run_job_once():
                    continue
            except Exception:
                log.exception("Fout in de worker-lus")
            self._stop.wait(POLL_SECONDS)
