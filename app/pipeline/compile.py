"""Compilaties: meerdere Twitch-clips achter elkaar in één liggende 16:9-video (1920x1080).

Per clip een eigen segment (zelfde codec/instellingen), met een balk linksonder
(plaats, cliptitel, streamer), korte fades en gelijk geluidsniveau. Optioneel een
intro- en outrokaart op een vervaagde achtergrond. De segmenten worden zonder
opnieuw coderen aan elkaar geplakt; hoofdstukken en credits gaan in de beschrijving.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.models import Compilation, CompilationItem, utcnow
from app.pipeline import media
from app.pipeline.download import download
from app.pipeline.errors import DownloadBlocked, DownloadFailed, RenderError
from app.pipeline.render import _run

log = logging.getLogger(__name__)

W, H, FPS = 1920, 1080, 30
INTRO_SECONDS = 3.0
OUTRO_SECONDS = 4.0
MAX_ITEMS = 100  # Twitch geeft per kanaal maximaal 100 clips per verzoek
CHAPTER_MIN_GAP = 10  # YouTube: hoofdstukken minstens 10 s
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-video_track_timescale", "15360", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
SHORT_W, SHORT_H = 1080, 1920
MAX_SHORT_ITEMS = 10  # meer past niet leesbaar in het lijstje
SHORT_MAX_SECONDS = 180  # YouTube Shorts: maximaal 3 minuten
FORMATS = {"landscape": "Lange video (16:9)", "short": "Short top-lijst (9:16)"}
RANKINGS = {"countdown": "Aflopend (#10 → #1)", "top": "Beste eerst (#1 → #10)", "none": "Zonder nummers"}
TEXT_EN = {"featuring": "Featuring", "thanks": "Thanks for watching!", "subscribe": "Subscribe for the next one"}
TEXT_NL = {"featuring": "Met clips van", "thanks": "Bedankt voor het kijken!", "subscribe": "Abonneer voor de volgende"}


def comp_dir(cid: int) -> Path:
    return get_settings().data_dir / "compilations" / str(cid)


def fmt_ts(sec: float) -> str:
    s = int(round(sec))
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def rank_of(ranking: str, idx: int, n: int) -> int | None:
    if ranking == "countdown":
        return n - idx
    if ranking == "top":
        return idx + 1
    return None


def default_title(period: str, streamers: list[str], lang: str, fmt: str = "landscape") -> str:
    when_en = {"dag": "Today", "week": "of the Week", "maand": "of the Month", "alles": "of All Time"}.get(period, "")
    when_nl = {"dag": "van Vandaag", "week": "van de Week", "maand": "van de Maand", "alles": "Aller Tijden"}.get(period, "")
    week = datetime.utcnow().isocalendar()[1]
    who = streamers[0] if len(set(streamers)) == 1 and streamers else "Twitch"
    if fmt == "short":
        n = len(streamers)
        return f"Top {n} *{who}* momenten" if lang == "nl" else f"Ranking Top {n} *{who}* Moments"
    if lang == "nl":
        base = f"Beste {who} Clips {when_nl}".strip()
    else:
        base = f"Best {who} Clips {when_en}".strip()
    return f"{base} #{week}" if period == "week" else base


def _ff_text(s: str, limit: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _drawtext(textfile: str, size: int, x: str, y: str, bold: bool = True, color: str = "white",
              box: bool = False, alpha: str | None = None) -> str:
    font = FONT_BOLD if bold else FONT
    parts = [f"textfile={textfile}", "expansion=none", f"fontsize={size}", f"fontcolor={color}", f"x={x}", f"y={y}"]
    if Path(font).exists():
        parts.insert(0, f"fontfile={font}")
    if box:
        parts += ["box=1", "boxcolor=black@0.62", "boxborderw=16"]
    else:
        parts += ["shadowcolor=black@0.7", "shadowx=2", "shadowy=2"]
    if alpha:
        parts.append(f"alpha='{alpha}'")
    return "drawtext=" + ":".join(parts)


def _fade_alpha(t0: float, t1: float, f: float = 0.35) -> str:
    return (f"if(lt(t,{t0}),0,if(lt(t,{t0 + f}),(t-{t0})/{f},"
            f"if(lt(t,{t1 - f}),1,if(lt(t,{t1}),({t1}-t)/{f},0))))")


def fit_size(text: str, max_size: int, char_w: float, width: int = W - 160) -> int:
    """Lettergrootte zodat de tekst op één regel binnen de breedte past (ruwe schatting per teken)."""
    return max(24, min(max_size, int(width / (char_w * max(1, len(text))))))


def _write(cwd: Path, name: str, text: str) -> str:
    (cwd / name).write_text(text, encoding="utf-8")
    return name


def clip_segment(src: Path, start: float, dur: float, out: Path, cwd: Path, *, rank: int | None,
                 title: str, credit: str, has_audio: bool, tag: str) -> None:
    """Eén clip als 1920x1080-segment met balk linksonder (plaats + titel, streamer)."""
    line1 = (f"#{rank}  " if rank is not None else "") + _ff_text(title, 64)
    t1 = _write(cwd, f"{tag}_l1.txt", line1)
    t2 = _write(cwd, f"{tag}_l2.txt", _ff_text(credit, 60))
    show_end = min(dur - 0.2, 6.0)
    alpha = _fade_alpha(0.4, max(1.2, show_end))
    fo = max(0.0, dur - 0.35)
    vf = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={FPS},format=yuv420p,"
        + _drawtext(t1, 46, "70", "h-200", box=True, alpha=alpha) + ","
        + _drawtext(t2, 32, "72", "h-118", bold=False, color="0xF5D90A", box=True, alpha=alpha) + ","
        f"fade=t=in:st=0:d=0.3,fade=t=out:st={fo:.2f}:d=0.35[v]"
    )
    a_in = "[0:a:0]" if has_audio else "[1:a]"
    af = (f"{a_in}aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
          f"afade=t=in:st=0:d=0.2,afade=t=out:st={fo:.2f}:d=0.35[a]")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostdin",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src.resolve()),
           "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
           "-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}",
           *ENC, str(out.resolve())]
    _run(cmd, cwd=cwd)


def card_segment(bg: Path, bg_at: float, dur: float, out: Path, cwd: Path, *, big: str, small: str,
                 tag: str, w: int = W, h: int = H) -> None:
    """Intro/outro: vervaagd, donker beeld uit een clip met grote tekst in het midden."""
    big, small = _ff_text(big, 60), _ff_text(small, 120)
    t1 = _write(cwd, f"{tag}_big.txt", big)
    t2 = _write(cwd, f"{tag}_small.txt", small)
    big_size = fit_size(big, 92, 0.64, w - 120)
    small_size = fit_size(small, 40, 0.56, w - 120)
    fo = max(0.0, dur - 0.4)
    vf = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={FPS},"
        "boxblur=24:2,eq=brightness=-0.22:saturation=0.8,format=yuv420p,"
        "drawbox=x=0:y=ih/2-190:w=iw:h=330:color=black@0.45:t=fill,"
        + _drawtext(t1, big_size, "(w-text_w)/2", "(h-text_h)/2-50") + ","
        + _drawtext(t2, small_size, "(w-text_w)/2", "(h/2)+60", bold=False, color="0xF5D90A") + ","
        f"fade=t=in:st=0:d=0.4,fade=t=out:st={fo:.2f}:d=0.4[v]"
    )
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostdin",
           "-ss", f"{bg_at:.3f}", "-t", f"{dur:.3f}", "-i", str(bg.resolve()),
           "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
           "-filter_complex", vf, "-map", "[v]", "-map", "1:a", "-t", f"{dur:.3f}",
           *ENC, str(out.resolve())]
    _run(cmd, cwd=cwd)


# ---------------------------------------------------------------- short top-lijst (9:16)
SHORT_MARGIN = 36  # vervaagde rand rond de video
RANK_COLORS = {1: (245, 197, 24), 2: (225, 225, 225), 3: (224, 123, 57)}  # goud, zilver, brons
TITLE_ACCENT = (64, 220, 72)  # 'Top 5' groen
TITLE_MARK = (235, 40, 40)  # *woord* rood
_EMOJI = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F]")


def _font(size: int, bold: bool = True):
    from PIL import ImageFont

    path = FONT_BOLD if bold else FONT
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def _title_words(title: str) -> list[tuple[str, tuple[int, int, int]]]:
    """Woorden met kleur: 'Top <n>' groen, *woord* rood, de rest wit."""
    words = _EMOJI.sub("", title or "").split()
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if w.lower() == "top" and i + 1 < len(words) and words[i + 1].isdigit():
            out += [(w, TITLE_ACCENT), (words[i + 1], TITLE_ACCENT)]
            i += 2
            continue
        if len(w) > 2 and w.startswith("*") and w.endswith("*"):
            out.append((w.strip("*"), TITLE_MARK))
        else:
            out.append((w.replace("*", ""), (255, 255, 255)))
        i += 1
    return out


def _wrap_words(words, font, max_w: int, draw) -> list[list[tuple[str, tuple]]]:
    lines, cur = [], []
    for wd in words:
        trial = cur + [wd]
        if cur and draw.textlength(" ".join(x for x, _ in trial), font=font) > max_w:
            lines.append(cur)
            cur = [wd]
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def short_overlay(path: Path, title: str, list_lines: list[tuple[int, str, str]], credit: str,
                  list_area: tuple[int, int] | None = None) -> None:
    """Tekstlaag (PNG met transparantie): titel bovenaan en links het genummerde lijstje.

    Grote witte letters met een dikke zwarte rand, zoals in populaire 'Ranking top 5'-shorts;
    nog niet getoonde plaatsen tonen alleen het nummer.
    """
    from PIL import Image, ImageDraw

    w, h = SHORT_W, SHORT_H
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # titel: maximaal 2 regels, zo groot als past
    words = _title_words(title) or [("Top clips", (255, 255, 255))]
    for size in (96, 88, 80, 72, 64, 56, 48):
        f = _font(size)
        lines = _wrap_words(words, f, w - 2 * 60, d)
        if len(lines) <= 2:
            break
    lines = lines[:2]
    y = 70
    for line in lines:
        total = d.textlength(" ".join(x for x, _ in line), font=f)
        x = (w - total) / 2
        for word, color in line:
            d.text((x, y), word, font=f, fill=color, stroke_width=max(4, size // 14), stroke_fill=(0, 0, 0))
            x += d.textlength(word + " ", font=f)
        y += int(size * 1.12)
    if credit:
        fc = _font(30, bold=False)
        d.text((w - SHORT_MARGIN - 16 - d.textlength(credit, font=fc), y + 6), credit, font=fc,
               fill=(255, 255, 255, 220), stroke_width=2, stroke_fill=(0, 0, 0))
    # lijstje links: nummer in medaillekleur, getoonde clips met hun titel
    if list_lines:
        n = len(list_lines)
        lo, hi = list_area if list_area else (330, int(h * 0.62))
        top = max(y + 70, lo)
        step = min(165, int((hi - top) / max(1, n - 1))) if n > 1 else 0
        num_size = 84 if n <= 5 else (72 if n <= 7 else 60)
        num_size = max(40, min(num_size, int(step * 0.62))) if n > 1 else num_size
        txt_size = int(num_size * 0.62)
        fn, ft = _font(num_size), _font(txt_size)
        for i, (rank, text, state) in enumerate(list_lines):
            yy = top + i * step
            label = f"{rank}."
            d.text((SHORT_MARGIN + 18, yy), label, font=fn, fill=RANK_COLORS.get(rank, (255, 255, 255)),
                   stroke_width=6, stroke_fill=(0, 0, 0))
            if state != "nog":
                tx = SHORT_MARGIN + 18 + d.textlength(label + " ", font=fn)
                room = w - tx - SHORT_MARGIN - 16
                t = _EMOJI.sub("", text or "").strip()
                while t and d.textlength(t, font=ft) > room:
                    t = t[:-2].rstrip() + "…" if len(t) > 2 else ""
                d.text((tx, yy + (num_size - txt_size) * 0.55), t, font=ft, fill=(255, 255, 255),
                       stroke_width=5, stroke_fill=(0, 0, 0))
    img.save(path)


def short_split(src_w: int, src_h: int, cam: tuple[float, float, float, float], cam_pos: str,
                game_x: float = 0.5) -> tuple[str, int]:
    """Filter voor 'gestapeld' in een short (binnen de vervaagde rand): facecam en gameplay onder elkaar.

    Geeft (filter van [fga] naar [fg], hoogte van het facecam-vak) terug.
    """
    fw, fh = SHORT_W - 2 * SHORT_MARGIN, SHORT_H - 2 * SHORT_MARGIN
    x, y, cw, ch = (max(0.0, min(1.0, float(v))) for v in cam)
    cx, cy = int(x * src_w) // 2 * 2, int(y * src_h) // 2 * 2
    cwp, chp = max(16, int(cw * src_w) // 2 * 2), max(16, int(ch * src_h) // 2 * 2)
    cwp, chp = min(cwp, src_w - cx), min(chp, src_h - cy)
    top_h = int(max(0.28 * fh, min(0.45 * fh, fw * chp / cwp))) // 2 * 2
    game_h = fh - top_h
    gx = max(0.0, min(1.0, game_x))
    parts = (
        f"[fga]split=2[ca][ga];"
        f"[ca]crop={cwp}:{chp}:{cx}:{cy},scale={fw}:{top_h}:force_original_aspect_ratio=increase,"
        f"crop={fw}:{top_h},setsar=1[cam];"
        f"[ga]scale={fw}:{game_h}:force_original_aspect_ratio=increase,"
        f"crop={fw}:{game_h}:(iw-{fw})*{gx:.3f}:(ih-{game_h})/2,setsar=1[game];"
    )
    order = "[cam][game]" if cam_pos == "top" else "[game][cam]"
    return parts + f"{order}vstack=inputs=2[fg]", top_h


def short_segment(src: Path, start: float, dur: float, out: Path, cwd: Path, *, title: str,
                  list_lines: list[tuple[int, str, str]], credit: str, has_audio: bool, tag: str,
                  layout: dict | None = None, src_size: tuple[int, int] | None = None) -> None:
    """Eén clip als staand 1080x1920-segment met een smalle vervaagde rand, titel en genummerd lijstje.

    layout: {'mode': 'fill', 'crop_x': 0..1} (uitsnede, positie links-rechts) of
    {'mode': 'split', 'cam': (x,y,w,h), 'cam_pos': 'top'|'bottom', 'crop_x': ...} (facecam + gameplay).
    """
    w, h = SHORT_W, SHORT_H
    lay = layout or {"mode": "fill", "crop_x": 0.5}
    fw, fh = w - 2 * SHORT_MARGIN, h - 2 * SHORT_MARGIN
    gx = max(0.0, min(1.0, float(lay.get("crop_x", 0.5))))
    list_area = None
    if lay.get("mode") == "custom" and src_size:
        fg, list_area = custom_short_filter(src_size[0], src_size[1], lay["layout"])
    elif lay.get("mode") == "split" and lay.get("cam") and src_size:
        fg, top_h = short_split(src_size[0], src_size[1], lay["cam"], lay.get("cam_pos", "bottom"), gx)
        if lay.get("cam_pos") == "top":
            list_area = (SHORT_MARGIN + top_h + 30, int(h * 0.82))
        else:
            list_area = (330, SHORT_MARGIN + (fh - top_h) - 90)
    else:
        fg = (f"[fga]scale={fw}:{fh}:force_original_aspect_ratio=increase,"
              f"crop={fw}:{fh}:(iw-{fw})*{gx:.3f}:(ih-{fh})/2,setsar=1[fg]")
    ov = cwd / f"{tag}_overlay.png"
    short_overlay(ov, title, list_lines, credit, list_area)
    fo = max(0.0, dur - 0.25)
    vf = (
        "[0:v]split=2[bga][fga];"
        f"[bga]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
        "boxblur=24:2,eq=brightness=-0.1:saturation=0.8[bg];"
        + fg + ";"
        f"[bg][fg]overlay={SHORT_MARGIN}:{SHORT_MARGIN}[base];"
        f"[base][2:v]overlay=0:0,fps={FPS},format=yuv420p,"
        f"fade=t=in:st=0:d=0.2,fade=t=out:st={fo:.2f}:d=0.25[v]"
    )
    a_in = "[0:a:0]" if has_audio else "[1:a]"
    af = (f"{a_in}aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
          f"afade=t=in:st=0:d=0.15,afade=t=out:st={fo:.2f}:d=0.25[a]")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostdin",
           "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src.resolve()),
           "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
           "-loop", "1", "-t", f"{dur:.3f}", "-i", ov.name,
           "-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}",
           *ENC, str(out.resolve())]
    _run(cmd, cwd=cwd)


def short_total(comp: Compilation) -> float:
    """Lengte van een short: de clips plus een eventuele intro/outro."""
    items = comp.included[:MAX_SHORT_ITEMS]
    return (sum(i.length for i in items) + (INTRO_SECONDS if comp.intro else 0.0)
            + (OUTRO_SECONDS if comp.outro else 0.0))


def item_source(cid: int, it: CompilationItem) -> Path | None:
    """Al gedownload bronbestand van deze clip (of None)."""
    d = comp_dir(cid) / "clips" / it.slug
    from app.pipeline.download import SOURCE_NAME

    files = [p for p in d.glob("source.*") if SOURCE_NAME.fullmatch(p.name)] if d.is_dir() else []
    return files[0] if files else None


_dl_locks: dict[tuple[int, str], threading.Lock] = {}
_dl_locks_guard = threading.Lock()


def ensure_source(cid: int, it: CompilationItem) -> Path:
    """Bronbestand van deze clip, zo nodig eerst downloaden (blijft bewaard voor de editor en opnieuw renderen).

    Eén download tegelijk per clip: de editor en de render kunnen hem allebei vragen.
    """
    with _dl_locks_guard:
        lock = _dl_locks.setdefault((cid, it.slug), threading.Lock())
    with lock:
        return item_source(cid, it) or download(it.clip_url, comp_dir(cid) / "clips" / it.slug)[0]


_fetching: set[tuple[int, int]] = set()
_fetch_errors: dict[tuple[int, int], str] = {}
_fetch_lock = threading.Lock()


def fetch_in_background(cid: int, iid: int) -> None:
    """Download de clip voor de editor op de achtergrond (één keer tegelijk per clip)."""
    with _fetch_lock:
        if (cid, iid) in _fetching:
            return
        _fetching.add((cid, iid))
        _fetch_errors.pop((cid, iid), None)

    def work():
        from app.core.db import new_session

        db = new_session()
        try:
            it = db.get(CompilationItem, iid)
            if it is not None:
                ensure_source(cid, it)
        except Exception as exc:  # noqa: BLE001
            log.warning("Clip %s voor de editor ophalen mislukt: %s", iid, type(exc).__name__)
            known = isinstance(exc, (DownloadBlocked, DownloadFailed))
            with _fetch_lock:
                _fetch_errors[(cid, iid)] = str(exc)[:300] if known else f"Onverwachte fout ({type(exc).__name__})."
        finally:
            db.close()
            with _fetch_lock:
                _fetching.discard((cid, iid))

    threading.Thread(target=work, daemon=True, name=f"fetch-{iid}").start()


def fetch_error(cid: int, iid: int) -> str | None:
    with _fetch_lock:
        return _fetch_errors.get((cid, iid))


def is_fetching(cid: int, iid: int) -> bool:
    with _fetch_lock:
        return (cid, iid) in _fetching


def custom_short_filter(src_w: int, src_h: int, layout: dict) -> tuple[str, tuple[int, int] | None]:
    """Eigen indeling uit de editor ({'type': 'crop'|'split', ...}) als filter naar [fg] binnen de rand."""
    fw, fh = SHORT_W - 2 * SHORT_MARGIN, SHORT_H - 2 * SHORT_MARGIN

    def px(r):
        x, y, w, h = (max(0.0, min(1.0, float(v))) for v in r)
        cx, cy = int(x * src_w) // 2 * 2, int(y * src_h) // 2 * 2
        cw = max(16, min(src_w - cx, int(w * src_w)) // 2 * 2)
        ch = max(16, min(src_h - cy, int(h * src_h)) // 2 * 2)
        return cw, ch, cx, cy

    def fit(r, ow, oh, tag_in, tag_out):
        cw, ch, cx, cy = px(r)
        return (f"[{tag_in}]crop={cw}:{ch}:{cx}:{cy},scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},setsar=1[{tag_out}]")

    if layout.get("type") == "split":
        top_h = int(fh * max(0.2, min(0.6, float(layout.get("top", 0.35))))) // 2 * 2
        f = ("[fga]split=2[ca][ga];" + fit(layout["cam"], fw, top_h, "ca", "cam") + ";"
             + fit(layout["game"], fw, fh - top_h, "ga", "game") + ";[cam][game]vstack=inputs=2[fg]")
        return f, (SHORT_MARGIN + top_h + 30, int(SHORT_H * 0.82))
    return fit(layout["rect"], fw, fh, "fga", "fg"), None


def item_layout(it: CompilationItem, src: Path, info: dict, start: float, end: float) -> tuple[dict, str | None]:
    """Indeling voor een clip in een short. Bij 'gestapeld' zonder eigen kader: facecam automatisch zoeken."""
    from app.pipeline import framing

    custom = None
    if getattr(it, "layout_json", None):
        try:
            custom = framing.validate_layout(json.loads(it.layout_json))
        except ValueError:
            custom = None
    if custom is not None:
        return {"mode": "custom", "layout": custom}, None
    gx = it.crop_x if it.crop_x is not None else 0.5
    if (it.layout or "fill") != "split":
        return {"mode": "fill", "crop_x": gx}, None
    from app.pipeline.facecam import parse_box

    cam = parse_box(it.cam_box) if it.cam_box else None
    note = None
    if cam is None:
        from app.pipeline import framing

        try:
            fr = framing.choose_framing(src, info["width"], info["height"], start, end, force_mode="split")
            if fr.mode == "split" and fr.layout:
                cam = tuple(fr.layout["cam"])
        except Exception:  # noqa: BLE001
            cam = None
        if cam is None:
            return {"mode": "fill", "crop_x": gx}, "geen facecam gevonden: gevuld weergegeven"
        note = "facecam automatisch gevonden"
    return {"mode": "split", "cam": cam, "cam_pos": it.cam_pos or "bottom", "crop_x": gx}, note


def short_list(ranking: str, items: list[CompilationItem], k: int) -> list[tuple[int, str, str]]:
    """Het lijstje bij clip k: plaats 1 bovenaan; al getoonde clips met titel, de rest '???'."""
    n = len(items)
    if ranking == "none":
        return []
    by_rank = {rank_of(ranking, i, n): (i, it) for i, it in enumerate(items)}
    out = []
    for r in range(1, n + 1):
        i, it = by_rank[r]
        state = "nu" if i == k else ("gezien" if i < k else "nog")
        out.append((r, it.title, state))
    return out


def concat(segments: list[Path], out: Path, cwd: Path) -> None:
    lst = cwd / "concat.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in segments), encoding="utf-8")
    _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i", lst.name,
          "-c", "copy", "-movflags", "+faststart", str(out.resolve())], cwd=cwd)


def build_chapters(starts: list[tuple[float, str]]) -> list[dict]:
    """Hoofdstukken voor YouTube: eerste op 0:00, minstens 10 s uit elkaar, minstens 3 stuks."""
    out: list[dict] = []
    for t, label in starts:
        t = 0.0 if not out else t
        if out and t - out[-1]["t"] < CHAPTER_MIN_GAP:
            continue
        out.append({"t": round(t, 2), "label": label})
    return out if len(out) >= 3 else []


def item_label(item: CompilationItem, rank: int | None) -> str:
    base = f"{item.title} ({item.streamer})" if item.streamer else item.title
    return (f"#{rank} " if rank is not None else "") + base


# ---------------------------------------------------------------- tekst
def _lang(db: Session, owner_id: int) -> str:
    from app.core.store import user_settings

    lang = user_settings(db, owner_id).post_language or "en"
    return "nl" if lang == "nl" else "en"


def _hashtags(raw, short: bool = False) -> str:
    """Hashtags netjes maken. Lange video: nooit #shorts; short: altijd #shorts."""
    items = re.split(r"[\s,]+", raw) if isinstance(raw, str) else [str(x) for x in (raw or [])]
    tags: list[str] = ["#shorts"] if short else []
    for t in items:
        t = re.sub(r"[^\w]", "", t.strip().lstrip("#"), flags=re.UNICODE)
        if t and t.lower() != "shorts" and ("#" + t).lower() not in {x.lower() for x in tags}:
            tags.append("#" + t)
    return " ".join(tags[:6 if short else 5])


