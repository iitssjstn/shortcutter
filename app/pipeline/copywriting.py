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
    insights: str = "",
) -> dict[int, dict]:
    """items: [{'id', 'text', 'title', optioneel 'before', 'context', 'frames', 'title_unreliable', 'duration'}].

    Geeft {id: {'title','description','hashtags'}} terug. Met 'frames' (base64-JPEG) krijgt het model ook
    beelden te zien; kan het model geen beelden lezen, dan wordt het automatisch zonder beelden opnieuw geprobeerd.
    """
    from app.pipeline import pick

    if not items:
        return {}
    system = ("Je bent een social-media-copywriter voor YouTube Shorts, TikTok en Reels. Je baseert je op wat er in de "
              "clip echt gebeurt: wat je ziet op de beelden en wat er gezegd wordt. Bedenk niets wat niet te zien of te "
              "horen is. Antwoord uitsluitend met JSON.")
    intro = ("Schrijf voor elke clip hieronder een posttekst. " + copy_rules(language) + "\n"
             "Gebruik vooral de beelden en de gesproken tekst. Titels die als ONBETROUWBAAR gemarkeerd zijn, zijn door "
             "kijkers bedacht en vaak willekeurig: neem ze niet over. Noem de game alleen als die bekend is.\n"
             'Geef JSON: {"clips": [{"id": 1, "title": "...", "description": "...", "hashtags": ["#a", "#b"]}]}')
    if insights:
        intro = ("ERVARING VAN DIT KANAAL (echte resultaten; schrijf titels en beschrijvingen in de stijl van wat "
                 "goed werkte en vermijd wat slecht werkte):\n" + insights + "\n\n" + intro)

    def block(it: dict) -> str:
        title_label = "werktitel (onbetrouwbaar)" if it.get("title_unreliable") else "werktitel"
        lines = [f"CLIP {it['id']}" + (f" ({it['duration']} s)" if it.get("duration") else "")]
        if it.get("context"):
            lines.append(f"Context: {it['context']}")
        lines.append(f"{title_label}: {it.get('title') or '-'}")
        if it.get("before"):
            lines.append(f"Vlak ervoor gezegd: {it['before'][:600]}")
        lines.append(f"Gezegd in de clip: {it['text'][:2500]}")
        if it.get("frames"):
            lines.append(f"Hieronder {len(it['frames'])} beelden uit deze clip (begin, midden, eind).")
        return "\n".join(lines)

    def build(with_frames: bool) -> list[dict]:
        if not with_frames:
            text = intro + "\n\n" + "\n\n".join(block({**it, "frames": []}) for it in items)
            return [{"role": "system", "content": system}, {"role": "user", "content": text}]
        content: list[dict] = [{"type": "text", "text": intro}]
        for it in items:
            content.append({"type": "text", "text": "\n\n" + block(it)})
            for f in it.get("frames") or []:
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{f}"}})
        return [{"role": "system", "content": system}, {"role": "user", "content": content}]

    has_frames = any(it.get("frames") for it in items)
    messages = build(has_frames)
    kw = {"client": client, "meta": meta}
    if sleep is not None:
        kw["sleep"] = sleep
    try:
        data = pick.chat_json(cfg, messages, **kw)
    except LLMError as exc:
        if not has_frames or exc.kind in ("invalid", "no_credit"):
            raise
        # model kan (waarschijnlijk) geen beelden lezen: zonder beelden opnieuw
        data = pick.chat_json(cfg, build(False), **kw)
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
