import math
import random

from app.estimation.calibration import Point, accuracy, apply, fit
from app.estimation.estimator import estimate, multiplier, viral_score
from app.estimation.features import compute_features


def test_viral_score_weighted_and_clamped():
    hi = {"hook_sterkte": 10, "emotie_of_verrassing": 10, "begrijpelijkheid_zonder_context": 10, "afronding": 10,
          "duur": 30, "woorden_per_seconde": 3, "vraag_in_hook": True}
    lo = {"hook_sterkte": 1, "emotie_of_verrassing": 1, "begrijpelijkheid_zonder_context": 1, "afronding": 1,
          "duur": 70, "woorden_per_seconde": 0.5}
    assert viral_score(hi) == 10.0
    assert viral_score(lo) == 1.0
    mid = {"hook_sterkte": 8, "emotie_of_verrassing": 4, "begrijpelijkheid_zonder_context": 6, "afronding": 6,
           "duur": 30, "woorden_per_seconde": 3}
    assert viral_score(mid) == round(8 * .35 + 4 * .25 + 6 * .2 + 6 * .2, 1)


def test_estimate_range():
    e = estimate(5.5, 1000)
    assert e["mid"] == 1000 and e["low"] == 300 and e["high"] == 3000
    assert multiplier(8.5) == 3.0
    assert estimate(8.5, 1000)["mid"] == 3000


def test_features():
    words = [{"start": 0.0, "end": 0.4, "word": "Waarom"}, {"start": 0.5, "end": 0.9, "word": "werkt"},
             {"start": 1.0, "end": 1.4, "word": "dit?"}, {"start": 5.0, "end": 5.4, "word": "Klaar."}]
    f = compute_features(0, 10, {"hook_sterkte": 7}, words, face_ratio=0.5)
    assert f["vraag_in_hook"] and f["gezicht_in_beeld"] and f["duur"] == 10.0
    assert f["woorden_per_seconde"] == 0.4 and f["hook_sterkte"] == 7.0


def test_calibration_too_little_data():
    cal = fit([Point(6, 1000, 500)])
    assert cal.method == "standaard"


def test_calibration_median():
    pts = [Point(5.5, 1000, v) for v in (2000, 1800, 2200, 2000)]
    cal = fit(pts)
    assert cal.method == "mediaan" and math.isclose(cal.factor, 2.0)
    mid, lo, hi = apply(cal, 5.5, 1000)
    assert math.isclose(mid, 2000) and (lo, hi) == (0.3, 3.0)


def test_calibration_regression():
    rnd = random.Random(1)
    pts = []
    for _ in range(40):
        s = rnd.uniform(2, 9)
        views = math.exp(4 + 0.5 * s + rnd.gauss(0, 0.2))
        pts.append(Point(s, 1000, int(views)))
    cal = fit(pts)
    assert cal.method == "regressie"
    assert abs(cal.b - 0.5) < 0.1 and abs(cal.a - 4) < 0.6
    assert cal.low_factor < 1 < cal.high_factor
    e = estimate(7.0, 1000, cal)
    assert abs(math.log(e["mid"]) - (4 + 3.5)) < 0.3


def test_accuracy():
    pts = [Point(5, 1000, 2000, 1000, 300, 3000), Point(5, 1000, 500, 1000, 300, 3000), Point(5, 1000, 100, 1000, 300, 3000)]
    acc = accuracy(pts)
    assert acc["n"] == 3 and acc["binnen_bereik_pct"] == 67
    assert acc["gem_factor"] > 1
