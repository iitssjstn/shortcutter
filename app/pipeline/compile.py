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
MAX_ITEMS = 30
CHAPTER_MIN_GAP = 10  # YouTube: hoofdstukken minstens 10 s
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-video_track_timescale", "15360", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
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


def default_title(period: str, streamers: list[str], lang: str) -> str:
    when_en = {"dag": "Today", "week": "of the Week", "maand": "of the Month", "alles": "of All Time"}.get(period, "")
    when_nl = {"dag": "van Vandaag", "week": "van de Week", "maand": "van de Maand", "alles": "Aller Tijden"}.get(period, "")
    week = datetime.utcnow().isocalendar()[1]
    who = streamers[0] if len(set(streamers)) == 1 and streamers else "Twitch"
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
                 tag: str) -> None:
    """Intro/outro: vervaagd, donker beeld uit een clip met grote tekst in het midden."""
    big, small = _ff_text(big, 60), _ff_text(small, 120)
    t1 = _write(cwd, f"{tag}_big.txt", big)
    t2 = _write(cwd, f"{tag}_small.txt", small)
    big_size = fit_size(big, 92, 0.64)
    small_size = fit_size(small, 40, 0.56)
    fo = max(0.0, dur - 0.4)
    vf = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},"
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


def _hashtags(raw) -> str:
    items = re.split(r"[\s,]+", raw) if isinstance(raw, str) else [str(x) for x in (raw or [])]
    tags: list[str] = []
    for t in items:
        t = re.sub(r"[^\w]", "", t.strip().lstrip("#"), flags=re.UNICODE)
        if t and t.lower() != "shorts" and ("#" + t).lower() not in {x.lower() for x in tags}:
            tags.append("#" + t)
    return " ".join(tags[:5])


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


