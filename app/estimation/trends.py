"""Wat werkt er nu op YouTube voor dit onderwerp?

Zoekt de best bekeken recente video's over dezelfde game/streamer (YouTube Data API) en vat hun
tags, hashtags en titels samen. Die samenvatting gaat mee in de prompt, zodat de AI tags en hashtags
kiest die mensen echt gebruiken en zoeken. Titels zijn inspiratie, nooit om over te nemen.

Kosten: een zoekopdracht kost 100 van de standaard 10.000 quota-eenheden per dag; resultaten worden
per onderwerp 24 uur bewaard.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import YoutubeAccount

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
CACHE_SECONDS = 24 * 3600
MAX_RESULTS = 15
DAYS = 30
TAGS_MAX_CHARS = 450  # YouTube staat 500 tekens aan tags toe; wat marge
HASHTAG = re.compile(r"#(\w{2,40})", re.UNICODE)

_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()


def _client_factory() -> httpx.Client:  # vervangbaar in tests
    return httpx.Client(timeout=15)


def enabled(db: Session) -> bool:
    from app.core.store import get_global

    return get_global(db, "trend_tags") != "0"


def query_from_meta(meta: dict, fallback: str = "") -> str:
    """Zoekterm uit de brongegevens: game + streamer, anders de (eerste woorden van de) titel."""
    parts = [str(meta.get("game") or "").strip(), str(meta.get("streamer") or "").strip()]
    q = " ".join(p for p in parts if p)
    if not q:
        q = " ".join((fallback or "").split()[:6])
    return q[:100]


def _auth(db: Session, owner_id: int, client: httpx.Client) -> tuple[dict, dict] | None:
    """(headers, params) voor de API: liefst de gekoppelde YouTube-account, anders de API-sleutel."""
    from app.core.keys import youtube_key
    from app.integrations import youtube_upload as yt

    acc = db.scalar(select(YoutubeAccount).where(YoutubeAccount.user_id == owner_id))
    if acc is not None:
        try:
            return {"Authorization": f"Bearer {yt.access_token(db, acc, client)}"}, {}
        except Exception:  # noqa: BLE001 - dan de API-sleutel proberen
            log.info("Trends: OAuth-token niet beschikbaar, val terug op API-sleutel")
    key = youtube_key(db)
    if key:
        return {"X-Goog-Api-Key": key}, {}
    return None


def summarize(videos: list[dict]) -> dict:
    """Tags/hashtags gewogen naar views; titels van de best bekeken video's."""
    tags: Counter = Counter()
    shown: dict[str, str] = {}
    hashtags: Counter = Counter()
    titles = []
    for v in videos:
        sn = v.get("snippet") or {}
        views = int((v.get("statistics") or {}).get("viewCount") or 0)
        weight = math.log10(views + 10)
        seen = set()
        for t in sn.get("tags") or []:
            t = re.sub(r"\s+", " ", str(t)).strip()
            key = t.lower()
            if 2 <= len(t) <= 30 and key not in seen:
                seen.add(key)
                tags[key] += weight
                shown.setdefault(key, t)
        for h in set(x.lower() for x in HASHTAG.findall(f"{sn.get('title', '')} {sn.get('description', '')}")):
            if h != "shorts":
                hashtags[h] += weight
        if sn.get("title"):
            titles.append((sn["title"][:100], views))
    titles.sort(key=lambda x: x[1], reverse=True)
    return {
        "tags": [shown[k] for k, _ in tags.most_common(25)],
        "hashtags": ["#" + h for h, _ in hashtags.most_common(12)],
        "titles": titles[:6],
        "n": len(videos),
    }


def fetch(db: Session, owner_id: int, query: str, short: bool = True) -> dict | None:
    """Samenvatting van de best bekeken video's van de afgelopen 30 dagen voor `query` (of None)."""
    query = (query or "").strip()
    if not query or not enabled(db):
        return None
    key = f"{query.lower()}|{'short' if short else 'long'}"
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    result = None
    try:
        with _client_factory() as client:
            auth = _auth(db, owner_id, client)
            if auth is None:
                return None
            headers, extra = auth
            since = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            params = {"part": "id", "q": query, "type": "video", "order": "viewCount", "maxResults": MAX_RESULTS,
                      "publishedAfter": since, "videoDuration": "short" if short else "medium", **extra}
            r = client.get(f"{API}/search", params=params, headers=headers)
            if r.status_code != 200:
                log.warning("Trends: zoeken mislukt (HTTP %s)", r.status_code)
                return None
            ids = [it.get("id", {}).get("videoId") for it in r.json().get("items", [])]
            ids = [i for i in ids if i]
            if ids:
                r = client.get(f"{API}/videos", params={"part": "snippet,statistics", "id": ",".join(ids), **extra},
                               headers=headers)
                if r.status_code == 200:
                    result = summarize(r.json().get("items", []))
                    result["query"] = query
    except httpx.HTTPError as exc:
        log.warning("Trends ophalen mislukt: %s", type(exc).__name__)
        return None
    with _lock:
        _cache[key] = (time.time() + CACHE_SECONDS, result)
    return result


def prompt_text(tr: dict | None) -> str:
    """Compacte tekst voor in een prompt ('' zonder gegevens)."""
    if not tr or not (tr.get("tags") or tr.get("hashtags")):
        return ""
    lines = [f"WAT NU WERKT OP YOUTUBE voor '{tr.get('query', '')}' (best bekeken video's van de afgelopen "
             f"{DAYS} dagen, {tr.get('n', 0)} stuks):"]
    if tr.get("tags"):
        lines.append("Veelgebruikte tags: " + ", ".join(tr["tags"][:25]))
    if tr.get("hashtags"):
        lines.append("Veelgebruikte hashtags: " + " ".join(tr["hashtags"][:12]))
    if tr.get("titles"):
        lines.append("Titels die goed liepen (alleen als inspiratie voor stijl, NIET overnemen):")
        lines += [f"- {t} ({v:,} views)".replace(",", ".") for t, v in tr["titles"][:6]]
    lines.append("Kies tags en hashtags die ECHT bij de clip passen; liefst uit deze lijsten als ze kloppen. "
                 "Geen misleidende of niet-passende populaire tags.")
    return "\n".join(lines)


def normalize_tags(raw, limit_chars: int = TAGS_MAX_CHARS) -> str:
    """Verborgen tags: zonder #, uniek, elk max. 30 tekens, samen binnen de YouTube-limiet. Komma-gescheiden."""
    items = re.split(r"[,\n]+", raw) if isinstance(raw, str) else [str(x) for x in (raw or [])]
    out: list[str] = []
    total = 0
    for t in items:
        t = re.sub(r"\s+", " ", t.strip().lstrip("#").replace("<", "").replace(">", "")).strip()
        if not t or len(t) > 30 or t.lower() in {x.lower() for x in out}:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)  # YouTube telt quotes rond tags met spaties
        if total + cost > limit_chars:
            break
        out.append(t)
        total += cost
    return ", ".join(out[:20])


def upload_tags(yt_tags: str | None, hashtags: str | None) -> list[str]:
    """Tags voor de upload: de verborgen tags plus de hashtags (zonder #), binnen de limiet."""
    merged = [t.strip() for t in (yt_tags or "").split(",") if t.strip()]
    merged += [h.lstrip("#") for h in (hashtags or "").split() if h.strip("#")]
    return [t for t in normalize_tags(merged).split(", ") if t]