def compose_description(intro: str, comp: Compilation) -> str:
    """AI-tekst + hoofdstukken + credits per streamer + hashtags."""
    parts = [intro.strip()] if intro and intro.strip() else []
    if comp.chapters:
        parts.append("\n".join(f"{fmt_ts(c['t'])} {c['label']}" for c in comp.chapters))
    seen, credits = set(), []
    for it in comp.included:
        if it.channel_login and it.channel_login not in seen:
            seen.add(it.channel_login)
            credits.append(f"{it.streamer or it.channel_login}: https://twitch.tv/{it.channel_login}")
    if credits:
        parts.append("Clips:\n" + "\n".join(credits))
    if comp.hashtags:
        parts.append(comp.hashtags)
    return "\n\n".join(parts)


def comp_trends(db: Session, comp: Compilation) -> str:
    """Promptregels met wat nu werkt op YouTube voor de game/streamer(s) van deze compilatie."""
    from collections import Counter

    from app.estimation import trends

    items = comp.included
    games = Counter(i.game for i in items if i.game)
    streamers = Counter(i.streamer for i in items if i.streamer)
    meta = {"game": games.most_common(1)[0][0] if games else "",
            "streamer": streamers.most_common(1)[0][0] if len(streamers) == 1 else ""}
    try:
        return trends.prompt_text(trends.fetch(db, comp.owner_id, trends.query_from_meta(meta, comp.title.replace("*", "")),
                                               short=comp.format == "short"))
    except Exception:  # noqa: BLE001
        log.warning("Trends voor compilatie %s niet beschikbaar", comp.id)
        return ""


