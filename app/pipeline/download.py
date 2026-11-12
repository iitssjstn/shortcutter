"""Download van de bron met yt-dlp (max 1080p, mp4, geen playlists)."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.pipeline.errors import DownloadBlocked, DownloadFailed

log = logging.getLogger(__name__)

FORMAT = (
    "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
    "b[height<=1080][ext=mp4]/"
    "bv*[height<=1080]+ba/"
    "b[height<=1080]"
)

_BLOCKED_PATTERNS = re.compile(
    r"sign in to confirm|not a bot|http error 403|forbidden|429|too many requests|"
    r"age-restricted|confirm your age|private video|members-only|cookies|login required|"
    r"unavailable in your country|blocked",
    re.I,
)

YOUTUBE_RE = re.compile(r"^https?://([a-z0-9-]+\.)?(youtube\.com|youtu\.be)/", re.I)


def is_valid_url(url: str) -> bool:
    return bool(YOUTUBE_RE.match(url.strip())) and len(url) < 500


def _clean_error(msg: str) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    return msg.replace("ERROR: ", "").strip()[:400]


def download(url: str, out_dir: Path, cookies_file: Path | None = None) -> tuple[Path, dict]:
    """Download naar out_dir/source.mp4. Geeft (pad, info) terug."""
    import yt_dlp  # lazy import

    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": FORMAT,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "overwrites": True,
    }
    if cookies_file is not None:
        opts["cookiefile"] = str(cookies_file)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # yt_dlp.utils.DownloadError e.a.
        msg = _clean_error(str(exc))
        if _BLOCKED_PATTERNS.search(msg):
            raise DownloadBlocked(
                "YouTube blokkeert de download ("
                + msg
                + "). Upload het bronbestand zelf, of voeg een cookies.txt toe bij API-sleutels."
            ) from exc
        raise DownloadFailed(f"Downloaden mislukt: {msg}") from exc
    if info is None:
        raise DownloadFailed("Downloaden mislukt: geen video-informatie ontvangen.")
    if info.get("_type") == "playlist":
        raise DownloadFailed("Playlists worden niet ondersteund; plak de link van één video.")
    candidates = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    candidates = [p for p in candidates if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if not candidates:
        raise DownloadFailed("Downloaden mislukt: geen videobestand gevonden.")
    meta = {
        "title": info.get("title") or "",
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
    }
    return candidates[0], meta
