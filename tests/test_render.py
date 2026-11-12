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


def test_no_detector_falls_back_to_blur(sample_video, monkeypatch):
    monkeypatch.setattr(framing, "_detector", lambda: None)
    ratio, fx = framing.detect_faces(sample_video, 0, 2)
    assert ratio == -1 and fx is None
    assert framing.choose_framing(sample_video, 1280, 720, 0, 2).mode == "blur"
    # 'vullen' werkt ook zonder detector: uitsnede in het midden
    assert framing.choose_framing(sample_video, 1280, 720, 0, 2, force_mode="crop").face_x == 0.5


def test_detector_chain(monkeypatch, tmp_path):
    framing._detector_cache.clear()
    det = framing._detector()
    assert det is not None and det.name == "yunet"
    # zonder YuNet-model: terugval op Haar
    framing._detector_cache.clear()
    monkeypatch.setattr(framing, "MODEL_PATH", tmp_path / "bestaat-niet.onnx")
    det = framing._detector()
    assert det is not None and det.name == "haar"
    # zonder model en zonder cascade: geen detector
    framing._detector_cache.clear()

    class Empty:
        def empty(self):
            return True

    monkeypatch.setattr(framing, "_cascade", lambda: Empty())
    assert framing._detector() is None
    framing._detector_cache.clear()


def test_pick_track_follows_main_person():
    # hoofdpersoon (groot) beweegt van 0.3 naar 0.5; een klein gezicht staat vast op 0.9
    samples = []
    for i in range(20):
        x = 0.3 + i * 0.01
        samples.append((i * 0.25, [(x, 0.4, 0.15, 0.9), (0.9, 0.2, 0.03, 0.7)]))
    samples[5] = (1.25, [(0.9, 0.2, 0.03, 0.7)])  # hoofdpersoon kort niet gevonden
    track = framing.pick_track(samples)
    assert track[5][1] is None
    assert track[0][1] == 0.3 and abs(track[-1][1] - 0.49) < 1e-9
    assert all(x is None or x < 0.6 for _, x in track)


def test_pick_track_reacquires_after_cut():
    samples = [(i * 0.25, [(0.2, 0.5, 0.1, 0.9)]) for i in range(8)]
    samples += [(2 + i * 0.25, [(0.8, 0.5, 0.1, 0.9)]) for i in range(8)]  # andere shot
    track = framing.pick_track(samples)
    assert track[-1][1] == 0.8


def test_smooth_path_ignores_jitter_and_follows_movement():
    jitter = [(i * 0.25, 0.5 + (0.01 if i % 2 else -0.01)) for i in range(20)]
    path = framing.smooth_path(jitter)
    assert max(p for _, p in path) - min(p for _, p in path) < 1e-9  # trilling genegeerd
    move = [(i * 0.25, 0.2 + i * 0.03) for i in range(20)] + [(5.0, None)]
    path = framing.smooth_path(move)
    assert path[-1][1] > 0.6 and path[0][1] < 0.3
    assert framing.smooth_path([(0, None), (0.25, None)]) is None


def test_build_commands_interpolates():
    cmds = framing.build_commands([(0.0, 0.25), (1.0, 0.75)], 1280, 404)
    lines = [l for l in cmds.splitlines() if l]
    assert lines[0] == "0.000 crop@fc x 118;"
    assert lines[-1].endswith("crop@fc x 758;")
    xs = [int(l.split()[-1].rstrip(";")) for l in lines]
    assert xs == sorted(xs) and len(xs) > 10


def test_tracking_render_moves_crop(tmp_path, sample_video, monkeypatch):
    """De uitsnede beweegt mee met een (nep)gezicht dat van links naar rechts gaat."""

    class MovingFace:
        name = "nep"

        def __init__(self):
            self.n = 0

        def detect(self, frame):
            self.n += 1
            return [(0.15 + 0.05 * self.n, 0.5, 0.1, 0.9)]

    monkeypatch.setattr(framing, "_detector", lambda: MovingFace())
    fr = framing.choose_framing(sample_video, 1280, 720, 1.0, 5.0, cmd_file="clip_01_crop.cmd")
    assert fr.mode == "crop" and fr.commands and "sendcmd=f=clip_01_crop.cmd" in fr.filter
    (tmp_path / "clip_01_crop.cmd").write_text(fr.commands)
    out = render.render_raw(sample_video, 1.0, 5.0, fr.filter, tmp_path / "clip_01_raw.mp4", cwd=tmp_path)
    assert (media.probe(out)["width"], media.probe(out)["height"]) == (1080, 1920)

    def expected(src_t, x):
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(src_t), "-i", str(sample_video), "-frames:v", "1",
                              "-vf", f"crop=404:720:{x}:0,scale=1080:1920", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        return np.frombuffer(raw, dtype=np.uint8).reshape(1920, 1080, 3).astype(int)

    first_x = int(fr.commands.splitlines()[0].split()[-1].rstrip(";"))
    last_x = int(fr.commands.strip().splitlines()[-1].split()[-1].rstrip(";"))
    assert last_x > first_x + 300
    early = frame_at(out, 0.0).astype(int)
    late = frame_at(out, 3.9).astype(int)
    assert np.abs(early - expected(1.0, first_x)).mean() < 15
    assert np.abs(late - expected(4.9, last_x)).mean() < 15