def generate_text(db: Session, comp: Compilation) -> tuple[str, str, str, str]:
    """(titel, intro-tekst, hashtags, verborgen tags). Met LLM als die is ingesteld, anders een nette standaard."""
    from app.estimation.trends import normalize_tags

    from app.core.keys import load_llm_config
    from app.pipeline import pick
    from app.pipeline.errors import LLMError

    lang = _lang(db, comp.owner_id)
    items = comp.included
    n = len(items)
    streamers = sorted({i.streamer for i in items if i.streamer})
    games = sorted({i.game for i in items if i.game})
    short = comp.format == "short"
    fallback_title = (comp.title or default_title(comp.period, [i.streamer for i in items], lang, comp.format)).replace("*", "")
    if lang == "nl":
        fallback_intro = (f"De {n} beste clips van {', '.join(streamers[:5]) or 'Twitch'}"
                          + (f" in {', '.join(games[:3])}" if games else "") + ". Welke was jouw favoriet?")
    else:
        fallback_intro = (f"The top {n} clips from {', '.join(streamers[:5]) or 'Twitch'}"
                          + (f" in {', '.join(games[:3])}" if games else "") + ". Which one was your favorite?")
    fallback_tags = _hashtags(short=short, raw=["twitch", "twitchclips"] + [s.replace(" ", "") for s in streamers[:2]]
                              + [g.replace(" ", "") for g in games[:1]])
    fallback_yt = normalize_tags(["twitch clips"] + streamers[:3] + games[:2]
                                 + [f"{s} {g}" for s in streamers[:1] for g in games[:1]])
    cfg = load_llm_config(db, "copy")
    if cfg is None:
        return fallback_title, fallback_intro, fallback_tags, fallback_yt
    lines = "\n".join(
        f"- {item_label(it, rank_of(comp.ranking, k, n))} | {it.views} views" + (f" | game: {it.game}" if it.game else "")
        for k, it in enumerate(items))
    language = "Nederlands" if lang == "nl" else "English"
    trend = comp_trends(db, comp)
    messages = [
        {"role": "system", "content": "You write YouTube titles and descriptions for Twitch clip compilations "
                                      + ("(a vertical YouTube Short with a top-N ranking). " if short else "(long-form, landscape). ")
                                      + "Answer only with JSON."},
        {"role": "user", "content": (
            f"Language: {language}. Write for this compilation:\n"
            f"- title: max {60 if short else 80} characters, catchy but honest, no clickbait lies, may mention the streamer(s) or game; "
            f"working title is '{fallback_title}'.\n"
            "- description: 2-3 short sentences that make people want to watch; do NOT list chapters or links "
            "(those are added automatically).\n"
            + ("- hashtags: 3-5 relevant hashtags.\n" if short else "- hashtags: 3-5 relevant hashtags (no #shorts).\n")
            + "- tags: 8-15 hidden YouTube tags (no #): game, streamer names (incl. common spellings), what happens, "
            "search terms people really use.\n"
            + 'JSON: {"title": "...", "description": "...", "hashtags": ["#..."], "tags": ["..."]}\n\n'
            + (trend + "\n\n" if trend else "") + "Clips in order:\n" + lines)},
    ]
    try:
        out = pick.chat_json(cfg, messages, retries=2, timeout=60.0)
    except LLMError as exc:
        log.warning("Compilatietekst via LLM mislukt: %s", exc)
        return fallback_title, fallback_intro, fallback_tags, fallback_yt
    title = _ff_text(str(out.get("title") or ""), 100) or fallback_title
    intro = str(out.get("description") or "").strip()[:1500] or fallback_intro
    tags = _hashtags(out.get("hashtags"), short) or fallback_tags
    return title, intro, tags, normalize_tags(out.get("tags")) or fallback_yt


