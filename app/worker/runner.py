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
SCHEDULE_EVERY = 300.0  # wekelijkse compilaties: elke 5 minuten kijken of er een aan de beurt is


def sync_all_stats() -> None:
    """Views van geposte YouTube-clips ophalen en de lessen van elk kanaal bijwerken."""
    from sqlalchemy import select

    from app.core.models import Clip
    from app.estimation.insights import refresh_lessons, sync_user_stats

    db = new_session()
    try:
        owners = set(db.scalars(select(Clip.owner_id).where(Clip.posted_at.is_not(None))).all())
        for uid in owners:
            for step in (sync_user_stats, refresh_lessons):  # apart: een mislukte sync blokkeert de lessen niet
                try:
                    step(db, uid)
                except Exception:  # noqa: BLE001
                    db.rollback()
                    log.exception("%s voor gebruiker %s mislukt", step.__name__, uid)
    finally:
        db.close()


UPLOAD_KINDS = ("upload",)


def _notify_upload(db, clip_id: int) -> None:
    """Discord-melding na een clip-upload (faalt stil)."""
    from app.core.models import Clip
    from app.integrations import discord

    try:
        clip = db.get(Clip, clip_id)
        if clip is None:
            return
        if clip.yt_status == "done":
            note = f"\n{clip.yt_error}" if clip.yt_error else ""
            discord.notify(db, clip.owner_id, f"Op YouTube: {clip.title[:150]}",
                           (clip.posted_url or "") + note, "ok")
        elif clip.yt_status == "failed":
            discord.notify(db, clip.owner_id, f"Upload mislukt: {clip.title[:150]}", clip.yt_error or "", "error",
                           f"/projects/{clip.project_id}#clip-{clip.id}")
    except Exception:  # noqa: BLE001
        log.warning("Discord-melding voor clip %s mislukt", clip_id)


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
                _notify_upload(db, job.clip_id)
        except Exception as exc:  # vangnet: de worker mag nooit stoppen
            db.rollback()
            log.exception("Job %s faalde", job.id)
            error = type(exc).__name__
        job = db.merge(job)
        queue.finish(db, job, error)
        return True
    finally:
        db.close()


def run_compilation_once(upload: bool = False) -> bool:
    """Render (hoofdworker) of upload (uploadworkers) de volgende compilatie. False als er niets is."""
    from app.pipeline import compile as cp

    db = new_session()
    try:
        return cp.run_next_upload(db) if upload else cp.run_next_render(db)
    finally:
        db.close()


def run_schedules() -> int:
    """Wekelijkse compilaties, autopilot en het Discord-bundelbericht voor clips die klaarstaan."""
    from app.integrations import discord
    from app.pipeline import autopilot, weekly

    db = new_session()
    n = 0
    try:
        for fn in (weekly.run_due, autopilot.run_due, discord.digest_ready_clips):
            try:
                n += fn(db) or 0
            except Exception:  # noqa: BLE001
                db.rollback()
                log.exception("Planning %s mislukt", getattr(fn, "__qualname__", fn))
        return n
    finally:
        db.close()


class Worker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cleanup = 0.0
        self._last_stats = time.monotonic() - STATS_EVERY + 600  # eerste keer ±10 min na de start
        self._last_schedule = 0.0

    def start(self) -> None:
        db = new_session()
        try:
            n = queue.recover(db)
            if n:
                log.info("%s onderbroken job(s) opnieuw in de wachtrij gezet", n)
            from app.pipeline import compile as cp

            cp.recover(db)
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
                if run_job_once(kinds=UPLOAD_KINDS) or run_compilation_once(upload=True):
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
                if time.monotonic() - self._last_schedule > SCHEDULE_EVERY:
                    self._last_schedule = time.monotonic()
                    run_schedules()
                if run_job_once(exclude=UPLOAD_KINDS) or run_compilation_once():
                    continue
            except Exception:
                log.exception("Fout in de worker-lus")
            self._stop.wait(POLL_SECONDS)
