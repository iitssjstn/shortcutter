"""Posttekst per clip (titel, beschrijving, hashtags), klaar om te kopiëren."""
from __future__ import annotations

import re
from typing import Callable

import httpx

from app.pipeline.errors import LLMError
from app.pipeline.pick import LLMConfig

TITLE_MAX = 100  # YouTube-limiet; korter is beter (±40-70 tekens zichtbaar)
MAX_TAGS = 6

POST_LANGUAGES = {
    "en": "Engels (meeste bereik)",
    "nl": "Nederlands",
    "auto": "Taal van de video",
}
_LANG_RULE = {
    "en": "Schrijf titel, beschrijving en hashtags in het ENGELS, ook als het transcript in een andere taal is.",
    "nl": "Schrijf titel, beschrijving en hashtags in het NEDERLANDS.",
    "auto": "Schrijf titel, beschrijving en hashtags in de taal van het transcript.",
}


def copy_rules(lang: str = "en") -> str:
    return (
        "Regels voor de posttekst: "
        "TITEL: pakkend, maximaal 70 tekens, maakt nieuwsgierig zonder te liegen, geen aanhalingstekens, "
        "hooguit één emoji als het echt past. "
        "BESCHRIJVING: 1 à 2 korte zinnen die de kijker laten blijven kijken of reageren (bijv. een vraag). "
        "HASHTAGS: 3 tot 5 relevante hashtags, inclusief #shorts. "
        + _LANG_RULE.get(lang, _LANG_RULE["en"])
        + " De hook is een letterlijk citaat in de originele taal; de reden schrijf je in het Nederlands."
    )


COPY_RULES = copy_rules("en")


def normalize_hashtags(raw) -> str:
    if isinstance(raw, str):
        items = re.split(r"[\s,]+", raw)
    elif isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        items = []
    tags: list[str] = []
    for t in items:
        t = re.sub(r"[^\w]", "", t.strip().lstrip("#"), flags=re.UNICODE)
        if t and ("#" + t).lower() not in {x.lower() for x in tags}:
            tags.append("#" + t)
    if "#shorts" not in {x.lower() for x in tags}:
        tags.append("#shorts")
    return " ".join(tags[:MAX_TAGS])


def clean_title(raw, fallback: str) -> str:
    t = str(raw or "").strip().strip("\"'“”‘’").replace("\n", " ")
    return (t or fallback)[:TITLE_MAX]


def clean_description(raw) -> str:
    return str(raw or "").strip()[:1000]


def generate_post_texts(
    cfg: LLMConfig,
    items: list[dict],
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
    language: str = "en",
    meta: dict | None = None,
) -> dict[int, dict]:
    """items: [{'id', 'text', 'title'}]. Geeft {id: {'title','description','hashtags'}} terug."""
    from app.pipeline import pick

    if not items:
        return {}
    blocks = "\n\n".join(f"CLIP {it['id']} (werktitel: {it.get('title') or '-'}):\n{it['text'][:2500]}" for it in items)
    messages = [
        {"role": "system", "content": "Je bent een social-media-copywriter voor YouTube Shorts, TikTok en Reels. "
                                      "Antwoord uitsluitend met JSON."},
        {"role": "user", "content": (
            "Schrijf voor elke clip hieronder een posttekst. " + copy_rules(language) + "\n"
            'Geef JSON: {"clips": [{"id": 1, "title": "...", "description": "...", "hashtags": ["#a", "#b"]}]}\n\n'
            + blocks
        )},
    ]
    kw = {"client": client, "meta": meta}
    if sleep is not None:
        kw["sleep"] = sleep
    data = pick.chat_json(cfg, messages, **kw)
    out: dict[int, dict] = {}
    known = {it["id"]: it for it in items}
    for row in (data.get("clips") if isinstance(data, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        try:
            cid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if cid not in known:
            continue
        out[cid] = {
            "title": clean_title(row.get("title"), known[cid].get("title") or "Clip"),
            "description": clean_description(row.get("description")),
            "hashtags": normalize_hashtags(row.get("hashtags")),
        }
    if not out:
        raise LLMError("De LLM gaf geen bruikbare posttekst terug.", "error")
    return out
