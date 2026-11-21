"""Leren van eigen resultaten.

* Views/likes/reacties van op YouTube geplaatste clips automatisch ophalen.
* Uit de best en slechtst presterende clips laat de AI een paar concrete lessen trekken.
* Die lessen + voorbeelden gaan mee in de prompts voor clipkeuze en posttekst.

Er wordt niets 'getraind': het model krijgt bij elke opdracht de ervaring van dit kanaal mee.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import Clip, YoutubeAccount, utcnow
from app.core.store import user_settings

log = logging.getLogger(__name__)

MIN_POINTS = 5  # minimaal zoveel geposte clips met views voordat er lessen getrokken worden
REFRESH_AFTER_NEW = 3  # lessen opnieuw trekken na zoveel nieuwe datapunten
MAX_CONTEXT = 1800  # tekens context in een prompt
YT_ID = re.compile(r"(?:youtube\.com/(?:shorts/|watch\?v=|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")
FRAMING_NL = {"crop": "gevuld", "blur": "passend", "vertical": "staand", "split": "gesplitst"}


def youtube_id(clip: Clip) -> str | None:
    if clip.yt_video_id:
        return clip.yt_video_id
    m = YT_ID.search(clip.posted_url or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- views ophalen
def _client_factory() -> httpx.Client:  # vervangbaar in tests
    return httpx.Client(timeout=20)


def sync_user_stats(db: Session, user_id: int) -> int:
    """Haal views/likes/reacties op voor de op YouTube geplaatste clips van deze gebruiker.

    Gebruikt de gekoppelde YouTube-account (OAuth); zonder koppeling de YouTube API-sleutel.
    Geeft het aantal bijgewerkte clips terug.
    """
    from app.core.keys import youtube_key
    from app.integrations import youtube_upload as yt

    clips = [c for c in db.scalars(select(Clip).where(Clip.owner_id == user_id, Clip.posted_at.is_not(None)))
             if youtube_id(c)]
    if not clips:
        return 0
    acc = db.scalar(select(YoutubeAccount).where(YoutubeAccount.user_id == user_id))
    by_id: dict[str, list[Clip]] = {}
    for c in clips:
        by_id.setdefault(youtube_id(c), []).append(c)
    updated = 0
    with _client_factory() as client:
        headers, params_extra = {}, {}
        token = None
        if acc is not None:
            try:
                token = yt.access_token(db, acc, client)
            except yt.YoutubeError as exc:  # koppeling verlopen/ingetrokken: val terug op de API-sleutel
                log.warning("YouTube-koppeling van gebruiker %s werkt niet (%s)", user_id, exc.kind)
                db.rollback()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            key = youtube_key(db)
            if not key:
                return 0
            headers["X-Goog-Api-Key"] = key
        ids = list(by_id)
        for i in range(0, len(ids), 50):
            r = client.get(f"{yt.API}/videos", params={"part": "statistics", "id": ",".join(ids[i:i + 50]), **params_extra},
                           headers=headers)
            if r.status_code != 200:
                log.warning("YouTube-statistieken ophalen mislukt (HTTP %s)", r.status_code)
                break
            for item in r.json().get("items", []):
                st = item.get("statistics") or {}
                for c in by_id.get(item.get("id"), []):
                    c.actual_views = int(st.get("viewCount", c.actual_views or 0))
                    c.likes = int(st["likeCount"]) if "likeCount" in st else c.likes
                    c.comments = int(st["commentCount"]) if "commentCount" in st else c.comments
                    c.views_updated_at = utcnow()
                    updated += 1
    us = user_settings(db, user_id)
    us.stats_synced_at = utcnow()
    db.commit()
    try:  # uitgebreide cijfers (Shorts-feed, kijkers na 3 s) als de koppeling dat recht heeft
        from app.estimation import analytics

        analytics.sync(db, user_id)
    except Exception as exc:  # noqa: BLE001 - analytics zijn een extra
        db.rollback()
        log.warning("YouTube Analytics ophalen mislukt: %s", type(exc).__name__)
    return updated


# ---------------------------------------------------------------- lessen
def performance_rows(db: Session, owner_id: int) -> list[Clip]:
    rows = db.scalars(select(Clip).where(Clip.owner_id == owner_id, Clip.posted_at.is_not(None),
                                         Clip.actual_views.is_not(None))).all()
    return sorted(rows, key=lambda c: c.actual_views or 0, reverse=True)


def _line(c: Clip) -> str:
    sm = c.project.source_meta if c.project else {}
    bits = [f"{c.actual_views} views"]
    if c.likes is not None:
        bits.append(f"{c.likes} likes")
    if c.hook_retention is not None:
        bits.append(f"{min(100, round(c.hook_retention * 100))}% kijkt nog na 3 s")
    if c.avg_view_pct is not None:
        bits.append(f"gem. {c.avg_view_pct:.0f}% bekeken")
    if c.shorts_feed_share is not None:
        bits.append(f"{round(c.shorts_feed_share * 100)}% uit Shorts-feed")
    bits += [f"titel: {c.title[:90]}"]
    if c.hook:
        bits.append(f"hook: {c.hook[:90]}")
    bits += [f"{c.category}", f"{c.duration:.0f}s", FRAMING_NL.get(c.framing_mode or "", c.framing_mode or "-")]
    if sm.get("game"):
        bits.append(f"game: {sm['game']}")
    if sm.get("streamer"):
        bits.append(f"streamer: {sm['streamer']}")
    return "- " + " | ".join(bits)


def examples_text(rows: list[Clip], top: int = 5, bottom: int = 3) -> str:
    best = rows[:top]
    worst = [c for c in rows[-bottom:] if c not in best]
    parts = ["Best presterend:"] + [_line(c) for c in best]
    if worst:
        parts += ["Slechtst presterend:"] + [_line(c) for c in worst]
    return "\n".join(parts)


def stored(db: Session, owner_id: int) -> dict:
    us = user_settings(db, owner_id)
    try:
        return json.loads(us.insights_json or "{}")
    except ValueError:
        return {}


def refresh_lessons(db: Session, owner_id: int, force: bool = False) -> tuple[bool, str]:
    """Laat de AI lessen trekken uit de eigen resultaten. Geeft (bijgewerkt, melding)."""
    from app.core.keys import load_llm_config
    from app.pipeline import pick
    from app.pipeline.errors import LLMError

    rows = performance_rows(db, owner_id)
    if len(rows) < MIN_POINTS:
        return False, f"Nog te weinig data: {len(rows)} van de {MIN_POINTS} geposte clips met views."
    old = stored(db, owner_id)
    if not force and old.get("lessons") and len(rows) - int(old.get("n", 0)) < REFRESH_AFTER_NEW:
        return False, "Lessen zijn nog actueel."
    cfg = load_llm_config(db, "copy")
    if cfg is None:
        return False, "Er is geen LLM-sleutel ingesteld."
    data_lines = "\n".join(_line(c) for c in rows[:40])
    messages = [
        {"role": "system", "content": "Je bent een analist van YouTube Shorts. Je trekt concrete, toetsbare lessen uit "
                                      "echte resultaten. Antwoord uitsluitend met JSON."},
        {"role": "user", "content": (
            "Hieronder de geposte shorts van één kanaal, gesorteerd van meeste naar minste views. Trek 3 tot 6 korte, "
            "concrete lessen (in het Nederlands) over wat werkt en wat niet: type moment, hook, titelstijl, lengte, "
            "en (als die cijfers er zijn) wat 'kijkt nog na 3 s' en 'gem. % bekeken' zeggen over het begin en de lengte, "
            "indeling, game/streamer. Wees eerlijk als de data te weinig zegt. Geef JSON: "
            '{"lessen": ["...", "..."]}\n\n' + data_lines)},
    ]
    try:
        out = pick.chat_json(cfg, messages, retries=2, timeout=60.0)
    except LLMError as exc:
        return False, f"Analyseren mislukt: {exc}"
    lessons = [str(x).strip()[:300] for x in (out.get("lessen") or out.get("lessons") or []) if str(x).strip()][:6]
    if not lessons:
        return False, "De AI gaf geen bruikbare lessen terug."
    us = user_settings(db, owner_id)
    us.insights_json = json.dumps({"lessons": lessons, "n": len(rows),
                                   "generated_at": datetime.utcnow().isoformat(timespec="seconds")}, ensure_ascii=False)
    db.commit()
    return True, f"Lessen bijgewerkt op basis van {len(rows)} clips."


def context_for_prompt(db: Session, owner_id: int) -> str:
    """Compacte ervaring van dit kanaal voor in een prompt ('' als er nog te weinig data is)."""
    rows = performance_rows(db, owner_id)
    if len(rows) < MIN_POINTS:
        return ""
    lessons = stored(db, owner_id).get("lessons") or []
    text = ""
    if lessons:
        text += "Lessen:\n" + "\n".join(f"- {x}" for x in lessons) + "\n"
    text += examples_text(rows)
    return text[:MAX_CONTEXT]


# ---------------------------------------------------------------- op de achtergrond
# Ophalen en analyseren kan (met trage modellen en terugvallers) lang duren; dat gebeurt
# daarom in een aparte thread, zodat de pagina direct terugkomt en niets blijft hangen.
_bg_lock = threading.Lock()
_bg_running: dict[int, threading.Thread] = {}
_bg_result: dict[int, tuple[bool, str]] = {}


def is_running(owner_id: int) -> bool:
    with _bg_lock:
        t = _bg_running.get(owner_id)
        return t is not None and t.is_alive()


def last_result(owner_id: int) -> tuple[bool, str] | None:
    with _bg_lock:
        return _bg_result.pop(owner_id, None)


def _bg_job(owner_id: int, sync: bool, force: bool) -> None:
    from app.core.db import new_session

    db = new_session()
    ok, msg = True, ""
    try:
        if sync:
            try:
                n = sync_user_stats(db, owner_id)
                msg = f"Views van {n} clip(s) bijgewerkt. "
            except Exception as exc:  # noqa: BLE001 - netwerk/koppeling
                db.rollback()
                log.warning("Views ophalen mislukt: %s", type(exc).__name__)
                ok, msg = False, f"Views ophalen mislukt ({type(exc).__name__}). Is je YouTube-kanaal gekoppeld? "
        try:
            done, lesson_msg = refresh_lessons(db, owner_id, force=force)
            if force and not done:
                ok = False
            msg += lesson_msg
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.exception("Lessen trekken mislukt")
            ok, msg = False, msg + f"Analyseren mislukt ({type(exc).__name__})."
    finally:
        db.close()
        with _bg_lock:
            _bg_result[owner_id] = (ok, msg.strip())
            _bg_running.pop(owner_id, None)


def start_background(owner_id: int, sync: bool, force: bool = False) -> bool:
    """Start ophalen/analyseren op de achtergrond. False als er al een run bezig is."""
    with _bg_lock:
        t = _bg_running.get(owner_id)
        if t is not None and t.is_alive():
            return False
        _bg_result.pop(owner_id, None)
        t = threading.Thread(target=_bg_job, args=(owner_id, sync, force), daemon=True, name=f"insights-{owner_id}")
        _bg_running[owner_id] = t
        t.start()
        return True


def wait_idle(owner_id: int, timeout: float = 30.0) -> None:
    """Wacht tot een achtergrondrun klaar is (voor tests)."""
    with _bg_lock:
        t = _bg_running.get(owner_id)
    if t is not None:
        t.join(timeout)
