"""Zwaar werk (ffmpeg, Whisper) begrenzen zodat de server bruikbaar blijft.

* Minder threads: ffmpeg/x264 en Whisper gebruiken per thread extra geheugen; standaard 2 threads voor
  ffmpeg (FFMPEG_THREADS) en voor Whisper de helft van de kernen, hooguit 4 (WHISPER_THREADS).
* Lagere voorrang (nice): andere containers en SSH blijven vlot (PROCESS_NICE, 0 = uit).
"""
from __future__ import annotations

import os


def ffmpeg_threads() -> int:
    from app.core.config import get_settings

    return max(1, get_settings().ffmpeg_threads)


def whisper_threads(requested: int = 0) -> int:
    if requested and requested > 0:
        return requested
    cores = os.cpu_count() or 2
    return max(1, min(4, cores // 2))


def lower_priority() -> None:
    """Voor subprocess.preexec_fn: zet de nice-waarde van het kindproces."""
    try:
        from app.core.config import get_settings

        n = get_settings().process_nice
        if n > 0:
            os.nice(n)
    except Exception:  # noqa: BLE001 - nooit het starten blokkeren
        pass


def ffmpeg_args(cmd: list[str]) -> list[str]:
    """Voeg thread-limieten toe aan een ffmpeg-commando (vóór het uitvoerbestand)."""
    if not cmd or os.path.basename(cmd[0]) != "ffmpeg" or "-threads" in cmd:
        return cmd
    n = str(ffmpeg_threads())
    return [cmd[0], "-filter_complex_threads", n, "-filter_threads", n, *cmd[1:-1], "-threads", n, cmd[-1]]
