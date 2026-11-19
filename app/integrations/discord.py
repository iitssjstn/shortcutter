"""Discord-meldingen via een webhook (per gebruiker, versleuteld opgeslagen).

Meldingen gaan nooit mee met sleutels of andere geheimen; alleen titels, aantallen en links.
Een mislukte melding stopt nooit het werk.
"""
from __future__ import annotations

import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import get_settings
from app.core.models import Clip, Project, UserSettings, utcnow

log = logging.getLogger(__name__)
WEBHOOK_RE = re.compile(r"^https://(?:canary\.|ptb\.)?(?:discord|discordapp)\.com/api/webhooks/\d{5,30}/[\w-]{20,120}$")
COLOR = {"ok": 0x2ECC71, "info": 0x5865F2, "error": 0xE74C3C}


def _client_factory() -> httpx.Client:  # vervangbaar in tests
    return httpx.Client(timeout=10)


def valid_webhook(url: str) -> bool:
    return bool(WEBHOOK_RE.match((url or "").strip()))


def mask(url: str) -> str:
    """Laat alleen het begin van de webhook zien (het token blijft geheim)."""
    m = re.match(r"^https://[^/]+/api/webhooks/(\d+)/", url or "")
    return f"discord.com/api/webhooks/{m.group(1)[:4]}…/••••" if m else "••••"


def webhook_for(db: Session, user_id: int) -> str | None:
    us = db.get(UserSettings, user_id)
    if us is None or not us.discord_webhook_enc:
        return None
    try:
        return crypto.decrypt_str(us.discord_webhook_enc)
    except Exception:  # noqa: BLE001 - master key gewisseld e.d.
        return None


def link(path: str) -> str:
    """Absolute link naar de app als PUBLIC_URL is ingesteld, anders ''."""
    base = get_settings().public_url
    return f"{base}{path}" if base else ""


def send_to(url: str, title: str, text: str = "", kind: str = "info", url_link: str = "") -> bool:
    embed = {"title": title[:250], "description": text[:3500], "color": COLOR.get(kind, COLOR["info"])}
    if url_link:
        embed["url"] = url_link
    try:
        with _client_factory() as client:
            r = client.post(url, json={"username": "Shortcutter", "embeds": [embed],
                                       "allowed_mentions": {"parse": []}})
        if r.status_code >= 300:
            log.warning("Discord-melding mislukt (HTTP %s)", r.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("Discord-melding mislukt: %s", type(exc).__name__)
        return False


def notify(db: Session, user_id: int, title: str, text: str = "", kind: str = "info", path: str = "") -> bool:
    """Stuur een melding naar de webhook van deze gebruiker (als die is ingesteld)."""
    url = webhook_for(db, user_id)
    if not url:
        return False
    full = link(path) if path else ""
    if full and text:
        text = f"{text}\n{full}"
    elif full:
        text = full
    return send_to(url, title, text, kind, full)


def mark_all_notified(db: Session, user_id: int) -> None:
    """Bij het instellen van een webhook: bestaande clips niet alsnog melden."""
    for c in db.scalars(select(Clip).where(Clip.owner_id == user_id, Clip.notified_at.is_(None))):
        c.notified_at = utcnow()


def digest_ready_clips(db: Session) -> int:
    """Eén bundelbericht per gebruiker voor nieuwe clips die klaarstaan om na te kijken."""
    sent = 0
    users = db.scalars(select(UserSettings).where(UserSettings.discord_webhook_enc.is_not(None))).all()
    for us in users:
        clips = list(db.scalars(
            select(Clip).join(Project, Clip.project_id == Project.id).where(
                Clip.owner_id == us.user_id, Clip.notified_at.is_(None), Clip.status == "ready",
                Project.status == "done", Clip.posted_at.is_(None))
        ).all())
        if not clips:
            continue
        lines = [f"• {c.title[:80]}" for c in clips[:10]]
        if len(clips) > 10:
            lines.append(f"… en nog {len(clips) - 10}")
        if notify(db, us.user_id, f"{len(clips)} nieuwe clip(s) klaar om na te kijken", "\n".join(lines), "ok",
                  "/review"):
            sent += 1
        for c in clips:  # ook bij een mislukte melding: niet blijven herhalen
            c.notified_at = utcnow()
        db.commit()
    return sent
