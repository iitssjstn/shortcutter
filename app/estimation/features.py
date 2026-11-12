"""Kenmerken van een clip die de view-schatting gebruikt."""
from __future__ import annotations

import re

from app.pipeline.pick import CLAIM_WORDS, SUBSCORE_KEYS

NUMBER = re.compile(r"\b\d+([.,]\d+)?\s*(%|procent|euro|dollar|k|keer|x)?\b", re.I)


def compute_features(
    start: float,
    end: float,
    subscores: dict,
    words: list[dict],
    face_ratio: float | None = None,
) -> dict:
    dur = max(0.1, end - start)
    inside = [w for w in words if start <= w["start"] < end]
    first3 = " ".join(w["word"] for w in inside if w["start"] < start + 3)
    question = "?" in first3
    claim = bool(CLAIM_WORDS.search(first3) or NUMBER.search(first3))
    return {
        **{k: float(subscores.get(k, 5)) for k in SUBSCORE_KEYS},
        "duur": round(dur, 1),
        "woorden_per_seconde": round(len(inside) / dur, 2),
        "vraag_in_hook": question,
        "claim_in_hook": claim,
        "gezicht_in_beeld": bool(face_ratio is not None and face_ratio >= 0.3),
    }
