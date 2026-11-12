"""Orchestratie van de pipeline per project en per clip."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.keys import cookies_file, load_llm_config
from app.core.models import Clip, Project, utcnow
from app.core.paths import find_source, project_input, project_output
from app.core.store import effective_whisper_model
from app.estimation.features import compute_features
from app.estimation.estimator import viral_score
from app.pipeline import captions, framing, media, render
from app.pipeline.download import download
from app.pipeline.errors import PipelineError, RenderError, SourceMissing
from app.pipeline.pick import pick_clips
from app.pipeline.transcribe import all_words, transcribe

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "queued": "In wachtrij",
    "downloading": "Downloaden",
    "transcribing": "Transcriberen",
    "picking": "Fragmenten kiezen",
    "rendering": "Renderen",
    "done": "Klaar",
    "failed": "Fout",
    "waiting_source": "Wacht op bronbestand",
}
ACTIVE_STATUSES = ("queued", "downloading", "transcribing", "picking", "rendering")


def _set(db: Session, project: Project, status: str | None = None, progress: int | None = None, msg: str | None = None):
    if status:
        project.status = status
    if progress is not None:
        project.progress = max(0, min(100, int(progress)))
    if msg is not None:
        project.stage_message = msg[:300]
    project.updated_at = utcnow()
    db.commit()


def load_transcript(pid: int) -> dict | None:
    p = project_output(pid) / "transcript.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def clip_basename(clip: Clip) -> str:
    return f"clip_{clip.index:02d}"


def write_clips_json(db: Session, project: Project) -> Path:
    from app.estimation.service import clip_estimate

    out = project_output(project.id)
    out.mkdir(parents=True, exist_ok=True)
    data = []
    for c in project.clips:
        est = clip_estimate(db, c)
        data.append(
            {
                "index": c.index,
                "bestand": f"{clip_basename(c)}.mp4",
                "start": c.start,
                "einde": c.end,
                "duur": round(c.duration, 2),
                "titel": c.title,
                "hook": c.hook,
                "reden": c.reason,
                "beschrijving": c.description,
                "hashtags": c.hashtags,
                "posttekst": c.post_text,
                "categorie": c.category,
                "bron": c.source,
                "viral_score": c.viral_score,
                "subscores": c.subscores,
                "geschatte_views": {"laag": est["low"], "midden": est["mid"], "hoog": est["high"], "label": "schatting"},
                "status": c.status,
            }
        )
    path = out / "clips.json"
    path.write_text(json.dumps({"project": project.title, "clips": data}, ensure_ascii=False, indent=2))
    return path


def render_clip(db: Session, clip: Clip, source: Path, info: dict, transcript: dict) -> None:
    """Render één clip. Faalt nooit hard: zet clip.status en render_error."""
    project = clip.project
    out_dir = project_output(project.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = clip_basename(clip)
    raw = out_dir / f"{base}_raw.mp4"
    ass = out_dir / f"{base}.ass"
    final = out_dir / f"{base}.mp4"
    thumb = out_dir / f"{base}.jpg"
    clip.status = "rendering"
    clip.render_error = None
    db.commit()
    words = all_words(transcript)
    try:
        mode = clip.framing_choice or project.framing or "auto"
        fr = framing.choose_framing(source, info["width"], info["height"], clip.start, clip.end,
                                    force_mode=mode, face_x=clip.crop_x)
        clip.framing_mode = fr.mode
        captions.write_ass(ass, words, clip.start, clip.end, project.caption_style)
        clip.ass_path = ass.name
        for p in (final,):
            if p.exists():
                p.unlink()
        render.render_raw(source, clip.start, clip.end, fr.filter, raw, has_audio=info.get("has_audio", True))
        clip.raw_path = raw.name
        media.thumbnail(raw, thumb, at=min(1.0, clip.duration / 2))
        clip.thumb_path = thumb.name if thumb.exists() else None
        feats = compute_features(clip.start, clip.end, clip.subscores, words,
                                 fr.face_ratio if fr.mode == "crop" else None)
        clip.features_json = json.dumps(feats)
        clip.viral_score = viral_score(feats)
        db.commit()
    except RenderError as exc:
        clip.status = "failed"
        clip.render_error = exc.message
        clip.file_path = None
        db.commit()
        return
    if clip.captions_enabled:
        try:
            render.burn_subtitles(raw, ass, final)
            clip.file_path = final.name
        except RenderError as exc:
            clip.status = "failed"
            clip.render_error = "Ondertitels inbranden mislukt; de ruwe versie en de .ass zijn wel te downloaden. " + exc.message
            clip.file_path = None
            db.commit()
            return
    else:
        clip.file_path = raw.name
    clip.status = "ready"
    db.commit()


def _probe(project: Project, source: Path) -> dict:
    try:
        info = media.probe(source)
    except RuntimeError as exc:
        raise SourceMissing(f"Het bronbestand is onleesbaar: {exc}") from exc
    project.width, project.height, project.duration = info["width"], info["height"], info["duration"]
    return info


def process_project(db: Session, project_id: int, **pick_kwargs) -> None:
    project = db.get(Project, project_id)
    if project is None:
        return
    s = get_settings()
    project.error = None
    project.error_kind = None
    out_dir = project_output(project.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 1. bron
        source = find_source(project.id)
        if source is None:
            if project.source_type != "youtube" or not project.source_url:
                raise SourceMissing("Het bronbestand ontbreekt. Upload het bestand opnieuw om verder te gaan.")
            _set(db, project, "downloading", 3, "Video downloaden met yt-dlp…")
            with cookies_file(db, project_input(project.id)) as cookies:
                source, meta = download(project.source_url, project_input(project.id), cookies)
            if meta.get("title"):
                project.title = meta["title"][:300]
        project.source_deleted = False
        info = _probe(project, source)
        if not project.title:
            project.title = project.source_filename or f"Project {project.id}"
        media.thumbnail(source, out_dir / "thumb.jpg", at=min(5.0, info["duration"] / 3 if info["duration"] else 0))
        _set(db, project, None, 10)

        # 2. transcriptie
        _set(db, project, "transcribing", 12, f"Transcriberen met Whisper ({effective_whisper_model(db)})…")

        def tprog(frac: float) -> None:
            _set(db, project, None, 12 + int(frac * 43))

        transcript = transcribe(
            source, out_dir / "transcript.json", effective_whisper_model(db), s.whisper_device, s.whisper_compute, tprog,
            threads=s.whisper_threads,
        )

        # 3. fragmentkeuze (alleen als er nog geen clips zijn, bv. bij opnieuw proberen)
        if not project.clips:
            _set(db, project, "picking", 58, "Fragmenten kiezen…")
            cfg = load_llm_config(db)
            from app.core.store import user_settings

            lang = user_settings(db, project.owner_id).post_language or "en"
            picked, method, warning = pick_clips(
                transcript, project.num_clips, project.min_dur, project.max_dur, cfg,
                post_language=lang, **pick_kwargs,
            )
            project.picker_used = method
            if warning:
                project.add_warning(warning)
            if not picked:
                project.add_warning("Er zijn geen geschikte fragmenten gevonden. Voeg handmatig een clip toe.")
            for i, c in enumerate(picked, 1):
                db.add(
                    Clip(
                        project_id=project.id,
                        owner_id=project.owner_id,
                        index=i,
                        start=c["start"],
                        end=c["end"],
                        title=c["title"],
                        hook=c["hook"],
                        reason=c["reason"],
                        description=c.get("description", ""),
                        hashtags=c.get("hashtags", ""),
                        category=c["category"],
                        subscores_json=json.dumps(c["subscores"]),
                        source=c["source"],
                    )
                )
            db.commit()
            db.refresh(project)

        # 4-6. framing, ondertitels, render
        todo = [c for c in project.clips if c.status != "ready"]
        for n, clip in enumerate(todo):
            _set(db, project, "rendering", 62 + int(36 * n / max(1, len(todo))), f"Clip {n + 1} van {len(todo)} renderen…")
            render_clip(db, clip, source, info, transcript)
        failed = [c for c in project.clips if c.status == "failed"]
        if failed:
            project.add_warning(
                f"{len(failed)} clip(s) konden niet volledig gerenderd worden. Probeer 'opnieuw renderen'."
            )
        write_clips_json(db, project)
        project.finished_at = utcnow()
        _set(db, project, "done", 100, "Klaar")
    except PipelineError as exc:
        db.rollback()
        project = db.get(Project, project_id)
        project.error = exc.message
        project.error_kind = exc.kind
        _set(db, project, "failed", None, STATUS_LABELS["failed"])
        log.warning("Project %s mislukt (%s)", project_id, exc.kind)
    except Exception as exc:  # onverwacht: toon type, geen details die gevoelig kunnen zijn
        db.rollback()
        log.exception("Onverwachte fout in project %s", project_id)
        project = db.get(Project, project_id)
        project.error = f"Onverwachte fout ({type(exc).__name__}). Bekijk de containerlogs en probeer opnieuw."
        project.error_kind = "onverwacht"
        _set(db, project, "failed", None, STATUS_LABELS["failed"])


def rerender_clip(db: Session, clip_id: int) -> None:
    clip = db.get(Clip, clip_id)
    if clip is None:
        return
    project = clip.project
    source = find_source(project.id)
    if source is None:
        clip.status = "failed"
        clip.render_error = "Het bronbestand is verwijderd (retentie). Upload de bron opnieuw om te renderen."
        db.commit()
        return
    transcript = load_transcript(project.id) or {"segments": []}
    try:
        info = media.probe(source)
    except RuntimeError as exc:
        clip.status = "failed"
        clip.render_error = str(exc)
        db.commit()
        return
    render_clip(db, clip, source, info, transcript)
    write_clips_json(db, project)
