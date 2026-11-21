"""Familievriendelijke modus: scheldwoorden wegpiepen en ongeschikte beelden vervagen.

* Geluid: woorden uit een vaste lijst (Engels en Nederlands) worden met de woordtijden uit het
  transcript vervangen door een piep; in de ondertitels worden ze gemaskeerd (f***).
* Beeld: een AI-model met beeldherkenning bekijkt ongeveer elke seconde een frame op naaktheid,
  seksuele inhoud en gore (ook in games). Die momenten worden vervaagd.

Dit verkleint de kans op een leeftijdsgrens of beperkte advertenties, maar YouTube beslist zelf.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.pipeline.errors import LLMError
from app.pipeline.render import X264, _run

log = logging.getLogger(__name__)

# stammen; een woord telt als het (zonder leestekens) met een van deze begint of er gelijk aan is
_PREFIX = [
    "fuck", "motherfuck", "shit", "bullshit", "bitch", "cunt", "dick", "pussy", "asshole", "bastard", "whore", "slut",
    "nigg", "fagg", "retard", "cock", "wank", "twat",
    "kanker", "tering", "tyfus", "klootzak", "kut", "hoer", "lul", "mongool", "flikker", "godver", "kolere", "pokke",
]
_EXACT = {"fag", "fk", "fkn", "wtf", "stfu", "damn", "goddamn", "piss", "pissed", "tits", "titties", "shitty"}
_ALLOW = {"cockpit", "cocktail", "cockroach", "dickens", "hoera", "hoeray", "scunthorpe"}  # onschuldige woorden
_WORD = re.compile(r"[^\w']+", re.UNICODE)
FLAG_REASONS = {"naakt": "naakt", "seksueel": "seksueel", "gore": "bloed/gore"}
FRAME_STEP = 1.0
MAX_FRAMES = 45
BATCH = 15


def _norm(word: str) -> str:
    return _WORD.sub("", (word or "").lower()).strip("'")


def is_profane(word: str) -> bool:
    w = _norm(word)
    if not w or w in _ALLOW:
        return False
    return w in _EXACT or any(w.startswith(p) for p in _PREFIX)


def mask(word: str) -> str:
    """'fucking!' -> 'f******!' (leestekens blijven staan)."""
    return re.sub(r"(\w)([\w']*)", lambda m: m[1] + "*" * len(m[2]), word or "")


def censor_words(words: list[dict]) -> list[dict]:
    return [{**w, "word": mask(w["word"])} if is_profane(w.get("word", "")) else w for w in words]


def mute_intervals(words: list[dict], clip_start: float, clip_end: float,
                   teaser: tuple[float, float] | None = None) -> list[tuple[float, float]]:
    """Uitvoertijden (s) waarin gepiept moet worden; houdt rekening met een teaser vooraan."""
    t_len = (teaser[1] - teaser[0]) if teaser else 0.0
    out = []
    for w in words:
        if not is_profane(w.get("word", "")) or w["end"] <= clip_start or w["start"] >= clip_end:
            continue
        a, b = max(w["start"], clip_start) - clip_start, min(w["end"], clip_end) - clip_start
        a, b = max(0.0, a - 0.05), b + 0.05
        out.append((a + t_len, b + t_len))
        if teaser and b > teaser[0] and a < teaser[1]:
            out.append((max(0.0, a - teaser[0]), min(t_len, b - teaser[0])))
    return merge(out)


def merge(iv: list[tuple[float, float]], gap: float = 0.1) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def _enable(iv: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in iv)


def censor(raw: Path, out: Path, mute: list[tuple[float, float]], blur: list[tuple[float, float]],
           has_audio: bool) -> Path:
    """Piep over `mute` en vervaag het beeld tijdens `blur` (uitvoertijden). Geeft `out` terug."""
    args = ["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", str(raw.resolve())]
    fc, maps = [], []
    if blur:
        fc.append(f"[0:v]boxblur=luma_radius=40:luma_power=3:enable='{_enable(blur)}'[v]")
        maps += ["-map", "[v]", *X264]
    else:
        maps += ["-map", "0:v", "-c:v", "copy"]
    if has_audio and mute:
        e = _enable(mute)
        fc.append(f"[0:a]volume=0:enable='{e}'[m];"
                  f"sine=frequency=1000:sample_rate=48000,volume=0.18,volume=0:enable='not({e})'[b];"
                  "[m][b]amix=inputs=2:duration=first:normalize=0[a]")
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    elif has_audio:
        maps += ["-map", "0:a", "-c:a", "copy"]
    if fc:
        args += ["-filter_complex", ";".join(fc)]
    _run(args + maps + ["-movflags", "+faststart", str(out.resolve())])
    return out


def check_frames(cfg, video: Path, duration: float, client=None, sleep=None) -> list[tuple[float, str]]:
    """Laat een AI-model met beeldherkenning frames beoordelen. Geeft [(tijd, reden)] terug."""
    import base64
    import subprocess

    from app.pipeline import pick

    n = max(1, min(MAX_FRAMES, int(duration / FRAME_STEP) + 1))
    times = [round(min(duration - 0.05, i * duration / n + duration / (2 * n)), 2) for i in range(n)]
    frames: list[tuple[float, str]] = []
    for t in times:
        res = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(video),
                              "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6", "-f", "image2", "-c:v", "mjpeg", "-"],
                             capture_output=True, timeout=60)
        if res.returncode == 0 and res.stdout:
            frames.append((t, base64.b64encode(res.stdout).decode()))
    flagged: list[tuple[float, str]] = []
    for i in range(0, len(frames), BATCH):
        batch = frames[i: i + BATCH]
        content = [{"type": "text", "text": (
            "Beoordeel elk beeld uit een (game)video voor een YouTube-publiek van alle leeftijden. Markeer ALLEEN: "
            "'naakt' (zichtbare blote borsten/geslachtsdelen/billen, ook geanimeerd of in een game), 'seksueel' "
            "(seksuele handelingen of duidelijk seksuele poses, bijv. paaldansen/lapdance), 'gore' (veel bloed, "
            "verminking, lichaamsdelen). Gewone game-actie, wapens, schieten, auto's, explosies en badkleding op het "
            "strand zijn 'geen'. Twijfel = 'geen'. Antwoord met JSON: "
            '{"frames": [{"i": 0, "probleem": "geen|naakt|seksueel|gore"}]}')}]
        for k, (_, b64) in enumerate(batch):
            content.append({"type": "text", "text": f"Beeld {k}:"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        kw = {"client": client}
        if sleep is not None:
            kw["sleep"] = sleep
        try:
            data = pick.chat_json(cfg, [{"role": "system", "content": "Je bent een zorgvuldige contentmoderator. "
                                                                       "Antwoord uitsluitend met JSON."},
                                        {"role": "user", "content": content}], retries=2, timeout=90.0, **kw)
        except LLMError as exc:
            log.warning("Beeldcontrole mislukt: %s", exc)
            raise
        for row in (data.get("frames") if isinstance(data, dict) else None) or []:
            try:
                k = int(row.get("i"))
            except (TypeError, ValueError, AttributeError):
                continue
            reason = str(row.get("probleem", "geen")).lower().strip()
            if 0 <= k < len(batch) and reason in FLAG_REASONS:
                flagged.append((batch[k][0], reason))
    return flagged


def blur_intervals(flags: list[tuple[float, str]], duration: float, pad: float = 0.8) -> list[tuple[float, float]]:
    return merge([(max(0.0, t - pad), min(duration, t + pad)) for t, _ in flags], gap=0.3)
