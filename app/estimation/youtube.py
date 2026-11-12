"""Kanaalgemiddelde ophalen via de YouTube Data API (optioneel)."""
from __future__ import annotations

import re
import statistics

import httpx

API = "https://www.googleapis.com/youtube/v3"
SHORT_MAX_SECONDS = 180


class ChannelError(Exception):
    pass


def _iso_seconds(d: str) -> int:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    days, h, mi, s = (int(x or 0) for x in m.groups())
    return days * 86400 + h * 3600 + mi * 60 + s


def _check(r: httpx.Response) -> dict:
    if r.status_code == 403 and "quota" in r.text.lower():
        raise ChannelError("YouTube Data API: quotum bereikt (rate limit). Probeer het morgen opnieuw.")
    if r.status_code in (400, 403):
        raise ChannelError("YouTube Data API weigert de sleutel (ongeldig of API niet ingeschakeld).")
    if r.status_code >= 400:
        raise ChannelError(f"YouTube Data API gaf fout {r.status_code}.")
    return r.json()


def fetch_channel_average(channel: str, key: str, client: httpx.Client | None = None) -> tuple[int, int]:
    """Mediaan van de views van de recente shorts (<= 3 min) van een kanaal.

    `channel` is een kanaal-ID (UC…) of een handle (@naam).
    """
    if not channel:
        raise ChannelError("Vul een kanaal-ID (UC…) of handle (@naam) in.")
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        client.headers["X-Goog-Api-Key"] = key  # sleutel in een header, nooit in de URL
        params = {"part": "contentDetails"}
        if channel.startswith("UC"):
            params["id"] = channel
        else:
            params["forHandle"] = channel if channel.startswith("@") else "@" + channel
        data = _check(client.get(f"{API}/channels", params=params))
        items = data.get("items") or []
        if not items:
            raise ChannelError("Kanaal niet gevonden.")
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        data = _check(client.get(f"{API}/playlistItems", params={"part": "contentDetails", "playlistId": uploads, "maxResults": 50}))
        ids = [it["contentDetails"]["videoId"] for it in data.get("items", [])]
        if not ids:
            raise ChannelError("Geen video's gevonden op dit kanaal.")
        data = _check(client.get(f"{API}/videos", params={"part": "contentDetails,statistics", "id": ",".join(ids)}))
        views = [
            int(v.get("statistics", {}).get("viewCount", 0))
            for v in data.get("items", [])
            if _iso_seconds(v.get("contentDetails", {}).get("duration", "")) <= SHORT_MAX_SECONDS
        ]
        if not views:
            raise ChannelError("Geen recente shorts gevonden op dit kanaal.")
        return int(statistics.median(views)), len(views)
    finally:
        if own:
            client.close()