# ---------------------------------------------------------------- render
def _set(db: Session, comp: Compilation, progress: int, msg: str) -> None:
    comp.progress = progress
    comp.stage_message = msg
    comp.updated_at = utcnow()
    db.commit()


def render_compilation(db: Session, cid: int) -> None:
    comp = db.get(Compilation, cid)
    if comp is None:
        return
    short = comp.format == "short"
    items = comp.included[: MAX_SHORT_ITEMS if short else MAX_ITEMS]
    if len(items) < 2:
        raise RenderError("Kies minstens 2 clips voor een compilatie.")
    if short and short_total(comp) > SHORT_MAX_SECONDS + 0.5:
        raise RenderError(f"Een short mag maximaal {SHORT_MAX_SECONDS // 60} minuten duren. Kort clips in of haal er een weg.")
    fw, fh = (SHORT_W, SHORT_H) if short else (W, H)
    base = comp_dir(cid)
    work = base / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    lang = _lang(db, comp.owner_id)
    words = TEXT_NL if lang == "nl" else TEXT_EN
    n = len(items)

    # 1. downloaden (blijft bewaard, zodat opnieuw renderen snel is)
    sources: dict[int, tuple[Path, dict]] = {}
    for k, it in enumerate(items):
        _set(db, comp, int(5 + 40 * k / n), f"Clip {k + 1} van {n} downloaden")
        path = ensure_source(cid, it)
        info = media.probe(path)
        if not it.duration:
            it.duration = info["duration"]
        sources[it.id] = (path, info)

    # 2. segmenten
    segments: list[Path] = []
    starts: list[tuple[float, str]] = []
    t = 0.0
    top_item = items[-1] if comp.ranking == "countdown" else items[0]
    top_src, top_info = sources[top_item.id]
    bg_at = min(max(0.0, top_item.trim_start + 1.0), max(0.0, top_info["duration"] - INTRO_SECONDS - 0.1))
    streamers = list(dict.fromkeys(i.streamer for i in items if i.streamer))
    if comp.intro:
        _set(db, comp, 46, "Intro maken")
        seg = work / "seg_intro.mp4"
        card_segment(top_src, bg_at, INTRO_SECONDS, seg, work, big=(comp.title or "Twitch Clips").replace("*", ""),
                     small=f"{words['featuring']} {', '.join(streamers[:6])}" if streamers else "", tag="intro",
                     w=fw, h=fh)
        segments.append(seg)
        t += media.probe(seg)["duration"]
    for k, it in enumerate(items):
        _set(db, comp, int(48 + 45 * k / n), f"Clip {k + 1} van {n} renderen")
        src, info = sources[it.id]
        start = max(0.0, min(it.trim_start or 0.0, info["duration"] - 1.0))
        end = it.trim_end if it.trim_end is not None else info["duration"]
        end = min(end, info["duration"])
        dur = max(1.0, end - start)
        rank = rank_of(comp.ranking, k, n)
        seg = work / f"seg_{k:02d}.mp4"
        credit = f"twitch.tv/{it.channel_login}" if it.channel_login else it.streamer
        if short:
            lay, _note = item_layout(it, src, info, start, start + dur)
            short_segment(src, start, dur, seg, work, title=comp.title or "Top clips",
                          list_lines=short_list(comp.ranking, items, k), credit=credit,
                          has_audio=info["has_audio"], tag=f"c{k:02d}",
                          layout=lay, src_size=(info["width"], info["height"]))
        else:
            clip_segment(src, start, dur, seg, work, rank=rank, title=it.title, credit=credit,
                         has_audio=info["has_audio"], tag=f"c{k:02d}")
        segments.append(seg)
        starts.append((t, item_label(it, rank)))
        t += media.probe(seg)["duration"]
    if comp.outro:
        _set(db, comp, 94, "Outro maken")
        seg = work / "seg_outro.mp4"
        card_segment(top_src, bg_at, OUTRO_SECONDS, seg, work, big=words["thanks"],
                     small=(f"{words['featuring']} " + " · ".join(streamers[:8])) if streamers else words["subscribe"],
                     tag="outro", w=fw, h=fh)
        segments.append(seg)

    # 3. aan elkaar plakken
    _set(db, comp, 96, "Samenvoegen")
    out = base / "compilation.mp4"
    concat(segments, out, work)
    info = media.probe(out)
    if (info["width"], info["height"]) != (fw, fh):
        raise RenderError(f"Onverwachte resolutie {info['width']}x{info['height']}.")
    thumb = base / "thumb.jpg"
    top_index = items.index(top_item)
    top_start = next((s for s, _ in starts[top_index:top_index + 1]), 0.0)
    media.thumbnail(out, thumb, at=top_start + 2.0, width=720 if short else 1280)
    comp.file_path = out.name
    comp.thumb_path = thumb.name if thumb.exists() else None
    comp.duration = info["duration"]
    comp.chapters_json = json.dumps([] if short else build_chapters(starts), ensure_ascii=False)
    shutil.rmtree(work, ignore_errors=True)

    # 4. titel en beschrijving (alleen invullen als de gebruiker nog niets eigens heeft)
    _set(db, comp, 98, "Titel en beschrijving schrijven")
    if not comp.description:
        title, intro, tags, yt_tags = generate_text(db, comp)
        comp.yt_title = comp.yt_title or title
        comp.hashtags = comp.hashtags or tags
        comp.yt_tags = comp.yt_tags or yt_tags
        comp.description = compose_description(intro, comp)
    comp.status = "done"
    comp.progress = 100
    comp.stage_message = "Klaar"
    comp.error = None
    comp.finished_at = utcnow()
    db.commit()


