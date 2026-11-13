"""Automatisch het kader van een facecam vinden.

Idee: gameplay verandert voortdurend, een webcambeeld (achtergrond) staat bijna stil en
het kader van de overlay heeft vaste, rechte randen. We meten over de clip heen:

* activiteit per pixel (gemiddeld verschil tussen opeenvolgende metingen),
* hoe vaak een pixel een rand is (Canny) - vaste randen scoren hoog.

Vanaf het gezicht van de hoofdpersoon zoeken we het samenhangende stilstaande gebied, nemen
daar de rechthoek van, en schuiven elke zijde naar de sterkste vaste rechte lijn in de buurt.
Is dat gebied (bijna) het hele beeld, dan is het geen facecam-overlay maar een webcam-shot.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

WORK_W = 320  # analyse op lage resolutie (snel genoeg voor een trage VPS)
SAMPLES = 24
ACTIVITY_THRESHOLD = 7.0  # grijswaarde-verschil (0-255) waaronder een pixel 'stil' is
MAX_AREA = 0.45  # groter dan dit deel van het beeld = geen overlay
Box = tuple[float, float, float, float]  # x, y, w, h als fracties van het frame


def _frames(video: Path, start: float, end: float, n: int = SAMPLES):
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    out = []
    try:
        dur = max(0.5, end - start)
        for i in range(n):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + dur * (i + 0.5) / n) * 1000)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (WORK_W, max(2, int(h * WORK_W / w))), interpolation=cv2.INTER_AREA)
            out.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    finally:
        cap.release()
    return out


def analyse(frames):
    """Geeft (activiteit, vaste-randen) als float-arrays terug."""
    import cv2
    import numpy as np

    blurred = [cv2.GaussianBlur(f, (5, 5), 0).astype(np.float32) for f in frames]
    diffs = [np.abs(b - a) for a, b in zip(blurred, blurred[1:])]
    activity = np.mean(diffs, axis=0) if diffs else np.zeros_like(blurred[0])
    edges = np.mean([cv2.Canny(f, 60, 160) > 0 for f in frames], axis=0).astype(np.float32)
    return activity, edges


def _refine_side(edges, fixed_lo: int, fixed_hi: int, pos: int, search: int, vertical: bool, limit: int) -> int:
    """Schuif een zijde naar de sterkste vaste lijn binnen `search` pixels."""
    import numpy as np

    best, best_score = pos, 0.0
    for p in range(max(0, pos - search), min(limit - 1, pos + search) + 1):
        seg = edges[fixed_lo:fixed_hi, max(0, p - 1): p + 2] if vertical else edges[max(0, p - 1): p + 2, fixed_lo:fixed_hi]
        if seg.size == 0:
            continue
        # per lijn: deel van de lijn dat vrijwel altijd rand is
        line = seg.max(axis=1 if vertical else 0)
        score = float(np.mean(line > 0.6))
        if score > best_score:
            best, best_score = p, score
    return best if best_score >= 0.5 else pos


def _walk(frac, start: int, step: int, limit: int, need: float = 0.55, patience: int = 3) -> int:
    """Loop vanaf `start` in richting `step` zolang het aandeel stil hoog blijft; geef de laatste goede index."""
    last_good = start
    bad = 0
    i = start
    while 0 <= i + step < limit:
        i += step
        if frac[i] >= need:
            last_good = i
            bad = 0
        else:
            bad += 1
            if bad >= patience:
                break
    return last_good


def find_facecam(activity, edges, face: tuple[float, float, float]) -> Box | None:
    """face = (cx, cy, breedte) als fracties. Geeft het facecam-kader of None."""
    import numpy as np

    H, W = activity.shape
    cx, cy, size = face
    fx, fy = min(W - 1, int(cx * W)), min(H - 1, int(cy * H))
    fw = max(4, int(size * W))
    fh = int(fw * 1.25)
    # drempel afhankelijk van hoeveel de rest van het beeld beweegt
    thr = min(ACTIVITY_THRESHOLD, max(1.5, 0.5 * float(np.median(activity))))
    still = (activity < thr).astype(np.float32)
    # het gezicht zelf beweegt (praten, hoofd draaien): dat stukje altijd meetellen
    still[max(0, fy - int(fh * 0.6)): fy + int(fh * 0.6), max(0, fx - int(fw * 0.6)): fx + int(fw * 0.6)] = 1

    # 1) links/rechts in een band ter hoogte van het gezicht
    band = still[max(0, fy - fh): min(H, fy + fh)]
    colfrac = band.mean(axis=0)
    left = _walk(colfrac, fx, -1, W)
    right = _walk(colfrac, fx, 1, W)
    # 2) boven/onder binnen die kolommen
    rowfrac = still[:, left: right + 1].mean(axis=1)
    top = _walk(rowfrac, fy, -1, H)
    bottom = _walk(rowfrac, fy, 1, H)
    # 3) nog een keer links/rechts over de volle hoogte van het kader
    colfrac = still[top: bottom + 1].mean(axis=0)
    left = _walk(colfrac, fx, -1, W)
    right = _walk(colfrac, fx, 1, W)

    if (right - left) * (bottom - top) > MAX_AREA * W * H:
        return None  # vrijwel het hele beeld: webcam-shot, geen overlay
    # zijden naar vaste rechte lijnen schuiven (het kader van de overlay)
    bw, bh = right - left, bottom - top
    sx, sy = max(3, bw // 8), max(3, bh // 8)
    left = _refine_side(edges, top, bottom + 1, left, sx, True, W)
    right = _refine_side(edges, top, bottom + 1, right, sx, True, W)
    top = _refine_side(edges, left, right + 1, top, sy, False, H)
    bottom = _refine_side(edges, left, right + 1, bottom, sy, False, H)
    bw, bh = right - left, bottom - top
    if bw < 1.3 * fw or bh < 1.1 * fw:  # een facecam is altijd wat groter dan het gezicht
        return None
    if not (left <= fx <= right and top <= fy <= bottom):
        return None
    if not 0.5 <= bw / max(1, bh) <= 3.0 or bw * bh > MAX_AREA * W * H:
        return None
    return (left / W, top / H, (bw + 1) / W, (bh + 1) / H)


def detect_facecam(video: Path, start: float, end: float, face: tuple[float, float, float]) -> Box | None:
    try:
        frames = _frames(video, start, end)
        if len(frames) < 4:
            return None
        activity, edges = analyse(frames)
        return find_facecam(activity, edges, face)
    except Exception:  # noqa: BLE001 - nooit de render laten falen op de herkenning
        log.exception("Facecam-herkenning mislukt")
        return None


def parse_box(text: str | None) -> Box | None:
    if not text:
        return None
    try:
        x, y, w, h = (float(v) for v in text.split(","))
    except ValueError:
        return None
    if w <= 0.02 or h <= 0.02 or x < 0 or y < 0 or x + w > 1.001 or y + h > 1.001:
        return None
    return (x, y, w, h)


def format_box(box: Box) -> str:
    return ",".join(f"{v:.4f}" for v in box)
