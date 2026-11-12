"""Framing naar 1080x1920: staand, crop rond gezicht, of scherp op wazige achtergrond."""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

OUT_W, OUT_H = 1080, 1920
SAMPLE_STEP = 0.5
FACE_RATIO = 0.30


@dataclass
class Framing:
    mode: str  # vertical | crop | blur
    filter: str  # ffmpeg filter_complex met invoer [0:v] en uitvoer [v]
    face_ratio: float = 0.0
    face_x: float | None = None  # mediane gezichtsmidden, als fractie van de breedte


def _cascade():
    import cv2

    path = Path(getattr(cv2, "data", None).haarcascades if hasattr(cv2, "data") else "") / (
        "haarcascade_frontalface_default.xml"
    )
    cascade = cv2.CascadeClassifier(str(path))
    return cascade


def detect_faces(video: Path, start: float, end: float, step: float = SAMPLE_STEP) -> tuple[float, float | None]:
    """Geeft (aandeel samples met gezicht, mediane x-midden als fractie) terug.

    Geeft (-1, None) als de cascade niet geladen kan worden.
    """
    try:
        import cv2
    except ImportError:  # pragma: no cover
        return -1.0, None
    cascade = _cascade()
    if cascade.empty():
        log.warning("Haar-cascade ontbreekt; framing valt terug op blur-modus")
        return -1.0, None
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return 0.0, None
    samples, hits, xs = 0, 0, []
    t = start
    try:
        while t < end:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            t += step
            if not ok or frame is None:
                continue
            samples += 1
            h, w = frame.shape[:2]
            scale = 640 / w if w > 640 else 1.0
            small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(24, 24))
            if len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                hits += 1
                xs.append((x + fw / 2) / small.shape[1])
    finally:
        cap.release()
    if samples == 0:
        return 0.0, None
    return hits / samples, (statistics.median(xs) if xs else None)


def vertical_filter() -> str:
    return (
        f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},setsar=1,fps=30[v]"
    )


def crop_filter(src_w: int, src_h: int, face_x: float) -> str:
    cw = int(src_h * 9 / 16) // 2 * 2
    cw = min(cw, src_w // 2 * 2)
    cx = int(face_x * src_w - cw / 2)
    cx = max(0, min(src_w - cw, cx))
    return f"[0:v]crop={cw}:{src_h // 2 * 2}:{cx}:0,scale={OUT_W}:{OUT_H},setsar=1,fps=30[v]"


def blur_filter() -> str:
    return (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={OUT_W // 4}:{OUT_H // 4}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W // 4}:{OUT_H // 4},boxblur=10:2,scale={OUT_W}:{OUT_H},setsar=1[bgb];"
        f"[fg]scale={OUT_W}:-2,setsar=1[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps=30[v]"
    )


MODES = ("auto", "crop", "blur")


def choose_framing(
    video: Path,
    src_w: int,
    src_h: int,
    start: float,
    end: float,
    force_mode: str | None = None,
    face_x: float | None = None,
) -> Framing:
    """Kies de framing.

    force_mode: None/'auto' = vullen als er een gezicht in beeld is, anders passend;
    'crop' = altijd vullen (rond het gezicht, of op face_x, of in het midden);
    'blur' = altijd passend op een wazige achtergrond.
    face_x: handmatige horizontale positie van de uitsnede (0 = links, 1 = rechts).
    """
    mode = force_mode if force_mode in MODES else "auto"
    if src_h >= src_w:
        return Framing("vertical", vertical_filter())
    if mode == "blur":
        return Framing("blur", blur_filter())
    if face_x is not None:  # gebruiker koos zelf een positie: altijd vullen
        fx = min(1.0, max(0.0, face_x))
        return Framing("crop", crop_filter(src_w, src_h, fx), 1.0, fx)
    ratio, fx = detect_faces(video, start, end)
    if mode == "crop":
        pos = fx if fx is not None else 0.5
        return Framing("crop", crop_filter(src_w, src_h, pos), max(ratio, 0.0), pos)
    if ratio >= FACE_RATIO and fx is not None:
        return Framing("crop", crop_filter(src_w, src_h, fx), ratio, fx)
    return Framing("blur", blur_filter(), max(ratio, 0.0), fx)
