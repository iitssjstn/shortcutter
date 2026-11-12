"""Kalibratie van de view-schatting met echte views van geposte clips.

* < 3 datapunten: geen kalibratie (standaardcurve).
* 3-19 datapunten: schaalfactor = mediaan van echt / geschat.
* >= 20 datapunten: lineaire regressie op log(views) tegen de viral score, met het
  bereik uit het 10e en 90e percentiel van de residuen.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from app.estimation.estimator import HIGH_FACTOR, LOW_FACTOR, multiplier

MIN_POINTS_MEDIAN = 3
MIN_POINTS_REGRESSION = 20


@dataclass
class Calibration:
    method: str  # standaard | mediaan | regressie
    n: int
    factor: float = 1.0
    a: float = 0.0
    b: float = 0.0
    low_factor: float = LOW_FACTOR
    high_factor: float = HIGH_FACTOR


@dataclass
class Point:
    score: float
    baseline: float
    actual: int
    est_at_post: float | None = None
    est_low_at_post: float | None = None
    est_high_at_post: float | None = None


def _quantile(vals: list[float], q: float) -> float:
    s = sorted(vals)
    if not s:
        return 0.0
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def fit(points: list[Point]) -> Calibration:
    pts = [p for p in points if p.actual is not None and p.actual >= 0]
    n = len(pts)
    if n < MIN_POINTS_MEDIAN:
        return Calibration("standaard", n)
    if n < MIN_POINTS_REGRESSION:
        ratios = [max(p.actual, 1) / max(1.0, p.baseline * multiplier(p.score)) for p in pts]
        return Calibration("mediaan", n, factor=statistics.median(ratios))
    xs = [p.score for p in pts]
    ys = [math.log(max(p.actual, 1)) for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 1e-9 else 0.0
    a = my - b * mx
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    lo = math.exp(_quantile(resid, 0.10))
    hi = math.exp(_quantile(resid, 0.90))
    return Calibration(
        "regressie", n, a=a, b=b, low_factor=min(lo, 0.95), high_factor=max(hi, 1.05)
    )


def apply(cal: Calibration, score: float, baseline: float) -> tuple[float, float, float]:
    """Geeft (midden, laag-factor, hoog-factor) terug."""
    if cal.method == "regressie":
        return math.exp(cal.a + cal.b * score), cal.low_factor, cal.high_factor
    if cal.method == "mediaan":
        return baseline * multiplier(score) * cal.factor, LOW_FACTOR, HIGH_FACTOR
    return baseline * multiplier(score), LOW_FACTOR, HIGH_FACTOR


def accuracy(points: list[Point]) -> dict:
    """Hoe goed klopten de schattingen die bij het posten getoond werden?"""
    pts = [p for p in points if p.est_at_post and p.actual is not None]
    if not pts:
        return {"n": 0}
    log_err = [abs(math.log10(max(p.actual, 1) / max(p.est_at_post, 1))) for p in pts]
    pct_err = [abs(p.actual - p.est_at_post) / max(p.est_at_post, 1) * 100 for p in pts]
    in_range = [
        1
        for p in pts
        if p.est_low_at_post is not None
        and p.est_high_at_post is not None
        and p.est_low_at_post <= p.actual <= p.est_high_at_post
    ]
    over = sum(1 for p in pts if p.actual > p.est_at_post)
    return {
        "n": len(pts),
        "gem_factor": round(10 ** statistics.fmean(log_err), 2),
        "mediaan_afwijking_pct": round(statistics.median(pct_err)),
        "binnen_bereik_pct": round(100 * len(in_range) / len(pts)),
        "vaker_meer_dan_geschat": over > len(pts) / 2,
    }
