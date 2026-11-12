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


MODEL_PATH = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"
TRACK_STEP = 0.25  # seconden tussen gezichtsmetingen
DETECT_WIDTH = 640  # frames worden voor detectie naar deze breedte geschaald
CMD_STEP = 1 / 15  # resolutie van de meebewegende uitsnede


@dataclass
class Framing:
    mode: str  # vertical | crop | blur
    filter: str  # ffmpeg filter_complex met invoer [0:v] en uitvoer [v]
    face_ratio: float = 0.0
    face_x: float | None = None  # (mediane) gezichtsmidden, als fractie van de breedte
    commands: str | None = None  # sendcmd-inhoud voor een meebewegende uitsnede


# ---------------------------------------------------------------- detectoren
def _cascade():
    import cv2

    path = Path(getattr(cv2, "data", None).haarcascades if hasattr(cv2, "data") else "") / (
        "haarcascade_frontalface_default.xml"
    )
    return cv2.CascadeClassifier(str(path))


class _Detector:
    """YuNet (neuraal netwerk) als het model er is, anders Haar-cascades (frontaal + profiel)."""

    def __init__(self) -> None:
        import cv2

        self.cv2 = cv2
        self.yunet = None
        if MODEL_PATH.is_file() and hasattr(cv2, "FaceDetectorYN"):
            try:
                self.yunet = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (320, 320), 0.6, 0.3, 50)
            except cv2.error:
                log.warning("YuNet kon niet geladen worden; terugval op Haar-cascades")
        self.frontal = self.profile = None
        if self.yunet is None:
            frontal = _cascade()
            if frontal.empty():
                raise RuntimeError("geen gezichtsdetector beschikbaar")
            self.frontal = frontal
            prof = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_profileface.xml"))
            self.profile = None if prof.empty() else prof

    @property
    def name(self) -> str:
        return "yunet" if self.yunet is not None else "haar"

    def detect(self, frame) -> list[tuple[float, float, float, float]]:
        """Geeft gezichten als (cx, cy, grootte, score) in fracties van het frame."""
        cv2 = self.cv2
        h, w = frame.shape[:2]
        scale = DETECT_WIDTH / w if w > DETECT_WIDTH else 1.0
        img = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame
        ih, iw = img.shape[:2]
        out: list[tuple[float, float, float, float]] = []
        if self.yunet is not None:
            self.yunet.setInputSize((iw, ih))
            _, faces = self.yunet.detect(img)
            for f in faces if faces is not None else []:
                x, y, fw, fh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])
                out.append(((x + fw / 2) / iw, (y + fh / 2) / ih, fw / iw, score))
            return out
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = [(*f, 1.0) for f in self.frontal.detectMultiScale(gray, 1.1, 4, minSize=(24, 24))]
        if self.profile is not None:
            found += [(*f, 0.8) for f in self.profile.detectMultiScale(gray, 1.1, 4, minSize=(24, 24))]
            flipped = cv2.flip(gray, 1)
            found += [(iw - x - fw, y, fw, fh, 0.8) for x, y, fw, fh in self.profile.detectMultiScale(flipped, 1.1, 4, minSize=(24, 24))]
        for x, y, fw, fh, score in found:
            out.append(((x + fw / 2) / iw, (y + fh / 2) / ih, fw / iw, score))
        return out


_detector_cache: list = []


def _detector() -> _Detector | None:
    if not _detector_cache:
        try:
            _detector_cache.append(_Detector())
        except Exception as exc:  # noqa: BLE001
            log.warning("Gezichtsdetectie niet beschikbaar (%s); framing valt terug op passend", exc)
            _detector_cache.append(None)
    return _detector_cache[0]


# ---------------------------------------------------------------- meten en volgen
Sample = tuple[float, list[tuple[float, float, float, float]]]


