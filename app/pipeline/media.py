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
