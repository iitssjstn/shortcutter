"""Whisper draait in een apart proces: een harde crash mag de app niet meenemen."""
import json
import sqlite3
import sys

import pytest

from app.core.db import init_db, new_session
from app.core.models import Job, Project
from app.pipeline import transcribe as tr
from app.pipeline.errors import WhisperError
from app.worker import queue
from tests.conftest import user_id


def _cmd(code: str):
    return lambda audio, out, *a: [sys.executable, "-c", code, str(audio), str(out)]


def test_hard_crash_becomes_clear_error(tmp_path, sample_video, monkeypatch):
    monkeypatch.setattr(tr, "_worker_cmd", _cmd(
        "import os, signal; print('PROGRESS 0.5', flush=True); os.kill(os.getpid(), signal.SIGKILL)"))
    seen = []
    with pytest.raises(WhisperError) as exc:
        tr.transcribe(sample_video, tmp_path / "transcript.json", "large-v3", progress=seen.append)
    assert "geheugen vol" in exc.value.message and "large-v3" in exc.value.message
    assert seen == [0.5]
    assert not (tmp_path / "audio.wav").exists()
    assert not (tmp_path / "transcript.json").exists()


def test_successful_subprocess(tmp_path, sample_video, monkeypatch):
    code = ("import json, sys; print('PROGRESS 1.0', flush=True); "
            "json.dump({'duration': 8, 'segments': [{'start': 0, 'end': 1, 'text': 'hoi', 'words': []}]}, open(sys.argv[2], 'w'))")
    monkeypatch.setattr(tr, "_worker_cmd", _cmd(code))
    data = tr.transcribe(sample_video, tmp_path / "transcript.json", "small")
    assert data["segments"][0]["text"] == "hoi"
    assert json.loads((tmp_path / "transcript.json").read_text()) == data
    # tweede keer uit de cache, zonder proces
    monkeypatch.setattr(tr, "_worker_cmd", _cmd("raise SystemExit(1)"))
    assert tr.transcribe(sample_video, tmp_path / "transcript.json", "small") == data


def test_real_worker_reports_model_error(tmp_path, sample_video):
    with pytest.raises(WhisperError) as exc:
        tr.transcribe(sample_video, tmp_path / "transcript.json", "dit-model-bestaat-niet")
    assert "laden mislukt" in exc.value.message


def test_explain_exit_codes():
    assert "geheugen" in tr.explain_exit(-9, "small")
    assert "SIGILL" in tr.explain_exit(-4, "small")
    assert "exitcode 3" in tr.explain_exit(3, "small")


def test_crash_loop_is_stopped(admin_client):
    db = new_session()
    p = Project(owner_id=user_id("admin"), source_type="upload", title="x")
    db.add(p)
    db.commit()
    job = queue.enqueue(db, p)
    for expected in ("queued", "failed"):
        queue.claim_next(db) if expected == "queued" else None
        if expected == "failed":
            j = db.get(Job, job.id)
            j.status = "running"
            db.commit()
        queue.recover(db)
        db.refresh(job)
        assert job.status == expected
    db.refresh(p)
    assert p.status == "failed" and p.error_kind == "onderbroken" and "Whisper-model" in p.error
    db.close()
    assert "Verwerking onderbroken" in admin_client.get(f"/projects/{p.id}").text


def test_migration_adds_attempts_column(tmp_path):
    path = tmp_path / "oud.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, project_id INTEGER, clip_id INTEGER, owner_id INTEGER, "
                "kind VARCHAR(20), status VARCHAR(20), error TEXT, created_at DATETIME, started_at DATETIME, finished_at DATETIME)")
    con.commit()
    con.close()
    init_db(str(path))
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(jobs)")]
    assert "attempts" in cols


def test_auto_cpu_isa():
    assert tr.auto_cpu_isa({"sse4_2", "avx", "avx2", "fma"}) is None
    assert tr.auto_cpu_isa({"sse4_2", "avx"}) == "AVX"
    assert tr.auto_cpu_isa({"sse2", "sse4_1"}) == "GENERIC"
    assert tr.auto_cpu_isa(set()) is None


def test_sigill_retries_in_safe_mode(tmp_path, sample_video, monkeypatch):
    """Oude CPU: eerste poging crasht met SIGILL, de veilige modus (GENERIC, float32, geen VAD) lukt."""
    calls = []

    def cmd(audio, out, model, device, compute, threads, vad=True):
        calls.append((compute, vad))
        if compute != "float32":
            code = "import os, signal; os.kill(os.getpid(), signal.SIGILL)"
        else:
            code = ("import json, os, sys; assert os.environ['CT2_FORCE_CPU_ISA'] == 'GENERIC'; "
                    "json.dump({'duration': 1, 'segments': []}, open(sys.argv[2], 'w'))")
        return [sys.executable, "-c", code, str(audio), str(out)]

    monkeypatch.setattr(tr, "_worker_cmd", cmd)
    data = tr.transcribe(sample_video, tmp_path / "transcript.json", "small", compute="int8")
    assert data == {"duration": 1, "segments": []}
    assert calls == [("int8", True), ("float32", False)]


def test_sigill_twice_gives_cpu_message(tmp_path, sample_video, monkeypatch):
    monkeypatch.setattr(tr, "_worker_cmd", _cmd("import os, signal; os.kill(os.getpid(), signal.SIGILL)"))
    with pytest.raises(WhisperError) as exc:
        tr.transcribe(sample_video, tmp_path / "transcript.json", "small")
    assert "CPU-instructie" in exc.value.message


def test_in_process_passes_array_not_file(tmp_path, sample_video, monkeypatch):
    """faster-whisper krijgt een numpy-array, zodat PyAV niet gebruikt wordt om audio te openen."""
    import types

    import numpy as np

    wav = tr.extract_audio(sample_video, tmp_path / "audio.wav")
    samples = tr.load_wav(wav)
    assert samples.dtype == np.float32 and abs(len(samples) / 16000 - 8.0) < 0.1
    assert 0.05 < np.abs(samples).max() <= 1.0
    seen = {}

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, audio, **kw):
            seen["audio"] = audio
            seen["kw"] = kw
            word = types.SimpleNamespace(start=0.0, end=0.5, word=" hoi")
            seg = types.SimpleNamespace(start=0.0, end=0.5, text=" hoi", words=[word])
            return iter([seg]), types.SimpleNamespace(duration=8.0, language="nl")

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    data = tr.transcribe_in_process(wav, "small", "cpu", "int8", vad=False)
    assert isinstance(seen["audio"], np.ndarray)
    assert seen["kw"]["word_timestamps"] is True and seen["kw"]["vad_filter"] is False
    assert data["segments"][0]["words"][0]["word"] == "hoi"
