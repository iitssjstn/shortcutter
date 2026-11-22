"""Zwaar werk begrenzen: thread-limieten en lagere voorrang voor ffmpeg/Whisper."""
from app.core import resources


def test_ffmpeg_args_and_whisper_threads(monkeypatch):
    cmd = ["ffmpeg", "-y", "-i", "in.mp4", "-c:v", "libx264", "out.mp4"]
    out = resources.ffmpeg_args(cmd)
    assert out[0] == "ffmpeg" and out[-1] == "out.mp4" and out[-3:-1] == ["-threads", "2"]
    assert "-filter_complex_threads" in out
    assert resources.ffmpeg_args(["ffprobe", "x"]) == ["ffprobe", "x"]
    assert resources.ffmpeg_args(out) == out  # niet dubbel
    monkeypatch.setattr(resources, "available_cpus", lambda: 16)
    assert resources.whisper_threads(0) == 4 and resources.whisper_threads(3) == 3
    monkeypatch.setattr(resources, "available_cpus", lambda: 2)
    assert resources.whisper_threads(0) == 1
    assert resources.ffmpeg_threads() == 2


def test_available_cpus_reads_docker_limit(monkeypatch):
    monkeypatch.setattr(resources, "_cgroup_cpu_quota", lambda: 2.0)
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(resources.os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
    assert resources.available_cpus() == 2


def test_render_still_works_with_limits(sample_video, tmp_path):
    from app.pipeline import media, render

    out = render.render_raw(sample_video, 0.5, 2.0, "[0:v]scale=540:960,setsar=1[v]", tmp_path / "o.mp4")
    assert media.probe(out)["width"] == 540
