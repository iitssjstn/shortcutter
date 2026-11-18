"""Twitch Helix API: kanalen en clips ophalen (app-token via client credentials).

De client-ID en het client-secret staan versleuteld in de database (API-sleutels, provider 'twitch').
Er wordt alleen gelezen: Shortcutter plaatst niets op Twitch.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.models import Credential, utcnow

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
PERIODS = {"dag": 1, "week": 7, "maand": 30, "alles": None}
PERIOD_LABELS = {"dag": "Afgelopen 24 uur", "week": "Afgelopen week", "maand": "Afgelopen maand", "alles": "Altijd"}
LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")
CHANNEL_IN_URL = re.compile(r"twitch\.tv/([A-Za-z0-9_]{3,25})/(?:clip|v|video)/", re.I)
CACHE_SECONDS = 300

_tokens: dict[str, tuple[str, float]] = {}
_cache: dict[tuple, tuple[float, object]] = {}


def _client_factory() -> httpx.Client:  # vervangbaar in tests
    return httpx.Client(timeout=15)


class TwitchError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind  # error | invalid | rate_limit | no_key | not_found


def twitch_credit(url: str) -> str | None:
    m = CHANNEL_IN_URL.search(url or "")
    return f"twitch.tv/{m.group(1).lower()}" if m else None


# ---------------------------------------------------------------- sleutel
def credential(db: Session) -> Credential | None:
    return db.scalar(select(Credential).where(Credential.provider == "twitch"))


def load_creds(db: Session) -> tuple[str, str] | None:
    cred = credential(db)
    if cred is None:
        return None
    try:
        data = json.loads(crypto.decrypt_str(cred.encrypted_value))
    except (RuntimeError, ValueError):
        return None
    cred.last_used_at = utcnow()
    db.commit()
    return data.get("client_id", ""), data.get("client_secret", "")


def encode_creds(client_id: str, client_secret: str) -> bytes:
    return crypto.encrypt(json.dumps({"client_id": client_id, "client_secret": client_secret}))


# ---------------------------------------------------------------- API
def get_token(client_id: str, secret: str, client: httpx.Client, force: bool = False) -> str:
    cached = _tokens.get(client_id)
    if cached and not force and cached[1] > time.time() + 60:
        return cached[0]
    try:
        r = client.post(TOKEN_URL, data={"client_id": client_id, "client_secret": secret,
                                         "grant_type": "client_credentials"})
    except httpx.HTTPError as exc:
        raise TwitchError(f"Twitch is niet bereikbaar ({type(exc).__name__}).") from exc
    if r.status_code in (400, 401, 403):
        raise TwitchError("Twitch weigert de client-ID of het client-secret.", "invalid")
    if r.status_code == 429:
        raise TwitchError("Rate limit bij Twitch; probeer het zo opnieuw.", "rate_limit")
    if r.status_code != 200:
        raise TwitchError(f"Onverwacht antwoord van Twitch (HTTP {r.status_code}).")
    body = r.json()
    token = body.get("access_token")
    if not token:
        raise TwitchError("Twitch gaf geen token terug.")
    _tokens[client_id] = (token, time.time() + float(body.get("expires_in", 3600)))
    return token


def helix(creds: tuple[str, str], path: str, params, client: httpx.Client) -> dict:
    client_id, secret = creds
    for attempt in range(2):
        token = get_token(client_id, secret, client, force=attempt > 0)
        try:
            r = client.get(f"{HELIX}/{path}", params=params,
                           headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise TwitchError(f"Twitch is niet bereikbaar ({type(exc).__name__}).") from exc
        if r.status_code == 401 and attempt == 0:
            continue  # token verlopen: één keer vernieuwen
        if r.status_code == 429:
            raise TwitchError("Rate limit bij Twitch; probeer het zo opnieuw.", "rate_limit")
        if r.status_code in (401, 403):
            raise TwitchError("Twitch weigert de sleutel.", "invalid")
        if r.status_code != 200:
            raise TwitchError(f"Onverwacht antwoord van Twitch (HTTP {r.status_code}).")
        return r.json()
    raise TwitchError("Twitch weigert de sleutel.", "invalid")


def _creds_or_raise(db: Session) -> tuple[str, str]:
    creds = load_creds(db)
    if not creds or not creds[0] or not creds[1]:
        raise TwitchError("Er is nog geen Twitch API-sleutel ingesteld (API-sleutels, alleen beheerder).", "no_key")
    return creds


def get_user(db: Session, login: str) -> dict | None:
    creds = _creds_or_raise(db)
    with _client_factory() as client:
        data = helix(creds, "users", {"login": login.lower()}, client)
    users = data.get("data") or []
    return users[0] if users else None


def _clip(d: dict) -> dict:
    thumb = d.get("thumbnail_url") or ""
    return {
        "id": d.get("id", ""),
        "url": d.get("url") or f"https://clips.twitch.tv/{d.get('id', '')}",
        "title": (d.get("title") or "").strip() or "Twitch-clip",
        "views": int(d.get("view_count") or 0),
        "duration": float(d.get("duration") or 0),
        "created_at": d.get("created_at", ""),
        "creator": d.get("creator_name", ""),
        "broadcaster_id": d.get("broadcaster_id", ""),
        "broadcaster_name": d.get("broadcaster_name", ""),
        "thumbnail": thumb if thumb.startswith("https://") else "",
        "game_id": str(d.get("game_id") or ""),
    }


def get_clips(db: Session, broadcaster_id: str, period: str = "week", first: int = 24,
              game_id: str | None = None) -> list[dict]:
    """Top-clips van een kanaal (broadcaster_id) of van een hele game/categorie (game_id)."""
    key = ("clips", broadcaster_id, game_id, period, first)
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]  # type: ignore[return-value]
    creds = _creds_or_raise(db)
    params: dict = {"first": max(1, min(100, first))}
    if game_id:
        params["game_id"] = game_id
    else:
        params["broadcaster_id"] = broadcaster_id
    days = PERIODS.get(period, 7)
    if days:
        now = datetime.now(timezone.utc)
        params["started_at"] = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["ended_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with _client_factory() as client:
        data = helix(creds, "clips", params, client)
    clips = sorted((_clip(d) for d in data.get("data") or []), key=lambda c: c["views"], reverse=True)
    _cache[key] = (time.time() + CACHE_SECONDS, clips)
    return clips


def search_categories(db: Session, query: str, first: int = 12) -> list[dict]:
    """Zoek games/categorieën op naam, bijv. 'GTA'."""
    query = (query or "").strip()[:100]
    if not query:
        return []
    key = ("search", query.lower())
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]  # type: ignore[return-value]
    creds = _creds_or_raise(db)
    with _client_factory() as client:
        data = helix(creds, "search/categories", {"query": query, "first": first}, client)
    out = []
    for d in data.get("data") or []:
        art = (d.get("box_art_url") or "").replace("{width}", "104").replace("{height}", "139").replace("52x72", "104x139")
        out.append({"id": str(d.get("id", "")), "name": d.get("name", ""),
                    "box_art": art if art.startswith("https://") else ""})
    _cache[key] = (time.time() + CACHE_SECONDS, out)
    return out


def get_game_name(db: Session, game_id: str) -> str | None:
    if not game_id:
        return None
    key = ("game", game_id)
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]  # type: ignore[return-value]
    try:
        creds = _creds_or_raise(db)
        with _client_factory() as client:
            data = helix(creds, "games", {"id": game_id}, client)
    except TwitchError:
        return None
    items = data.get("data") or []
    name = items[0].get("name") if items else None
    _cache[key] = (time.time() + 86400, name)
    return name


def get_clip(db: Session, slug: str) -> dict | None:
    creds = _creds_or_raise(db)
    with _client_factory() as client:
        data = helix(creds, "clips", {"id": slug}, client)
    items = data.get("data") or []
    return _clip(items[0]) if items else None


def test_creds(client_id: str, secret: str) -> tuple[str, str]:
    try:
        with _client_factory() as client:
            get_token(client_id, secret, client, force=True)
    except TwitchError as exc:
        status = {"invalid": "ongeldig", "rate_limit": "rate_limit"}.get(exc.kind, "fout")
        return status, str(exc)
    return "geldig", "Sleutel werkt."