def generate_text(db: Session, comp: Compilation) -> tuple[str, str, str]:
    """(titel, intro-tekst, hashtags). Met LLM als die is ingesteld, anders een nette standaard."""
    from app.core.keys import load_llm_config
    from app.pipeline import pick
    from app.pipeline.errors import LLMError

    lang = _lang(db, comp.owner_id)
    items = comp.included
    n = len(items)
    streamers = sorted({i.streamer for i in items if i.streamer})
    games = sorted({i.game for i in items if i.game})
    fallback_title = comp.title or default_title(comp.period, [i.streamer for i in items], lang)
    if lang == "nl":
        fallback_intro = (f"De {n} beste clips van {', '.join(streamers[:5]) or 'Twitch'}"
                          + (f" in {', '.join(games[:3])}" if games else "") + ". Welke was jouw favoriet?")
    else:
        fallback_intro = (f"The top {n} clips from {', '.join(streamers[:5]) or 'Twitch'}"
                          + (f" in {', '.join(games[:3])}" if games else "") + ". Which one was your favorite?")
    fallback_tags = _hashtags(["twitch", "twitchclips"] + [s.replace(" ", "") for s in streamers[:2]]
                              + [g.replace(" ", "") for g in games[:1]])
    cfg = load_llm_config(db, "copy")
    if cfg is None:
        return fallback_title, fallback_intro, fallback_tags
    lines = "\n".join(
        f"- {item_label(it, rank_of(comp.ranking, k, n))} | {it.views} views" + (f" | game: {it.game}" if it.game else "")
        for k, it in enumerate(items))
    language = "Nederlands" if lang == "nl" else "English"
    messages = [
        {"role": "system", "content": "You write YouTube titles and descriptions for Twitch clip compilations "
                                      "(long-form, landscape). Answer only with JSON."},
        {"role": "user", "content": (
            f"Language: {language}. Write for this compilation:\n"
            "- title: max 80 characters, catchy but honest, no clickbait lies, may mention the streamer(s) or game; "
            f"working title is '{fallback_title}'.\n"
            "- description: 2-3 short sentences that make people want to watch; do NOT list chapters or links "
            "(those are added automatically).\n"
            "- hashtags: 3-5 relevant hashtags (no #shorts).\n"
            'JSON: {"title": "...", "description": "...", "hashtags": ["#..."]}\n\nClips in order:\n' + lines)},
    ]
    try:
        out = pick.chat_json(cfg, messages, retries=2, timeout=60.0)
    except LLMError as exc:
        log.warning("Compilatietekst via LLM mislukt: %s", exc)
        return fallback_title, fallback_intro, fallback_tags
    title = _ff_text(str(out.get("title") or ""), 100) or fallback_title
    intro = str(out.get("description") or "").strip()[:1500] or fallback_intro
    tags = _hashtags(out.get("hashtags")) or fallback_tags
    return title, intro, tags


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
    items = comp.included[:MAX_ITEMS]
    if len(items) < 2:
        raise RenderError("Kies minstens 2 clips voor een compilatie.")
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
        d = base / "clips" / it.slug
        existing = [p for p in d.glob("source.*")] if d.is_dir() else []
        path = existing[0] if existing else download(it.clip_url, d)[0]
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
        card_segment(top_src, bg_at, INTRO_SECONDS, seg, work, big=comp.title or "Twitch Clips",
                     small=f"{words['featuring']} {', '.join(streamers[:6])}" if streamers else "", tag="intro")
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
        clip_segment(src, start, dur, seg, work, rank=rank, title=it.title,
                     credit=f"twitch.tv/{it.channel_login}" if it.channel_login else it.streamer,
                     has_audio=info["has_audio"], tag=f"c{k:02d}")
        segments.append(seg)
        starts.append((t, item_label(it, rank)))
        t += media.probe(seg)["duration"]
    if comp.outro:
        _set(db, comp, 94, "Outro maken")
        seg = work / "seg_outro.mp4"
        card_segment(top_src, bg_at, OUTRO_SECONDS, seg, work, big=words["thanks"],
                     small=(f"{words['featuring']} " + " · ".join(streamers[:8])) if streamers else words["subscribe"],
                     tag="outro")
        segments.append(seg)

    # 3. aan elkaar plakken
    _set(db, comp, 96, "Samenvoegen")
    out = base / "compilation.mp4"
    concat(segments, out, work)
    info = media.probe(out)
    if (info["width"], info["height"]) != (W, H):
        raise RenderError(f"Onverwachte resolutie {info['width']}x{info['height']}.")
    thumb = base / "thumb.jpg"
    top_index = items.index(top_item)
    top_start = next((s for s, _ in starts[top_index:top_index + 1]), 0.0)
    media.thumbnail(out, thumb, at=top_start + 2.0, width=1280)
    comp.file_path = out.name
    comp.thumb_path = thumb.name if thumb.exists() else None
    comp.duration = info["duration"]
    comp.chapters_json = json.dumps(build_chapters(starts), ensure_ascii=False)
    shutil.rmtree(work, ignore_errors=True)

    # 4. titel en beschrijving (alleen invullen als de gebruiker nog niets eigens heeft)
    _set(db, comp, 98, "Titel en beschrijving schrijven")
    if not comp.description:
        title, intro, tags = generate_text(db, comp)
        comp.yt_title = comp.yt_title or title
        comp.hashtags = comp.hashtags or tags
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
    log.info("Compilatie %s renderen", comp.id)
    try:
        render_compilation(db, comp.id)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        comp = db.get(Compilation, comp.id)
        if comp is not None:
            comp.status = "failed"
            known = isinstance(exc, (RenderError, DownloadBlocked, DownloadFailed))
            comp.error = str(exc)[:800] if known else f"Onverwachte fout ({type(exc).__name__})."
            comp.stage_message = "Mislukt"
            db.commit()
        log.exception("Compilatie mislukt")
    return True


def run_next_upload(db: Session) -> bool:
    comp = _claim(db, "yt_status", "queued", "uploading")
    if comp is None:
        return False
    try:
        run_upload(db, comp.id)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        comp = db.get(Compilation, comp.id)
        if comp is not None:
            comp.yt_status = "failed"
            comp.yt_error = f"Onverwachte fout ({type(exc).__name__})."
            db.commit()
        log.exception("Compilatie-upload mislukt")
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
    tags = [t.lstrip("#") for t in (comp.hashtags or "").split() if t.strip("#")]
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
