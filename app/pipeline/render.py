"""Render met ffmpeg: eerst de ruwe ge-cropte clip, daarna ondertitels inbranden.

Zo blijft bij een mislukte ondertitel-render altijd de ruwe versie (en de .ass)
beschikbaar om te downloaden, en is 'download zonder ondertitels' gratis.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from app.pipeline.errors import RenderError

X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 3600) -> None:
    try:
        res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RenderError("ffmpeg duurde te lang en is afgebroken.") from exc
    except FileNotFoundError as exc:
        raise RenderError("ffmpeg is niet geïnstalleerd.") from exc
    if res.returncode != 0:
        tail = "\n".join(res.stderr.strip().splitlines()[-6:])
        raise RenderError(f"ffmpeg faalde: {tail[:600]}")


def render_raw(
    source: Path, start: float, end: float, filter_complex: str, out: Path, has_audio: bool = True,
    cwd: Path | None = None,
) -> Path:
    """Knip (input-seek), frame naar 1080x1920, normaliseer het geluid (loudnorm I=-16)."""
    dur = max(0.1, end - start)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-nostdin",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(Path(source).resolve()),
        "-filter_complex", filter_complex, "-map", "[v]",
    ]
    if has_audio:
        cmd += ["-map", "0:a:0?", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd += X264 + ["-movflags", "+faststart", str(Path(out).resolve())]
    _run(cmd, cwd=cwd)
    if not out.exists() or out.stat().st_size == 0:
        raise RenderError("ffmpeg gaf geen uitvoerbestand.")
    return out


def burn_subtitles(raw: Path, ass: Path, out: Path) -> Path:
    """Brand de .ass in. Draait met cwd in de outputmap zodat het pad simpel blijft."""
    cwd = out.parent
    if ass.parent != cwd or raw.parent != cwd:
        raise RenderError("raw, .ass en uitvoer moeten in dezelfde map staan.")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-nostdin",
        "-i", raw.name, "-vf", f"subtitles={ass.name}",
        *X264, "-c:a", "copy", "-movflags", "+faststart", out.name,
    ]
    _run(cmd, cwd=cwd)
    if not out.exists() or out.stat().st_size == 0:
        raise RenderError("ffmpeg gaf geen uitvoerbestand bij het inbranden van de ondertitels.")
    return out


def add_teaser(raw: Path, a: float, b: float, out: Path, has_audio: bool = True) -> Path:
    """Zet het stuk [a, b] (clip-tijden) van `raw` kort vooraan, gevolgd door de hele clip.

    Zo ziet de kijker in de eerste seconde al het sterkste moment ('wacht hierop').
    """
    fo = max(0.0, (b - a) - 0.12)
    vf = (f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,fade=t=out:st={fo:.3f}:d=0.12[tv];"
          "[0:v]setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.12[mv];")
    if has_audio:
        vf += (f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={fo:.3f}:d=0.12[ta];"
               "[0:a]asetpts=PTS-STARTPTS[ma];[tv][ta][mv][ma]concat=n=2:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    else:
        vf += "[tv][mv]concat=n=2:v=1:a=0[v]"
        maps = ["-map", "[v]", "-an"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", str(Path(raw).resolve()),
           "-filter_complex", vf, *maps, *X264, "-movflags", "+faststart", str(Path(out).resolve())]
    _run(cmd)
    if not out.exists() or out.stat().st_size == 0:
        raise RenderError("ffmpeg gaf geen uitvoerbestand (teaser).")
    return out
