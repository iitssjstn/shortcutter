"""Kindproces voor Whisper: python -m app.pipeline.transcribe_worker <audio> <uit.json> <model> <device> <compute> <threads> [vad 1|0]

Schrijft 'PROGRESS 0.42'-regels naar stdout en het resultaat als JSON naar <uit.json>.
Exitcode 0 = gelukt, 2 = nette fout (melding op een 'ERROR '-regel).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.pipeline.errors import WhisperError
from app.pipeline.transcribe import transcribe_in_process


def main(argv: list[str]) -> int:
    audio, out, model, device, compute, threads = argv[:6]
    vad = (argv[6] if len(argv) > 6 else "1") == "1"
    try:
        data = transcribe_in_process(
            Path(audio), model, device, compute, int(threads),
            progress=lambda f: print(f"PROGRESS {f:.4f}", flush=True), vad=vad,
        )
    except WhisperError as exc:
        print(f"ERROR {exc.message}", flush=True)
        return 2
    Path(out).write_text(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
