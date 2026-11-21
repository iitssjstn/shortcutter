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
TWITCH_RE = re.compile(
    r"^https?://(?:(?:www|m|go)\.)?twitch\.tv/(?:videos/\d+|[A-Za-z0-9_]{2,25}/(?:clip|v|video)/[A-Za-z0-9_-]+)"
    r"|^https?://clips\.twitch\.tv/[A-Za-z0-9_-]+",
    re.I,
)
TWITCH_CLIP_RE = re.compile(r"(?:clips\.twitch\.tv/|twitch\.tv/[A-Za-z0-9_]+/clip/)([A-Za-z0-9_-]+)", re.I)


def url_platform(url: str) -> str | None:
    url = (url or "").strip()
    if len(url) >= 500:
        return None
    if YOUTUBE_RE.match(url):
        return "youtube"
    if TWITCH_RE.match(url):
        return "twitch"
    return None


def is_valid_url(url: str) -> bool:
    return url_platform(url) is not None


def twitch_clip_slug(url: str) -> str | None:
    m = TWITCH_CLIP_RE.search(url or "")
    return m.group(1) if m else None


def _clean_error(msg: str) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    return msg.replace("ERROR: ", "").strip()[:400]


class _NotSingle(Exception):
    pass


SOURCE_NAME = re.compile(r"source\.(mp4|mkv|webm|mov|m4v|avi)", re.I)  # niet source.f137.mp4 (half samengevoegd)


def download(url: str, out_dir: Path, cookies_file: Path | None = None) -> tuple[Path, dict]:
    """Download naar out_dir/source.mp4. Geeft (pad, info) terug."""
    import yt_dlp  # lazy import

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("source.*"):  # restjes van een afgebroken download (bv. source.f137.mp4)
        old.unlink(missing_ok=True)
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
            info = ydl.extract_info(url, download=False)  # eerst kijken wat het is, dan pas downloaden
            if info is not None and info.get("_type") in ("playlist", "multi_video"):
                raise _NotSingle("Playlists en kanalen worden niet ondersteund; plak de link van één video.")
            if info is not None and (info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming")):
                raise _NotSingle("Livestreams kunnen niet; wacht tot de stream voorbij is en plak dan de link.")
            if info is not None:
                info = ydl.process_ie_result(info, download=True)
    except _NotSingle as exc:
        raise DownloadFailed(str(exc)) from exc
    except Exception as exc:  # yt_dlp.utils.DownloadError e.a.
        msg = _clean_error(str(exc))
        if _BLOCKED_PATTERNS.search(msg):
            site = "Twitch" if "twitch.tv" in url else "YouTube"
            raise DownloadBlocked(
                f"{site} blokkeert de download ("
                + msg
                + "). Upload het bronbestand zelf"
                + (", of voeg een cookies.txt toe bij API-sleutels." if site == "YouTube" else ".")
            ) from exc
        raise DownloadFailed(f"Downloaden mislukt: {msg}") from exc
    if info is None:
        raise DownloadFailed("Downloaden mislukt: geen video-informatie ontvangen.")
    if info.get("_type") == "playlist":
        raise DownloadFailed("Playlists worden niet ondersteund; plak de link van één video.")
    candidates = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    candidates = [p for p in candidates if SOURCE_NAME.fullmatch(p.name)]
    if not candidates:
        raise DownloadFailed("Downloaden mislukt: geen videobestand gevonden.")
    meta = {
        "title": info.get("title") or "",
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "description": (info.get("description") or "")[:1500],
        "creator": info.get("creator"),
    }
    return candidates[0], meta