# ---------------------------------------------------------------- wachtrij (worker)
_lock = threading.Lock()
MAX_ATTEMPTS = 2


def _claim(db: Session, field: str, waiting: str, busy: str) -> Compilation | None:
    with _lock:
        col = getattr(Compilation, field)
        comp = db.scalar(select(Compilation).where(col == waiting).order_by(Compilation.updated_at, Compilation.id).limit(1))
        if comp is None:
            return None
        setattr(comp, field, busy)
        if field == "status":
            comp.attempts = (comp.attempts or 0) + 1
        db.commit()
        return comp


def run_next_render(db: Session) -> bool:
    comp = _claim(db, "status", "queued", "rendering")
    if comp is None:
        return False
    cid = comp.id
    log.info("Compilatie %s renderen", cid)
    try:
        render_compilation(db, cid)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        comp = db.get(Compilation, cid)
        if comp is not None:
            comp.status = "failed"
            known = isinstance(exc, (RenderError, DownloadBlocked, DownloadFailed))
            comp.error = str(exc)[:800] if known else f"Onverwachte fout ({type(exc).__name__})."
            comp.stage_message = "Mislukt"
            db.commit()
        log.exception("Compilatie mislukt")
    _notify_comp(db, cid, render=True)
    return True


def _notify_comp(db: Session, cid: int, render: bool) -> None:
    """Discord-melding na renderen of uploaden van een compilatie (faalt stil)."""
    from app.integrations import discord

    try:
        comp = db.get(Compilation, cid)
        if comp is None:
            return
        if render:
            if comp.status == "done":
                discord.notify(db, comp.owner_id, f"Compilatie klaar: {comp.title.replace('*', '')[:150]}",
                               f"{len(comp.included)} clips · {fmt_ts(comp.duration or 0)}", "ok", f"/compilations/{cid}")
            elif comp.status == "failed":
                discord.notify(db, comp.owner_id, f"Compilatie mislukt: {comp.title.replace('*', '')[:150]}",
                               comp.error or "", "error", f"/compilations/{cid}")
        elif comp.yt_status == "done":
            discord.notify(db, comp.owner_id, f"Op YouTube: {(comp.yt_title or comp.title)[:150]}",
                           f"https://www.youtube.com/watch?v={comp.yt_video_id}", "ok")
        elif comp.yt_status == "failed":
            discord.notify(db, comp.owner_id, f"Upload mislukt: {(comp.yt_title or comp.title)[:150]}",
                           comp.yt_error or "", "error", f"/compilations/{cid}")
    except Exception:  # noqa: BLE001
        log.warning("Discord-melding voor compilatie %s mislukt", cid)


