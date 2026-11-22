"""Het zware werk (verwerken, renderen, uploaden, planningen) als eigen proces.

De website start dit proces zelf (zie app.main). Zo kan het renderen de website niet meer
ophouden: het draait met lagere voorrang, deelt geen Python-interpreter met de website en is
bij geheugengebrek als eerste aan de beurt om gestopt te worden (daarna start het vanzelf weer).
"""
from __future__ import annotations

import logging
import os
import signal
import threading

from app.core import crypto
from app.core.config import get_settings
from app.core.db import init_db
from app.core.resources import make_background_process


def main() -> None:
    s = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    make_background_process()
    init_db(str(s.db_path))
    crypto.load_master_key(s)

    from app.worker.runner import Worker

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    parent = os.getppid()
    w = Worker()
    w.start()
    logging.getLogger("shortcutter").info("Worker-proces gestart (pid %s)", os.getpid())
    while not stop.wait(2):
        if os.getppid() != parent:  # de website is gestopt: wij ook
            break
    w.stop()


if __name__ == "__main__":
    main()
