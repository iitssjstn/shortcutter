"""End-to-end render met een synthetische video en een nep-transcript."""
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.pipeline import captions, framing, media, render
from app.pipeline.transcribe import all_words
from tests.conftest import fake_transcript


def frame_at(video: Path, t: float) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(1920, 1080, 3)


@pytest.mark.parametrize("mode", ["blur", "crop"])
def test_render_end_to_end(tmp_path, sample_video, mode):
    info = media.probe(sample_video)
    assert (info["width"], info["height"], info["has_audio"]) == (1280, 720, True)
    fr = framing.choose_framing(sample_video, 1280, 720, 1.0, 7.0, force_mode=mode, face_x=0.3 if mode == "crop" else None)
    assert fr.mode == mode
    out = tmp_path
    raw, ass, final = out / "clip_01_raw.mp4", out / "clip_01.ass", out / "clip_01.mp4"
    t = fake_transcript(8.0)
    captions.write_ass(ass, all_words(t), 1.0, 7.0, "geel")
    render.render_raw(sample_video, 1.0, 7.0, fr.filter, raw)
    render.burn_subtitles(raw, ass, final)
    for f in (raw, final):
        p = media.probe(f)
        assert (p["width"], p["height"]) == (1080, 1920)
        assert 5.5 < p["duration"] < 6.6
        assert p["has_audio"]
    # ondertitels zichtbaar: het ondertitelgebied verschilt duidelijk tussen raw en final
    t_check = 2.0
    a, b = frame_at(raw, t_check).astype(int), frame_at(final, t_check).astype(int)
    band = slice(1920 - 520 - 160, 1920 - 520 + 40)
    diff_band = np.abs(a[band] - b[band]).mean()
    diff_top = np.abs(a[:300] - b[:300]).mean()
    assert diff_band > 8, f"ondertitels niet zichtbaar (diff {diff_band:.1f})"
    assert diff_top < diff_band / 4
    # gele highlight aanwezig in het ondertitelgebied (R en G hoog, B laag)
    reg = b[band]
    yellow = ((reg[..., 0] > 200) & (reg[..., 1] > 180) & (reg[..., 2] < 90)).sum()
    assert yellow > 200
    assert media.thumbnail(final, out / "clip_01.jpg")


def test_vertical_source_scale_crop(tmp_path):
    src = tmp_path / "staand.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=25:duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)], check=True)
    info = media.probe(src)
    assert not info["has_audio"]
    fr = framing.choose_framing(src, info["width"], info["height"], 0, 3)
    assert fr.mode == "vertical"
    out = render.render_raw(src, 0.5, 2.5, fr.filter, tmp_path / "clip_01_raw.mp4", has_audio=False)
    p = media.probe(out)
    assert (p["width"], p["height"]) == (1080, 1920)


def test_landscape_without_faces_uses_blur(sample_video):
    fr = framing.choose_framing(sample_video, 1280, 720, 0, 3)
    assert fr.mode == "blur"


def test_missing_cascade_falls_back_to_blur(sample_video, monkeypatch):
    class Empty:
        def empty(self):
            return True

    monkeypatch.setattr(framing, "_cascade", lambda: Empty())
    ratio, fx = framing.detect_faces(sample_video, 0, 2)
    assert ratio == -1 and fx is None
    assert framing.choose_framing(sample_video, 1280, 720, 0, 2).mode == "blur"


def test_crop_filter_clamps():
    f = framing.crop_filter(1920, 1080, 0.99)
    assert "crop=606:1080:1314:0" in f
    f = framing.crop_filter(1920, 1080, 0.0)
    assert "crop=606:1080:0:0" in f
