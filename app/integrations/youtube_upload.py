"""Uploaden naar YouTube via de YouTube Data API (OAuth per gebruiker).

* De beheerder zet één Google OAuth-client (client-ID + secret) in API-sleutels (provider 'google_oauth').
* Elke gebruiker koppelt zijn eigen kanaal; het refresh-token staat versleuteld in de database.
* Upload gaat 'resumable': eerst de metadata, daarna het bestand in één stream.

Let op (beperkingen van Google): uploads vanuit een niet-geaudit API-project worden door YouTube
privé gezet, en elke upload kost ±1600 van de 10.000 quota-eenheden per dag.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import get_settings
from app.core.models import Credential, YoutubeAccount, utcnow

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
PRIVACY = {"private": "Privé", "unlisted": "Verborgen (unlisted)", "public": "Openbaar"}
TITLE_MAX = 100
DESC_MAX = 5000


def _client_factory() -> httpx.Client:  # vervangbaar in tests
    return httpx.Client(timeout=httpx.Timeout(60.0, write=600.0))


class YoutubeError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind  # error | no_client | not_connected | auth | quota


# ---------------------------------------------------------------- configuratie
def oauth_credential(db: Session) -> Credential | None:
    return db.scalar(select(Credential).where(Credential.provider == "google_oauth"))


def oauth_client(db: Session) -> tuple[str, str] | None:
    cred = oauth_credential(db)
    if cred is None:
        return None
    try:
        data = json.loads(crypto.decrypt_str(cred.encrypted_value))
    except (RuntimeError, ValueError):
        return None
    return data.get("client_id", ""), data.get("client_secret", "")


def redirect_uri(base_url: str) -> str:
    public = get_settings().public_url
    return (public or base_url).rstrip("/") + "/youtube/callback"


def auth_url(client_id: str, redirect: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })


# ---------------------------------------------------------------- tokens
def _token_request(data: dict, client: httpx.Client) -> dict:
    try:
        r = client.post(TOKEN_URL, data=data)
    except httpx.HTTPError as exc:
        raise YoutubeError(f"Google is niet bereikbaar ({type(exc).__name__}).") from exc
    if r.status_code in (400, 401):
        raise YoutubeError("Google weigert de koppeling (verlopen of ingetrokken). Koppel je kanaal opnieuw.", "auth")
    if r.status_code != 200:
        raise YoutubeError(f"Onverwacht antwoord van Google (HTTP {r.status_code}).")
    return r.json()


def exchange_code(db: Session, code: str, redirect: str) -> dict:
    client_cfg = oauth_client(db)
    if not client_cfg:
        raise YoutubeError("De beheerder heeft nog geen Google-koppeling ingesteld.", "no_client")
    with _client_factory() as client:
        return _token_request({"code": code, "client_id": client_cfg[0], "client_secret": client_cfg[1],
                               "redirect_uri": redirect, "grant_type": "authorization_code"}, client)


def fetch_channel(access_token: str, client: httpx.Client) -> dict | None:
    r = client.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                   headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code != 200:
        return None
    items = r.json().get("items") or []
    return items[0] if items else None


def connect(db: Session, user_id: int, tokens: dict) -> YoutubeAccount:
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise YoutubeError("Google gaf geen refresh-token terug. Probeer opnieuw te koppelen.", "auth")
    with _client_factory() as client:
        ch = fetch_channel(tokens.get("access_token", ""), client)
    if ch is None:
        raise YoutubeError("Er is geen YouTube-kanaal gevonden bij dit Google-account.", "auth")
    acc = db.scalar(select(YoutubeAccount).where(YoutubeAccount.user_id == user_id))
    if acc is None:
        acc = YoutubeAccount(user_id=user_id)
        db.add(acc)
    acc.channel_id = ch.get("id", "")
    acc.channel_title = (ch.get("snippet") or {}).get("title", "")[:200]
    acc.refresh_token_enc = crypto.encrypt(refresh)
    acc.access_token_enc = crypto.encrypt(tokens.get("access_token", ""))
    acc.expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)) - 60)
    acc.connected_at = utcnow()
    db.commit()
    return acc


def access_token(db: Session, acc: YoutubeAccount, client: httpx.Client) -> str:
    if acc.access_token_enc and acc.expires_at and acc.expires_at > utcnow():
        return crypto.decrypt_str(acc.access_token_enc)
    client_cfg = oauth_client(db)
    if not client_cfg:
        raise YoutubeError("De beheerder heeft nog geen Google-koppeling ingesteld.", "no_client")
    data = _token_request({"refresh_token": crypto.decrypt_str(acc.refresh_token_enc), "client_id": client_cfg[0],
                           "client_secret": client_cfg[1], "grant_type": "refresh_token"}, client)
    acc.access_token_enc = crypto.encrypt(data["access_token"])
    acc.expires_at = utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)) - 60)
    db.commit()
    return data["access_token"]


def disconnect(db: Session, user_id: int) -> None:
    acc = db.scalar(select(YoutubeAccount).where(YoutubeAccount.user_id == user_id))
    if acc is None:
        return
    try:
        with _client_factory() as client:
            client.post(REVOKE_URL, data={"token": crypto.decrypt_str(acc.refresh_token_enc)})
    except Exception:  # noqa: BLE001 - intrekken is netjes, maar niet noodzakelijk
        pass
    db.delete(acc)
    db.commit()


# ---------------------------------------------------------------- upload
def clean_text(text: str, limit: int) -> str:
    # YouTube weigert < en > in titel en beschrijving
    return (text or "").replace("<", "‹").replace(">", "›").strip()[:limit]


def to_rfc3339(dt_local: datetime) -> str:
    return dt_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _api_error(r: httpx.Response) -> YoutubeError:
    try:
        reason = (r.json().get("error", {}).get("errors") or [{}])[0].get("reason", "")
    except ValueError:
        reason = ""
    if reason in ("quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "rateLimitExceeded"):
        return YoutubeError("Het dagquotum of uploadlimiet van YouTube is bereikt. Probeer het morgen opnieuw.", "quota")
    if r.status_code == 401:
        return YoutubeError("De koppeling met YouTube is verlopen. Koppel je kanaal opnieuw bij Mijn account.", "auth")
    if r.status_code == 403:
        return YoutubeError(f"YouTube weigert de upload ({reason or 'geen toegang'}).", "auth")
    return YoutubeError(f"YouTube gaf fout {r.status_code}{f' ({reason})' if reason else ''}.")


def upload(
    db: Session,
    acc: YoutubeAccount,
    path: Path,
    *,
    title: str,
    description: str,
    privacy: str = "private",
    publish_at: str | None = None,
    made_for_kids: bool = False,
    category_id: str = "22",
    tags: list[str] | None = None,
) -> dict:
    """Upload een video. Geeft {'id', 'url', 'privacy'} terug."""
    size = path.stat().st_size
    status: dict = {"privacyStatus": "private" if publish_at else privacy, "selfDeclaredMadeForKids": made_for_kids}
    if publish_at:
        status["publishAt"] = publish_at
    body = {
        "snippet": {"title": clean_text(title, TITLE_MAX) or "Short", "description": clean_text(description, DESC_MAX),
                    "categoryId": category_id, "tags": (tags or [])[:15]},
        "status": status,
    }
    with _client_factory() as client:
        token = access_token(db, acc, client)
        try:
            r = client.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                                     "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                            content=json.dumps(body).encode())
        except httpx.HTTPError as exc:
            raise YoutubeError(f"YouTube is niet bereikbaar ({type(exc).__name__}).") from exc
        if r.status_code != 200 or "location" not in r.headers:
            raise _api_error(r)
        session_url = r.headers["location"]
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with path.open("rb") as fh:
                    r = client.put(session_url, content=fh, headers={"Content-Type": "video/mp4",
                                                                      "Content-Length": str(size)})
                break
            except httpx.HTTPError as exc:  # verbinding weg: opnieuw proberen
                last_exc = exc
                time.sleep(2 * (attempt + 1))
        else:
            raise YoutubeError(f"Upload afgebroken ({type(last_exc).__name__}).")
        if r.status_code not in (200, 201):
            raise _api_error(r)
        data = r.json()
    vid = data.get("id")
    if not vid:
        raise YoutubeError("YouTube gaf geen video-ID terug.")
    acc.last_upload_at = utcnow()
    db.commit()
    return {"id": vid, "url": f"https://youtube.com/shorts/{vid}",
            "privacy": (data.get("status") or {}).get("privacyStatus", status["privacyStatus"])}
