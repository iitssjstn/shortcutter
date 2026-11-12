"""Opslaan van geüploade bronbestanden (streamend, met groottelimiet)."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.core.config import get_settings
from app.core.paths import project_input

ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def check_upload(file: UploadFile | None) -> str | None:
    if file is None or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Bestandstype niet ondersteund. Gebruik: {', '.join(sorted(ALLOWED_EXT))}")
    return ext


def save_upload(file: UploadFile, project_id: int) -> Path:
    ext = check_upload(file)
    dest_dir = project_input(project_id)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)
    dest = dest_dir / f"source{ext}"
    limit = get_settings().max_upload_mb * 1024 * 1024
    written = 0
    with dest.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"Bestand is groter dan {get_settings().max_upload_mb} MB.")
            out.write(chunk)
    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "Het geüploade bestand is leeg.")
    return dest