def test_crop_filter_clamps():
    f = framing.crop_filter(1920, 1080, 0.99)
    assert "crop=606:1080:1314:0" in f
    f = framing.crop_filter(1920, 1080, 0.0)
    assert "crop=606:1080:0:0" in f


def test_fill_mode_without_face_crops_center(sample_video):
    fr = framing.choose_framing(sample_video, 1280, 720, 0, 3, force_mode="crop")
    assert fr.mode == "crop" and fr.face_x == 0.5
    assert "crop=404:720:438:0" in fr.filter  # 9:16-uitsnede uit het midden


def test_manual_position_always_fills(sample_video):
    fr = framing.choose_framing(sample_video, 1280, 720, 0, 3, force_mode="auto", face_x=0.0)
    assert fr.mode == "crop" and "crop=404:720:0:0" in fr.filter
    fr = framing.choose_framing(sample_video, 1280, 720, 0, 3, force_mode="blur", face_x=0.0)
    assert fr.mode == "blur"


def test_fill_render_has_no_blur_bars(tmp_path, sample_video):
    """In vul-modus is ook de bovenkant van het 9:16-beeld de echte (bijgesneden) video, geen wazige balk."""
    expected = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", "2.0", "-i", str(sample_video), "-frames:v", "1",
         "-vf", "crop=404:720:438:0,scale=1080:1920", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    exp = np.frombuffer(expected, dtype=np.uint8).reshape(1920, 1080, 3).astype(int)
    results = {}
    for mode in ("crop", "blur"):
        fr = framing.choose_framing(sample_video, 1280, 720, 0, 3, force_mode=mode)
        out = render.render_raw(sample_video, 1.0, 3.0, fr.filter, tmp_path / f"clip_{mode}_raw.mp4")
        f = frame_at(out, 1.0).astype(int)
        results[mode] = np.abs(f[:400] - exp[:400]).mean()
    assert results["crop"] < 12, results
    assert results["blur"] > results["crop"] * 3, results


class _FixedFace:
    """Nep-detector met een vaste facecam linksboven."""
    name = "nep"

    def __init__(self, faces):
        self.faces = faces

    def detect(self, frame):
        return list(self.faces)


def test_split_regions():
    cam, game = framing.split_regions(1920, 1080, (0.12, 0.2, 0.06), game_x=0.5)
    cw, ch, x, y = cam
    assert abs(cw / ch - 1080 / 672) < 0.02 and x == 0 and y >= 0
    assert x <= 0.12 * 1920 <= x + cw and y <= 0.2 * 1080 <= y + ch  # gezicht valt binnen de facecam-uitsnede
    gw, gh, gx, gy = game
    assert gh == 1080 and abs(gw / gh - 1080 / 1248) < 0.01 and abs((gx + gw / 2) - 960) <= 2
    _, (gw2, _, gx2, _) = framing.split_regions(1920, 1080, (0.12, 0.2, 0.06), game_x=1.0)
    assert gx2 + gw2 == 1920  # gameplay helemaal naar rechts


def test_split_render(tmp_path, sample_video, monkeypatch):
    monkeypatch.setattr(framing, "_detector", lambda: _FixedFace([(0.15, 0.25, 0.08, 0.9)]))
    fr = framing.choose_framing(sample_video, 1280, 720, 0, 3, force_mode="split")
    assert fr.mode == "split" and "vstack" in fr.filter
    out = render.render_raw(sample_video, 0.5, 2.5, fr.filter, tmp_path / "clip_01_raw.mp4")
    p = media.probe(out)
    assert (p["width"], p["height"]) == (1080, 1920)


def test_split_without_face_falls_back_to_fill(sample_video, monkeypatch):
    monkeypatch.setattr(framing, "_detector", lambda: _FixedFace([]))
    fr = framing.choose_framing(sample_video, 1280, 720, 0, 3, force_mode="split", face_x=0.2)
    assert fr.mode == "crop" and fr.face_x == 0.2


def test_split_vertical_source_stays_vertical(sample_video):
    assert framing.choose_framing(sample_video, 720, 1280, 0, 3, force_mode="split").mode == "vertical"
