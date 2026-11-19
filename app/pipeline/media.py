"""Kleine ffmpeg/ffprobe-hulpjes."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe kon het bestand niet lezen: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise RuntimeError("Het bestand bevat geen videospoor.")
    w, h = int(video.get("width", 0)), int(video.get("height", 0))
    rot = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) in (90, 270):
        w, h = h, w
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    return {"width": w, "height": h, "duration": duration, "has_audio": audio is not None}


def thumbnail(video: Path, out: Path, at: float = 1.0, width: int = 480) -> bool:
    res = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(video),
            "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0 or not out.exists():
        if at > 0:
            return thumbnail(video, out, 0.0, width)
        return False
    return True


def frames_b64(video: Path, times: list[float], width: int = 512) -> list[str]:
    """Losse frames als base64-JPEG (voor een LLM met beeldherkenning). Mislukte frames worden overgeslagen."""
    import base64

    out = []
    for t in times:
        res = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "-"],
            capture_output=True, timeout=60,
        )
        if res.returncode == 0 and res.stdout:
            out.append(base64.b64encode(res.stdout).decode())
    return out


def loudest_moment(video: Path, start: float, end: float, rate: int = 8000) -> float | None:
    """Brontijd van het luidste moment (gemiddeld over ±1 s) binnen [start+1.5, end-0.8]; None zonder geluid.

    Bij streams valt het luidste moment meestal samen met de reactie of clou.
    """
    import numpy as np

    dur = max(0.0, end - start)
    if dur < 4:
        return None
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
         "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True, timeout=300,
    )
    if res.returncode != 0 or not res.stdout:
        return None
    a = np.frombuffer(res.stdout, dtype=np.int16).astype(np.float32)
    win = rate // 4  # vensters van 0,25 s
    n = len(a) // win
    if n < 8:
        return None
    rms = np.sqrt((a[: n * win].reshape(n, win) ** 2).mean(axis=1))
    if float(rms.max()) < 50:  # (vrijwel) stil
        return None
    smooth = np.convolve(rms, np.ones(4) / 4, mode="same")  # ±1 s
    lo, hi = 6, max(7, n - 3)  # niet in de eerste 1,5 s of de laatste 0,8 s
    i = lo + int(np.argmax(smooth[lo:hi]))
    return round(start + (i + 0.5) * 0.25, 2)