def run_next_upload(db: Session) -> bool:
    comp = _claim(db, "yt_status", "queued", "uploading")
    if comp is None:
        return False
    cid = comp.id
    try:
        run_upload(db, cid)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        comp = db.get(Compilation, cid)
        if comp is not None:
            comp.yt_status = "failed"
            comp.yt_error = f"Onverwachte fout ({type(exc).__name__})."
            db.commit()
        log.exception("Compilatie-upload mislukt")
    _notify_comp(db, cid, render=False)
    return True


def recover(db: Session) -> None:
    """Na een herstart: onderbroken renders opnieuw (max. 2x), onderbroken uploads als mislukt."""
    for comp in db.scalars(select(Compilation).where(Compilation.status == "rendering")):
        if (comp.attempts or 0) >= MAX_ATTEMPTS:
            comp.status = "failed"
            comp.error = "De server stopte onverwacht tijdens het renderen, ook bij een tweede poging."
        else:
            comp.status = "queued"
    for comp in db.scalars(select(Compilation).where(Compilation.yt_status == "uploading")):
        comp.yt_status = "failed"
        comp.yt_error = "Upload onderbroken door een herstart. Controleer YouTube Studio en probeer het opnieuw."
    db.commit()


def run_upload(db: Session, cid: int) -> bool:
    from datetime import timedelta

    from app.api.youtube import account_for
    from app.core.paths import safe_child
    from app.integrations import youtube_upload as yt

    comp = db.get(Compilation, cid)
    if comp is None:
        return True
    acc = account_for(db, comp.owner_id)
    path = safe_child(comp_dir(cid), comp.file_path)
    opts = json.loads(comp.yt_options_json or "{}")
    if acc is None or path is None or not opts:
        comp.yt_status = "failed"
        comp.yt_error = "Geen gekoppeld kanaal of geen gerenderde video."
        db.commit()
        return False
    note = None
    if opts.get("publish_at"):
        try:
            when = datetime.strptime(opts["publish_at"], "%Y-%m-%dT%H:%M:%SZ")
            if when <= datetime.utcnow() + timedelta(minutes=2):
                opts["publish_at"], opts["privacy"] = None, "private"
                note = "De geplande publicatietijd was al voorbij; de video is privé geüpload."
        except ValueError:
            opts["publish_at"] = None
    from app.estimation.trends import upload_tags

    tags = upload_tags(comp.yt_tags, comp.hashtags)
    try:
        res = yt.upload(db, acc, path, title=opts.get("title") or comp.yt_title or comp.title,
                        description=opts.get("description") or "", privacy=opts.get("privacy", "private"),
                        publish_at=opts.get("publish_at"), made_for_kids=bool(opts.get("made_for_kids")),
                        category_id="20", tags=tags)
    except yt.YoutubeError as exc:
        comp.yt_status = "failed"
        comp.yt_error = str(exc)
        db.commit()
        return False
    comp.yt_status = "done"
    comp.yt_video_id = res["id"]
    comp.yt_error = note
    if res.get("privacy") == "private" and opts.get("privacy") != "private" and not opts.get("publish_at"):
        comp.yt_error = ("YouTube heeft de video privé gezet (API-project nog niet geaudit); "
                         "zet hem in YouTube Studio op openbaar.")
    db.commit()
    return True
