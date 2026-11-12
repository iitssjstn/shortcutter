"""Viral score en view-schatting. Altijd een bereik: views zijn niet te voorspellen."""
from __future__ import annotations

WEIGHTS = {
    "hook_sterkte": 0.35,
    "emotie_of_verrassing": 0.25,
    "begrijpelijkheid_zonder_context": 0.20,
    "afronding": 0.20,
}
LOW_FACTOR = 0.3
HIGH_FACTOR = 3.0


def viral_score(features: dict) -> float:
    base = sum(float(features.get(k, 5)) * w for k, w in WEIGHTS.items())
    adj = 0.0
    if features.get("vraag_in_hook") or features.get("claim_in_hook"):
        adj += 0.4
    if features.get("gezicht_in_beeld"):
        adj += 0.2
    dur = float(features.get("duur", 30))
    if dur > 50:
        adj -= 0.3
    elif dur < 12:
        adj -= 0.3
    wps = float(features.get("woorden_per_seconde", 2.8))
    if wps < 1.5:
        adj -= 0.4
    elif wps > 4.5:
        adj -= 0.2
    return round(min(10.0, max(1.0, base + adj)), 1)


def multiplier(score: float) -> float:
    """Score 5.5 = kanaalgemiddelde; elke 3 punten = factor 3."""
    return 3 ** ((score - 5.5) / 3)


def estimate(score: float, baseline: float, calibration=None) -> dict:
    """Geeft {'low','mid','high','method'} terug."""
    from app.estimation.calibration import apply as apply_cal

    if calibration is not None:
        mid, lo_f, hi_f = apply_cal(calibration, score, baseline)
    else:
        mid, lo_f, hi_f = baseline * multiplier(score), LOW_FACTOR, HIGH_FACTOR
    mid = max(1.0, mid)
    return {
        "low": int(round(mid * lo_f)),
        "mid": int(round(mid)),
        "high": int(round(mid * hi_f)),
        "method": getattr(calibration, "method", "standaard") if calibration else "standaard",
    }


def fmt_views(n: float | int | None) -> str:
    if n is None:
        return "–"
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{int(n)}"