def sample_faces(video: Path, start: float, end: float, step: float = TRACK_STEP, detector=None) -> list[Sample] | None:
    """Meet elke `step` seconden de gezichten. Tijden zijn relatief aan `start`. None = geen detector."""
    import cv2

    detector = detector or _detector()
    if detector is None:
        return None
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    samples: list[Sample] = []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        every = max(1, int(round(step * fps)))
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
        n_frames = int((end - start) * fps)
        for i in range(n_frames):
            if i % every:
                if not cap.grab():
                    break
                continue
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            samples.append((i / fps, detector.detect(frame)))
    finally:
        cap.release()
    return samples


def pick_track(samples: list[Sample], max_jump: float = 0.2, reacquire_after: int = 4) -> list[tuple[float, float | None]]:
    """Kies per meting het gezicht van de hoofdpersoon.

    Startpunt is de x-positie waar (gewogen naar grootte) het vaakst een gezicht staat; daarna wordt
    steeds het grootste gezicht dicht bij de vorige positie gekozen. Is de hoofdpersoon een tijdje weg
    (bijv. een andere shot), dan wordt het grootste gezicht in beeld opnieuw gekozen.
    """
    bins = [0.0] * 20
    for _, faces in samples:
        for cx, _, size, score in faces:
            bins[min(19, max(0, int(cx * 20)))] += size * score
    if not any(bins):
        return [(t, None) for t, _ in samples]
    prev = (bins.index(max(bins)) + 0.5) / 20
    track: list[tuple[float, float | None]] = []
    misses = 0
    for t, faces in samples:
        near = [f for f in faces if abs(f[0] - prev) <= max_jump]
        if not near and faces and misses >= reacquire_after:
            near = faces
        if near:
            best = max(near, key=lambda f: f[2] * f[3])
            prev = best[0]
            misses = 0
            track.append((t, best[0]))
        else:
            misses += 1
            track.append((t, None))
    return track


def smooth_path(track: list[tuple[float, float | None]], window: int = 2, dead_zone: float = 0.04) -> list[tuple[float, float]] | None:
    """Vul gaten, middel uit en negeer kleine bewegingen, zodat de uitsnede rustig meebeweegt."""
    known = [x for _, x in track if x is not None]
    if not known:
        return None
    filled: list[float] = []
    last = known[0]
    for _, x in track:
        last = x if x is not None else last
        filled.append(last)
    avg = [
        statistics.fmean(filled[max(0, i - window): i + window + 1]) for i in range(len(filled))
    ]
    # Rustige 'camera': blijf stilstaan bij kleine bewegingen; gaat het gezicht buiten de marge,
    # stuur dan soepel bij tot het weer (bijna) in het midden staat.
    cur = avg[0]
    prev_p = avg[0]
    following = False
    out = []
    for (t, _), p in zip(track, avg):
        if abs(p - cur) > dead_zone:
            following = True
        if following:
            # bijsturen + de snelheid van het gezicht meenemen, zodat er geen achterstand ontstaat
            cur += (p - cur) * 0.6 + (p - prev_p) * 0.4
            if abs(p - cur) < 0.005 and abs(p - prev_p) < 0.002:
                cur = p
                following = False
        prev_p = p
        out.append((t, cur))
    return out


def detect_faces(video: Path, start: float, end: float, step: float = TRACK_STEP) -> tuple[float, float | None]:
    """Geeft (aandeel metingen met de hoofdpersoon, mediane x) terug; (-1, None) zonder detector."""
    samples = sample_faces(video, start, end, step)
    if samples is None:
        return -1.0, None
    if not samples:
        return 0.0, None
    track = pick_track(samples)
    xs = [x for _, x in track if x is not None]
    return len(xs) / len(samples), (statistics.median(xs) if xs else None)


