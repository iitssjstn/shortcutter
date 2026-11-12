"""Downloads: per clip, ondertitels, en streamende zips per project of selectie.

Downloads gebruiken alleen bestanden op schijf en de database: ze werken ook als de
LLM-API of andere externe diensten onbereikbaar zijn.
"""
from __future__ import annotations

import json
import re
import unicodedata
import zipfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.core.auth import require_user
from app.core.db import get_db
from app.core.models import Clip, Project, User
from app.core.paths import project_output, safe_child
from app.core.store import get_clip_for, get_project_for
from app.estimation.service import clip_estimate, user_calibration
from app.pipeline import captions
from app.pipeline.run import load_transcript
from app.pipeline.transcribe import all_words

router = APIRouter()


def slugify(text: str, fallback: str = "clip") -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (text[:60].strip("-")) or fallback


def attachment(filename: str) -> dict:
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    return {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"}


def clip_filename(clip: Clip, suffix: str = "", ext: str = "mp4") -> str:
    return f"{clip.index:02d}-{slugify(clip.title)}{suffix}.{ext}"


def _final_or_raw(clip: Clip) -> Path | None:
    base = project_output(clip.project_id)
    return safe_child(base, clip.file_path) or safe_child(base, clip.raw_path)


@router.get("/clips/{cid}/download")
def download_clip(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    path = _final_or_raw(clip)
    if path is None:
        raise HTTPException(404, "Deze clip is nog niet gerenderd.")
    return FileResponse(path, media_type="video/mp4", headers=attachment(clip_filename(clip)))


@router.get("/clips/{cid}/download-raw")
def download_raw(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    path = safe_child(project_output(clip.project_id), clip.raw_path)
    if path is None:
        raise HTTPException(404, "Er is geen versie zonder ondertitels beschikbaar.")
    return FileResponse(path, media_type="video/mp4", headers=attachment(clip_filename(clip, "-zonder-ondertitels")))


@router.get("/clips/{cid}/subtitles.{fmt}")
def download_subtitles(cid: int, fmt: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    clip = get_clip_for(db, user, cid)
    if fmt not in ("srt", "ass"):
        raise HTTPException(404, "Onbekend formaat.")
    transcript = load_transcript(clip.project_id)
    if transcript is None:
        path = safe_child(project_output(clip.project_id), clip.ass_path)
        if fmt == "ass" and path:
            return FileResponse(path, media_type="text/plain", headers=attachment(clip_filename(clip, ext="ass")))
        raise HTTPException(404, "Er is geen transcript voor deze clip.")
    words = all_words(transcript)
    if fmt == "srt":
        body = captions.build_srt(words, clip.start, clip.end)
    else:
        body = captions.build_ass(words, clip.start, clip.end, clip.project.caption_style)
    return Response(body, media_type="text/plain; charset=utf-8", headers=attachment(clip_filename(clip, ext=fmt)))


# ---------------------------------------------------------------- zip
class _Sink:
    """Niet-seekbare schrijfbuffer zodat zipfile streamend kan schrijven."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.pos = 0

    def write(self, b: bytes) -> int:
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self.pos

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def posting_text(project: Project, clips: list[Clip], estimates: dict[int, dict]) -> str:
    lines = [f"Project: {project.title}", "=" * 60, ""]
    for c in clips:
        est = estimates[c.id]
        lines += [
            f"[{c.index:02d}] {c.title}",
            f"Bestand: {clip_filename(c)}",
            "",
            "Titel:",
            c.title,
            "",
            "Beschrijving:",
            (c.hook + "\n\n" if c.hook else "") + "#shorts",
            "",
            f"Viral score: {c.viral_score}/10 · geschatte views (schatting): "
            f"{est['low']}–{est['high']} (midden {est['mid']})",
            "-" * 60,
            "",
        ]
    return "\n".join(lines)


def stream_zip(project: Project, clips: list[Clip], estimates: dict[int, dict]) -> Iterator[bytes]:
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", allowZip64=True) as zf:
        meta = {
            "project": project.title,
            "bron": project.source_url or project.source_filename,
            "clips": [
                {
                    "bestand": f"clips/{clip_filename(c)}",
                    "titel": c.title,
                    "hook": c.hook,
                    "viral_score": c.viral_score,
                    "categorie": c.category,
                    "start": c.start,
                    "einde": c.end,
                    "geschatte_views": {
                        "laag": estimates[c.id]["low"],
                        "midden": estimates[c.id]["mid"],
                        "hoog": estimates[c.id]["high"],
                        "label": "schatting",
                    },
                }
                for c in clips
            ],
        }
        zf.writestr("clips.json", json.dumps(meta, ensure_ascii=False, indent=2), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("titels-en-beschrijvingen.txt", posting_text(project, clips, estimates), compress_type=zipfile.ZIP_DEFLATED)
        yield sink.take()
        for c in clips:
            path = _final_or_raw(c)
            if path is None:
                continue
            info = zipfile.ZipInfo(f"clips/{clip_filename(c)}")
            info.compress_type = zipfile.ZIP_STORED
            info.file_size = path.stat().st_size
            with path.open("rb") as src, zf.open(info, "w", force_zip64=info.file_size > 2**31) as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    yield sink.take()
            yield sink.take()
    yield sink.take()


def _zip_response(db: Session, project: Project, clips: list[Clip], suffix: str = "") -> StreamingResponse:
    if not clips:
        raise HTTPException(404, "Er zijn geen clips om te downloaden.")
    cal = user_calibration(db, project.owner_id)
    estimates = {c.id: clip_estimate(db, c, cal) for c in clips}
    name = f"{slugify(project.title, 'project')}{suffix}.zip"
    return StreamingResponse(stream_zip(project, clips, estimates), media_type="application/zip", headers=attachment(name))


@router.get("/projects/{pid}/zip")
def download_zip(pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    project = get_project_for(db, user, pid)
    return _zip_response(db, project, list(project.clips))


@router.get("/projects/{pid}/zip-selection")
def download_selection(
    pid: int, ids: list[int] = Query(default=[]), user: User = Depends(require_user), db: Session = Depends(get_db)
):
    project = get_project_for(db, user, pid)
    wanted = set(ids)
    clips = [c for c in project.clips if c.id in wanted]
    if not clips:
        raise HTTPException(400, "Selecteer eerst een of meer clips.")
    return _zip_response(db, project, clips, "-selectie")
