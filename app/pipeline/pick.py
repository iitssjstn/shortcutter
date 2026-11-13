"""Fragmentkeuze: via een OpenAI-compatibele LLM-API, met heuristiek als terugval."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from app.pipeline.errors import LLMError
from app.pipeline.transcribe import all_words

log = logging.getLogger(__name__)

SUBSCORE_KEYS = [
    "hook_sterkte",
    "emotie_of_verrassing",
    "begrijpelijkheid_zonder_context",
    "afronding",
]
CATEGORIES = ["verhaal", "tip", "humor", "mening", "feit", "emotie", "overig"]
CHUNK_CHARS = 14_000
SENTENCE_GAP = 0.9  # seconden stilte die als zinsgrens telt
MAX_SNAP = 6.0  # maximaal zoveel seconden verschuiven om op een zinsgrens te landen

CLAIM_WORDS = re.compile(
    r"\b(nooit|altijd|niemand|iedereen|geheim|waarom|hoe|wat als|beste|slechtste|grootste|"
    r"fout|eerlijk|never|always|nobody|everyone|secret|why|how|best|worst|biggest|mistake|"
    r"truth|niemand weet|stop)\b",
    re.I,
)


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key: str
    fallbacks: tuple[str, ...] = ()  # volgende modellen als het eerste faalt

    @property
    def models(self) -> list[str]:
        return [self.model, *[m for m in self.fallbacks if m and m != self.model]]


# ---------------------------------------------------------------- zinsgrenzen
def sentences(words: list[dict]) -> list[tuple[float, float]]:
    """Splits woorden in zinnen: eindigt op . ! ? … of na een pauze."""
    out: list[tuple[float, float]] = []
    if not words:
        return out
    s_start = words[0]["start"]
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends = w["word"].rstrip("\"')»”").endswith((".", "!", "?", "…"))
        gap = (nxt["start"] - w["end"]) if nxt else 999
        if ends or gap >= SENTENCE_GAP or nxt is None:
            out.append((s_start, w["end"]))
            if nxt:
                s_start = nxt["start"]
    return out


def snap_clip(
    start: float,
    end: float,
    sents: list[tuple[float, float]],
    min_dur: float,
    max_dur: float,
    total: float | None = None,
) -> tuple[float, float] | None:
    """Snap start/eind op zinsgrenzen en dwing min/max duur af. None = onbruikbaar."""
    if total:
        start, end = max(0.0, start), min(end, total)
    if end <= start:
        return None
    if not sents:
        s, e = start, min(end, start + max_dur)
        return (round(s, 2), round(e, 2)) if e - s >= min_dur else None

    starts = [s for s, _ in sents]
    ends = [e for _, e in sents]
    # start: dichtstbijzijnde zinsbegin
    best_s = min(starts, key=lambda s: abs(s - start))
    if abs(best_s - start) <= MAX_SNAP:
        start = best_s
    # eind: dichtstbijzijnde zinseinde na de start
    cand_ends = [e for e in ends if e > start]
    if not cand_ends:
        return None
    best_e = min(cand_ends, key=lambda e: abs(e - end))
    if abs(best_e - end) <= MAX_SNAP:
        end = best_e

    dur = end - start
    if dur > max_dur:
        fitting = [e for e in cand_ends if start + min_dur <= e <= start + max_dur]
        end = max(fitting) if fitting else start + max_dur
    elif dur < min_dur:
        longer = [e for e in cand_ends if start + min_dur <= e <= start + max_dur]
        if longer:
            end = min(longer)
        else:
            # verleng naar achteren toe (eerdere zinsbegin)
            earlier = [s for s in starts if end - max_dur <= s <= end - min_dur]
            if earlier:
                start = max(earlier)
            elif total and total - start >= min_dur:
                end = min(start + min_dur, total)
            else:
                return None
    if end - start < min_dur - 0.01 or end - start > max_dur + 0.01:
        return None
    return round(start, 2), round(end, 2)


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def dedupe(clips: list[dict], min_overlap: float = 0.5) -> list[dict]:
    """Houd per overlappende groep alleen de clip met de hoogste score."""
    kept: list[dict] = []
    for c in sorted(clips, key=lambda c: c.get("_score", 0), reverse=True):
        if all(overlap((c["start"], c["end"]), (k["start"], k["end"])) < min_overlap for k in kept):
            kept.append(c)
    return kept


def _subscore_mean(sub: dict) -> float:
    vals = [float(sub.get(k, 5)) for k in SUBSCORE_KEYS]
    return sum(vals) / len(vals)


# ---------------------------------------------------------------- heuristiek
def heuristic_pick(transcript: dict, n: int, min_dur: float, max_dur: float) -> list[dict]:
    """Terugval zonder LLM: kies vensters met de hoogste spreekdichtheid."""
    words = all_words(transcript)
    sents = sentences(words)
    total = float(transcript.get("duration") or (words[-1]["end"] if words else 0))
    target = (min_dur + max_dur) / 2
    cands = []
    for i, (s, _) in enumerate(sents):
        e = s
        j = i
        while j < len(sents) and sents[j][1] - s <= max_dur:
            e = sents[j][1]
            if e - s >= target:
                break
            j += 1
        snapped = snap_clip(s, e, sents, min_dur, max_dur, total or None)
        if not snapped:
            continue
        cs, ce = snapped
        ws = [w for w in words if cs <= w["start"] < ce]
        if not ws:
            continue
        wps = len(ws) / (ce - cs)
        text = " ".join(w["word"] for w in ws)
        first = " ".join(w["word"] for w in ws if w["start"] < cs + 3)
        q = "?" in first
        claim = bool(CLAIM_WORDS.search(first))
        excl = text.count("!")
        hook = 4 + (3 if q else 0) + (2 if claim else 0)
        emotion = min(10, 4 + excl * 2)
        clarity = 6
        complete = 7 if ce in [x[1] for x in sents] else 5
        density = min(10.0, wps * 2.2)
        sub = {
            "hook_sterkte": min(10, hook),
            "emotie_of_verrassing": emotion,
            "begrijpelijkheid_zonder_context": clarity,
            "afronding": complete,
        }
        tw = []
        for w in ws[:10]:
            tw.append(w["word"])
            if w["word"].endswith((".", "!", "?")):
                break
        title = " ".join(tw).strip(" ,.") or "Fragment"
        cands.append(
            {
                "start": cs,
                "end": ce,
                "title": title[:80],
                "hook": first[:200],
                "reason": f"Gekozen op spreekdichtheid ({wps:.1f} woorden/s), zonder LLM.",
                "description": "",
                "hashtags": "#shorts",
                "category": "overig",
                "subscores": sub,
                "source": "heuristiek",
                "_score": density + _subscore_mean(sub) / 2,
            }
        )
    picked = dedupe(cands)[:n]
    return sorted(picked, key=lambda c: c["start"])


# ---------------------------------------------------------------- LLM
def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def extra_headers(base_url: str) -> dict:
    """OpenRouter vraagt optioneel om een app-naam; andere providers negeren dit."""
    if "openrouter.ai" in (base_url or ""):
        return {"X-Title": "Shortcutter"}
    return {}


def parse_json_content(content: str) -> dict:
    """Parse het JSON-antwoord, ook als het model er ```json-fences of tekst omheen zet."""
    text = (content or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        return json.loads(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("geen JSON in het antwoord")


def _chat_once(cfg: LLMConfig, model: str, messages, client, retries, sleep) -> tuple[dict, str]:
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json", **extra_headers(cfg.base_url)}
    last = "onbekende fout"
    for attempt in range(retries):
        try:
            r = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            last = f"verbindingsfout ({type(exc).__name__})"
            sleep(min(30, 2 ** (attempt + 1)))
            continue
        if r.status_code in (401, 403):
            raise LLMError("De LLM-API weigert de sleutel (ongeldig of geen toegang).", "invalid")
        if r.status_code == 402:
            raise LLMError("Onvoldoende tegoed bij de LLM-provider.", "no_credit")
        if r.status_code == 429 or r.status_code >= 500:
            last = "rate limit (429)" if r.status_code == 429 else f"serverfout ({r.status_code})"
            wait = 2 ** (attempt + 1)
            try:
                wait = max(wait, float(r.headers.get("retry-after", 0)))
            except ValueError:
                pass
            sleep(min(30, wait))
            continue
        if r.status_code >= 400:
            raise LLMError(f"Model '{model}' gaf fout {r.status_code} (bestaat het model wel?).", "model")
        try:
            body = r.json()
            content = body["choices"][0]["message"]["content"]
            return parse_json_content(content), str(body.get("model") or model)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            last = "onleesbaar antwoord"
            continue
    raise LLMError(f"Model '{model}' niet bereikbaar na {retries} pogingen: {last}.",
                   "rate_limit" if "429" in last else "error")


def chat_json(
    cfg: LLMConfig,
    messages: list[dict],
    *,
    client: httpx.Client | None = None,
    retries: int = 4,
    sleep: Callable[[float], None] = _sleep,
    timeout: float = 120.0,
    meta: dict | None = None,
) -> dict:
    """Chat-completion met response_format=json_object, retries bij 429/5xx en terugvalmodellen.

    Faalt een model (fout, overbelast, onleesbaar antwoord), dan wordt het volgende model uit
    cfg.fallbacks geprobeerd. Een ongeldige sleutel of geen tegoed stopt meteen.
    `meta['model']` krijgt het model dat echt antwoordde (zoals de provider het teruggeeft).
    """
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    last_exc: LLMError | None = None
    try:
        for model in cfg.models:
            try:
                data, used = _chat_once(cfg, model, messages, client, retries, sleep)
            except LLMError as exc:
                if exc.kind in ("invalid", "no_credit"):
                    raise
                log.warning("LLM-model %s faalde (%s); volgende model proberen", model, exc.kind)
                last_exc = exc
                continue
            if meta is not None:
                meta["model"] = used
            return data
    finally:
        if own:
            client.close()
    if last_exc and len(cfg.models) > 1:
        raise LLMError(f"Alle modellen faalden ({', '.join(cfg.models)}). Laatste fout: {last_exc}", last_exc.kind)
    raise last_exc or LLMError("Geen model ingesteld.", "error")


def _chunks(transcript: dict) -> list[str]:
    lines = [
        f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}" for seg in transcript.get("segments", [])
    ]
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > CHUNK_CHARS:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks


SYSTEM_PROMPT = (
    "Je bent een ervaren editor van korte verticale video's (YouTube Shorts, TikTok, Reels). "
    "Je kiest uit een transcript de fragmenten die als losse short het best werken: een sterke "
    "hook in de eerste seconden, begrijpelijk zonder context, en met een afgerond einde. "
    "Antwoord uitsluitend met JSON."
)


def _user_prompt(chunk: str, n: int, min_dur: float, max_dur: float, language: str = "en") -> str:
    from app.pipeline.copywriting import copy_rules

    return (
        f"Kies maximaal {n} fragmenten van {min_dur:.0f} tot {max_dur:.0f} seconden uit dit "
        "transcript (tijden in seconden tussen haken). Geef JSON in de vorm:\n"
        '{"clips": [{"start": 12.3, "end": 45.6, "title": "posttitel", '
        '"description": "beschrijving bij de post", "hashtags": ["#voorbeeld", "#shorts"], '
        '"hook": "de openingszin", "reason": "waarom dit werkt", '
        f'"category": "een van {"/".join(CATEGORIES)}", '
        '"hook_sterkte": 1-10, "emotie_of_verrassing": 1-10, '
        '"begrijpelijkheid_zonder_context": 1-10, "afronding": 1-10}]}\n'
        "Wees streng met de scores. " + copy_rules(language) + "\n\n"
        f"TRANSCRIPT:\n{chunk}"
    )


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def llm_pick(
    transcript: dict,
    n: int,
    min_dur: float,
    max_dur: float,
    cfg: LLMConfig,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = _sleep,
    post_language: str = "en",
    meta: dict | None = None,
) -> list[dict]:
    from app.pipeline.copywriting import clean_description, clean_title, normalize_hashtags

    words = all_words(transcript)
    sents = sentences(words)
    total = float(transcript.get("duration") or (words[-1]["end"] if words else 0)) or None
    chunks = _chunks(transcript)
    if not chunks:
        return []
    per_chunk = max(2, math.ceil(n * 1.5 / len(chunks)))
    cands: list[dict] = []
    for chunk in chunks:
        data = chat_json(
            cfg,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(chunk, per_chunk, min_dur, max_dur, post_language)},
            ],
            client=client,
            sleep=sleep,
            meta=meta,
        )
        items = data.get("clips") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            snapped = snap_clip(_num(it.get("start"), -1), _num(it.get("end"), -1), sents, min_dur, max_dur, total)
            if not snapped:
                continue
            sub = {k: int(min(10, max(1, round(_num(it.get(k), 5))))) for k in SUBSCORE_KEYS}
            cat = str(it.get("category", "overig")).lower().strip()
            cands.append(
                {
                    "start": snapped[0],
                    "end": snapped[1],
                    "title": clean_title(it.get("title"), "Clip"),
                    "description": clean_description(it.get("description")),
                    "hashtags": normalize_hashtags(it.get("hashtags")),
                    "hook": str(it.get("hook") or "")[:300],
                    "reason": str(it.get("reason") or "")[:500],
                    "category": cat if cat in CATEGORIES else "overig",
                    "subscores": sub,
                    "source": "llm",
                    "_score": _subscore_mean(sub),
                }
            )
    picked = dedupe(cands)[:n]
    return sorted(picked, key=lambda c: c["start"])


def pick_clips(
    transcript: dict,
    n: int,
    min_dur: float,
    max_dur: float,
    cfg: LLMConfig | None,
    **kw,
) -> tuple[list[dict], str, str | None]:
    """Geeft (clips, gebruikte_methode, waarschuwing) terug. Faalt nooit door de LLM."""
    warning = None
    if cfg is None:
        warning = "Geen LLM-sleutel ingesteld: fragmenten zijn gekozen met de heuristiek (spreekdichtheid)."
    else:
        try:
            clips = llm_pick(transcript, n, min_dur, max_dur, cfg, **kw)
            if clips:
                return clips, "llm", None
            warning = "De LLM gaf geen bruikbare fragmenten: teruggevallen op de heuristiek (spreekdichtheid)."
        except LLMError as exc:
            warning = f"LLM-clipkeuze mislukt ({exc}); teruggevallen op de heuristiek (spreekdichtheid)."
            log.warning("LLM-clipkeuze mislukt: %s", exc.kind)
    return heuristic_pick(transcript, n, min_dur, max_dur), "heuristiek", warning