def crop_size(src_w: int, src_h: int) -> tuple[int, int]:
    cw = min(int(src_h * 9 / 16) // 2 * 2, src_w // 2 * 2)
    return cw, src_h // 2 * 2


def _x_px(cx: float, src_w: int, cw: int) -> int:
    return max(0, min(src_w - cw, int(cx * src_w - cw / 2))) // 2 * 2


def build_commands(path: list[tuple[float, float]], src_w: int, cw: int, step: float = CMD_STEP) -> str:
    """sendcmd-regels die de x van de uitsnede soepel (lineair geïnterpoleerd) laten meebewegen."""
    lines = []
    last_x = None
    end = path[-1][0]
    t = 0.0
    j = 0
    while t <= end + 1e-9:
        while j + 1 < len(path) and path[j + 1][0] <= t:
            j += 1
        if j + 1 < len(path):
            (t0, x0), (t1, x1) = path[j], path[j + 1]
            cx = x0 + (x1 - x0) * ((t - t0) / (t1 - t0) if t1 > t0 else 0)
        else:
            cx = path[-1][1]
        x = _x_px(cx, src_w, cw)
        if x != last_x:
            lines.append(f"{t:.3f} crop@fc x {x};")
            last_x = x
        t += step
    return "\n".join(lines) + "\n"


def tracking_filter(src_w: int, src_h: int, x0: int, cmd_file: str) -> str:
    cw, ch = crop_size(src_w, src_h)
    return (
        f"[0:v]sendcmd=f={cmd_file},crop@fc={cw}:{ch}:{x0}:0,"
        f"scale={OUT_W}:{OUT_H},setsar=1,fps=30[v]"
    )


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


def _tracked(src_w: int, src_h: int, track, cmd_file: str, ratio: float) -> Framing:
    path = smooth_path(track)
    xs = [x for _, x in track if x is not None]
    med = statistics.median(xs)
    cw, _ = crop_size(src_w, src_h)
    if path is None or max(p for _, p in path) - min(p for _, p in path) < 0.03:
        return Framing("crop", crop_filter(src_w, src_h, med), ratio, med)  # staat stil: vaste uitsnede
    return Framing(
        "crop",
        tracking_filter(src_w, src_h, _x_px(path[0][1], src_w, cw), cmd_file),
        ratio,
        med,
        commands=build_commands(path, src_w, cw),
    )


def choose_framing(
    video: Path,
    src_w: int,
    src_h: int,
    start: float,
    end: float,
    force_mode: str | None = None,
    face_x: float | None = None,
    cmd_file: str = "crop.cmd",
) -> Framing:
    """Kies de framing.

    force_mode: None/'auto' = vullen (en het gezicht volgen) als de hoofdpersoon in minstens 30% van de
    metingen in beeld is, anders passend; 'crop' = altijd vullen (gezicht volgen, of het midden);
    'blur' = altijd passend op een wazige achtergrond.
    face_x: handmatige vaste positie van de uitsnede (0 = links, 1 = rechts).
    cmd_file: bestandsnaam voor de sendcmd-regels als de uitsnede meebeweegt (Framing.commands).
    """
    mode = force_mode if force_mode in MODES else "auto"
    if src_h >= src_w:
        return Framing("vertical", vertical_filter())
    if mode == "blur":
        return Framing("blur", blur_filter())
    if face_x is not None:  # gebruiker koos zelf een positie: vaste uitsnede
        fx = min(1.0, max(0.0, face_x))
        return Framing("crop", crop_filter(src_w, src_h, fx), 1.0, fx)
    samples = sample_faces(video, start, end)
    track = pick_track(samples) if samples else []
    hits = sum(1 for _, x in track if x is not None)
    ratio = hits / len(track) if track else (-1.0 if samples is None else 0.0)
    if hits and (mode == "crop" or ratio >= FACE_RATIO):
        return _tracked(src_w, src_h, track, cmd_file, ratio)
    if mode == "crop":
        return Framing("crop", crop_filter(src_w, src_h, 0.5), max(ratio, 0.0), 0.5)
    return Framing("blur", blur_filter(), max(ratio, 0.0), None)
