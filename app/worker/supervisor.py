"""Start het worker-proces (python -m app.worker) en start het opnieuw als het onverwacht stopt.

Bijvoorbeeld als het bij geheugengebrek gestopt werd: de website blijft gewoon draaien en
het werk gaat na een korte pauze verder (onderbroken taken worden dan opnieuw ingepland).
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

log = logging.getLogger("shortcutter")

RESTART_DELAY = 5.0
MAX_DELAY = 120.0


class Supervisor:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="shortcutter-supervisor", daemon=True)
        self._thread.start()

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-m", "app.worker"])

    def _loop(self) -> None:
        delay = RESTART_DELAY
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._proc = self._spawn()
            except OSError:
                log.exception("Worker-proces starten mislukt")
                self._stop.wait(delay)
                continue
            code = self._proc.wait()
            if self._stop.is_set():
                return
            if code in (-9, 137):
                log.error("Worker-proces gestopt door geheugengebrek (code %s); herstart over %.0f s. "
                          "Tip: kies een kleiner Whisper-model of geef de container meer geheugen.", code, delay)
            else:
                log.error("Worker-proces onverwacht gestopt (code %s); herstart over %.0f s.", code, delay)
            # draaide hij een tijd goed, dan direct weer kort wachten; anders steeds langer
            delay = RESTART_DELAY if time.monotonic() - started > 300 else min(MAX_DELAY, delay * 2)
            self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()
        p = self._proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
