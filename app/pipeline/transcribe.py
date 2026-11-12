"""Transcriptie met faster-whisper (woord-timestamps, VAD), gecachet als transcript.json.

Whisper draait in een apart proces. Crasht het hard (geheugen vol, niet-ondersteunde
CPU-instructies), dan stopt alleen dat proces: de webapp blijft draaien en het project
krijgt een duidelijke foutmelding in plaats van dat de hele container herstart.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Callable

from app.pipeline.errors import WhisperError

log = logging.getLogger(__name__)

TIMEOUT = 6 * 3600


# ---------------------------------------------------------------- in het kindproces
def load_wav(path: Path):
    """Lees een 16 kHz mono 16-bit wav als float32-array in [-1, 1].

    Zo hoeft faster-whisper de audio niet zelf via PyAV te decoderen (nieuwere PyAV-versies
    zijn daar niet compatibel mee).
    """
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
            raise WhisperError("Onverwacht audioformaat (verwacht 16-bit mono wav).")
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_in_process(
    audio: Path, model_name: str, device: str, compute: str, threads: int = 0,
    progress: Callable[[float], None] | None = None, vad: bool = True,
) -> dict:
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_name, device=device, compute_type=compute, cpu_threads=threads)
    except Exception as exc:
        raise WhisperError(
            f"Whisper-model '{model_name}' laden mislukt ({type(exc).__name__}: {str(exc)[:200]}). "
            "Controleer het model in Instellingen, de internetverbinding voor de eerste download, "
            "en WHISPER_DEVICE/WHISPER_COMPUTE."
        ) from exc
    try:
        samples = load_wav(audio)
        segments, info = model.transcribe(samples, word_timestamps=True, vad_filter=vad, beam_size=5)
        out_segments = []
        total = float(getattr(info, "duration", 0) or 0)
        for seg in segments:
            words = [
                {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip()}
                for w in (seg.words or [])
                if w.word.strip()
            ]
            out_segments.append(
                {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(), "words": words}
            )
            if progress and total:
                progress(min(1.0, seg.end / total))
    except Exception as exc:
        raise WhisperError(f"Transcriptie mislukt: {type(exc).__name__}: {str(exc)[:200]}") from exc
    return {"language": getattr(info, "language", None), "duration": total, "segments": out_segments}


# ---------------------------------------------------------------- in de app
def extract_audio(media: Path, out: Path) -> Path:
    """16 kHz mono wav: kleiner en betrouwbaarder om te decoderen dan de hele video."""
    res = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(out)],
        capture_output=True, text=True, timeout=3600,
    )
    if res.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        raise WhisperError("Kon de audio niet uit de video halen (heeft de video wel geluid?).")
    return out


def _worker_cmd(audio: Path, out: Path, model: str, device: str, compute: str, threads: int,
                vad: bool = True) -> list[str]:
    return [sys.executable, "-m", "app.pipeline.transcribe_worker", str(audio), str(out), model, device, compute,
            str(threads), "1" if vad else "0"]


def cpu_flags(path: str = "/proc/cpuinfo") -> set[str]:
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("flags"):
                    return set(line.split(":", 1)[1].split())
    except OSError:
        pass
    return set()


def auto_cpu_isa(flags: set[str] | None = None) -> str | None:
    """Kies een CTranslate2-instructieset die deze CPU ondersteunt (None = automatisch)."""
    flags = cpu_flags() if flags is None else flags
    if not flags:
        return None
    if {"avx2", "fma"} <= flags:
        return None
    if "avx" in flags:
        return "AVX"
    return "GENERIC"


CRASH_CODES = {-signal.SIGILL, -signal.SIGSEGV, -signal.SIGBUS, 132, 139, 135}


def explain_exit(code: int, model: str) -> str:
    if code in (-signal.SIGKILL, 137):
        return (f"Whisper is gestopt omdat het geheugen vol raakte (OOM) met model '{model}'. "
                "Kies een kleiner model bij Instellingen (bijv. 'base' of 'tiny') of geef de container meer geheugen.")
    if code in (-signal.SIGILL, 132):
        return ("Whisper crashte op een CPU-instructie die deze processor niet ondersteunt (SIGILL), "
                "ook in de veilige modus. Probeer het model 'tiny' of 'base' bij Instellingen.")
    if code in (-signal.SIGSEGV, 139):
        return (f"Whisper crashte (segmentation fault) met model '{model}'. "
                "Probeer een ander model of WHISPER_COMPUTE=float32.")
    return f"Whisper stopte onverwacht (exitcode {code})."


def _run_worker(audio, tmp_out, model_name, device, compute, threads, vad, isa, progress) -> tuple[int, deque]:
    tail: deque[str] = deque(maxlen=15)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if isa:
        env["CT2_FORCE_CPU_ISA"] = isa
    proc = subprocess.Popen(
        _worker_cmd(audio, tmp_out, model_name, device, compute, threads, vad),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        cwd=str(Path(__file__).resolve().parents[2]), env=env,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("PROGRESS "):
                if progress:
                    try:
                        progress(float(line.split()[1]))
                    except (ValueError, IndexError):
                        pass
            elif line.startswith("ERROR "):
                tail.append(line[6:])
            elif line:
                tail.append(line)
        return proc.wait(timeout=TIMEOUT), tail
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise WhisperError("Transcriptie duurde te lang en is afgebroken.") from exc


def transcribe(
    media: Path,
    cache: Path,
    model_name: str,
    device: str = "cpu",
    compute: str = "int8",
    progress: Callable[[float], None] | None = None,
    threads: int = 0,
) -> dict:
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except ValueError:
            cache.unlink()
    audio = cache.parent / "audio.wav"
    tmp_out = cache.parent / "transcript.tmp.json"
    extract_audio(media, audio)
    isa = os.environ.get("CT2_FORCE_CPU_ISA") or auto_cpu_isa()
    vad = os.environ.get("WHISPER_VAD", "true").strip().lower() not in ("0", "false", "no", "nee")
    attempts = [(compute, isa, vad)]
    if device == "cpu":
        # veilige modus na een crash op CPU-instructies: geen AVX, float32, geen VAD (onnxruntime)
        attempts.append(("float32", "GENERIC", False))
    try:
        for i, (comp, cpu_isa, use_vad) in enumerate(attempts):
            code, tail = _run_worker(audio, tmp_out, model_name, device, comp, threads, use_vad, cpu_isa, progress)
            if code == 0:
                break
            tmp_out.unlink(missing_ok=True)
            errors = [t for t in tail if "mislukt" in t]
            if code == 2 and errors:
                raise WhisperError(errors[-1])
            log.warning("Whisper-proces stopte met code %s (compute=%s, isa=%s, vad=%s): %s",
                        code, comp, cpu_isa or "auto", use_vad, " | ".join(list(tail)[-3:]))
            if code in CRASH_CODES and i + 1 < len(attempts):
                log.warning("Whisper opnieuw in veilige modus (oude CPU?): GENERIC, float32, zonder VAD")
                continue
            raise WhisperError(explain_exit(code, model_name))
    finally:
        audio.unlink(missing_ok=True)
    data = json.loads(tmp_out.read_text())
    tmp_out.replace(cache)
    return data


def all_words(transcript: dict) -> list[dict]:
    return [w for seg in transcript.get("segments", []) for w in seg.get("words", [])]


def words_between(transcript: dict, start: float, end: float) -> list[dict]:
    return [w for w in all_words(transcript) if w["end"] > start and w["start"] < end]
