"""Posttekst (titel, beschrijving, hashtags) voor clips, met zo veel mogelijk echte context.

De AI krijgt per clip: wat er gezegd wordt (plus een paar seconden context ervoor), een paar stilstaande
beelden uit de clip (als het model beelden kan lezen) en gegevens over de bron (game, streamer, titel en
beschrijving van de maker). Titels van Twitch-clips worden door kijkers bedacht en gelden als onbetrouwbaar.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.keys import load_llm_config
from app.core.models import Clip, Project
from app.core.paths import find_source
from app.core.store import get_global, user_settings
from app.pipeline import media
from app.pipeline.copywriting import generate_post_texts
from app.pipeline.errors import LLMError
from app.pipeline.run import load_transcript
from app.pipeline.transcribe import all_words

log = logging.getLogger(__name__)

CONTEXT_BEFORE = 6.0  # seconden spraak vóór de clip als context
FRAMES_PER_CLIP = 3


def source_context(project: Project) -> str:
    sm = project.source_meta
    parts = []
    if sm.get("platform"):
        parts.append(f"bron: {sm['platform']}")
    if sm.get("game"):
        parts.append(f"game: {sm['game']}")
    if sm.get("streamer") or sm.get("uploader"):
        parts.append(f"maker/streamer: {sm.get('streamer') or sm.get('uploader')}")
    if sm.get("original_title"):
        label = "titel door een kijker bedacht (ONBETROUWBAAR, niet overnemen)" if sm.get("title_unreliable") \
            else "titel van de originele video"
        parts.append(f"{label}: {sm['original_title']}")
    if sm.get("description"):
        parts.append(f"beschrijving van de maker: {sm['description'][:600]}")
    return "; ".join(parts)


def build_items(project: Project, clips: list[Clip], with_frames: bool) -> list[dict]:
    transcript = load_transcript(project.id) or {"segments": []}
    words = all_words(transcript)
    src = find_source(project.id) if with_frames else None
    ctx = source_context(project)
    unreliable = bool(project.source_meta.get("title_unreliable")) or project.source_type == "twitch"
    items = []
    for c in clips:
        spoken = " ".join(w["word"] for w in words if c.start <= w["start"] < c.end)
        before = " ".join(w["word"] for w in words if c.start - CONTEXT_BEFORE <= w["start"] < c.start)
        item = {
            "id": c.id,
            "text": spoken or "(er wordt in deze clip (bijna) niets gezegd)",
            "before": before,
            "title": c.title,
            "title_unreliable": unreliable or c.source == "volledig",
            "context": ctx,
            "duration": round(c.duration, 1),
            "frames": [],
        }
        if src is not None:
            d = max(0.5, c.duration)
            times = [c.start + d * f for f in (0.15, 0.5, 0.85)][:FRAMES_PER_CLIP]
            try:
                item["frames"] = media.frames_b64(src, times)
            except Exception:  # noqa: BLE001 - zonder beelden gaat het ook
                log.warning("Frames voor clip %s konden niet gemaakt worden", c.id)
        items.append(item)
    return items


def generate_for_clips(db: Session, project: Project, clips: list[Clip]) -> tuple[int, str | None]:
    """Schrijf posttekst voor `clips`. Geeft (aantal bijgewerkt, foutmelding of None)."""
    cfg = load_llm_config(db, "copy")
    if cfg is None:
        return 0, "Er is geen LLM-sleutel ingesteld (zie API-sleutels)."
    if not clips:
        return 0, "Er zijn geen clips."
    with_frames = get_global(db, "llm_vision") != "0"
    lang = user_settings(db, project.owner_id).post_language or "en"
    from app.pipeline.run import _insights

    insights = _insights(db, project.owner_id)
    batch_size = 4 if with_frames else 10
    done = 0
    for i in range(0, len(clips), batch_size):
        batch = clips[i: i + batch_size]
        items = build_items(project, batch, with_frames)
        try:
            result = generate_post_texts(cfg, items, language=lang, insights=insights)
        except LLMError as exc:
            db.commit()
            return done, f"Posttekst genereren mislukt: {exc}"
        for c in batch:
            r = result.get(c.id)
            if r:
                c.title, c.description, c.hashtags = r["title"], r["description"], r["hashtags"]
                done += 1
    db.commit()
    return done, None
