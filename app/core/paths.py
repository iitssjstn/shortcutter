"""Paden per project op het data-volume."""
from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings


def project_input(pid: int) -> Path:
    return get_settings().input_dir / str(pid)


def project_output(pid: int) -> Path:
    return get_settings().output_dir / str(pid)


def find_source(pid: int) -> Path | None:
    d = project_input(pid)
    if not d.is_dir():
        return None
    files = [p for p in d.glob("source.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi")]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def safe_child(base: Path, rel: str | None) -> Path | None:
    """Los een opgeslagen (relatief) pad op en controleer dat het binnen base blijft."""
    if not rel:
        return None
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
