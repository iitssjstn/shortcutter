"""Transcriptie met faster-whisper (woord-timestamps, VAD), gecachet als transcript.json."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable

from app.pipeline.errors import WhisperError

log = logging.getLogger(__name__)

_models: dict[tuple[str, str, str], object] = {}
_lock = threading.Lock()


def _load_model(name: str, device: str, compute: str):
    key = (name, device, compute)
    with _lock:
        if key in _models:
            return _models[key]
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(name, device=device, compute_type=compute)
        except Exception as exc:
            raise WhisperError(
                f"Whisper-model '{name}' laden mislukt ({type(exc).__name__}: {str(exc)[:200]}). "
                "Controleer het model in Instellingen, de internetverbinding voor de eerste download, "
                "en WHISPER_DEVICE/WHISPER_COMPUTE."
            ) from exc
        _models.clear()  # houd maar één model in het geheugen
        _models[key] = model
        return model


def transcribe(
    media: Path,
    cache: Path,
    model_name: str,
    device: str = "cpu",
    compute: str = "int8",
    progress: Callable[[float], None] | None = None,
) -> dict:
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except ValueError:
            cache.unlink()
    model = _load_model(model_name, device, compute)
    try:
        segments, info = model.transcribe(
            str(media), word_timestamps=True, vad_filter=True, beam_size=5
        )
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
    except WhisperError:
        raise
    except Exception as exc:
        raise WhisperError(f"Transcriptie mislukt: {type(exc).__name__}: {str(exc)[:200]}") from exc
    data = {"language": getattr(info, "language", None), "duration": total, "segments": out_segments}
    cache.write_text(json.dumps(data, ensure_ascii=False))
    return data


def all_words(transcript: dict) -> list[dict]:
    return [w for seg in transcript.get("segments", []) for w in seg.get("words", [])]


def words_between(transcript: dict, start: float, end: float) -> list[dict]:
    return [w for w in all_words(transcript) if w["end"] > start and w["start"] < end]
