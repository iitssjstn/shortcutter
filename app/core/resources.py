"""Zwaar werk (ffmpeg, Whisper) begrenzen zodat de server bruikbaar blijft.

* Minder threads: ffmpeg/x264 en Whisper gebruiken per thread extra geheugen; standaard 2 threads voor
  ffmpeg (FFMPEG_THREADS) en voor Whisper de helft van de kernen, hooguit 4 (WHISPER_THREADS).
* Lagere voorrang (nice): andere containers en SSH blijven vlot (PROCESS_NICE, 0 = uit).
"""
from __future__ import annotations

import os


def available_cpus() -> int:
    """Aantal CPU's dat deze container echt mag gebruiken (Docker `cpus:`), niet het aantal van de hele server."""
    n = os.cpu_count() or 2
    try:
        n = min(n, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    quota = _cgroup_cpu_quota()
    if quota:
        n = min(n, max(1, int(quota + 0.5)))
    return max(1, n)


def _cgroup_cpu_quota() -> float | None:
    try:  # cgroup v2: "200000 100000" of "max 100000"
        q, period = open("/sys/fs/cgroup/cpu.max").read().split()[:2]
        return None if q == "max" else int(q) / int(period)
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        period = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        return q / period if q > 0 and period > 0 else None
    except (OSError, ValueError):
        return None


def ffmpeg_threads() -> int:
    from app.core.config import get_settings

    return max(1, min(get_settings().ffmpeg_threads, available_cpus()))


def whisper_threads(requested: int = 0) -> int:
    if requested and requested > 0:
        return requested
    return max(1, min(4, available_cpus() // 2))


def make_background_process() -> None:
    """Voor het worker-proces: lagere voorrang en bij geheugengebrek eerder gestopt dan de website."""
    try:
        from app.core.config import get_settings

        n = get_settings().process_nice
        if n > 0:
            os.nice(n)
    except Exception:  # noqa: BLE001
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as fh:
            fh.write("500")
    except OSError:
        pass


def lower_priority(cmd: list[str]) -> list[str]:
    """Start het commando met een lagere voorrang via `nice`.

    (Niet via preexec_fn: dat is onveilig in een programma met meerdere threads en kan vastlopen.)
    """
    import shutil

    try:
        from app.core.config import get_settings

        n = get_settings().process_nice
    except Exception:  # noqa: BLE001 - nooit het starten blokkeren
        return cmd
    nice = shutil.which("nice") if n > 0 else None
    return [nice, "-n", str(n), *cmd] if nice and cmd else cmd


def ffmpeg_args(cmd: list[str]) -> list[str]:
    """Voeg thread-limieten toe aan een ffmpeg-commando: voor het decoderen (elke -i), filters en het coderen.

    Zonder limiet bij het decoderen gebruikt ffmpeg evenveel threads (en framebuffers) als de server kernen heeft.
    """
    if not cmd or os.path.basename(cmd[0]) != "ffmpeg" or "-threads" in cmd:
        return cmd
    n = str(ffmpeg_threads())
    body: list[str] = []
    for tok in cmd[1:-1]:
        if tok == "-i":
            body += ["-threads", n]
        body.append(tok)
    return [cmd[0], "-filter_complex_threads", n, "-filter_threads", n, *body, "-threads", n, cmd[-1]]
