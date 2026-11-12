"""Ondertitels: .ass met woord-voor-woord karaoke (\\k) en .srt voor download."""
from __future__ import annotations

from pathlib import Path

MAX_WORDS = 3
GROUP_GAP = 0.6

# ASS-kleuren zijn &HAABBGGRR
STYLE_COLORS = {
    "geel": ("&H0000E5FF", "&H00FFFFFF"),  # gesproken = geel, nog niet = wit
    "groen": ("&H0047E05A", "&H00FFFFFF"),
    "wit": ("&H00FFFFFF", "&H00FFFFFF"),
}

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,DejaVu Sans,86,{primary},{secondary},&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def escape_ass(text: str) -> str:
    """Escape tekens die in ASS een betekenis hebben: \\ { }."""
    text = text.replace("\\", "\\⁠")  # backslash + word-joiner: vormt nooit een tag
    text = text.replace("{", "\\{").replace("}", "\\}")
    return text.replace("\n", " ").replace("\r", " ")


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def group_words(words: list[dict], clip_start: float, clip_end: float) -> list[list[dict]]:
    """Woorden relatief aan de clipstart, in groepjes van max 3."""
    rel = []
    for w in words:
        s, e = max(w["start"], clip_start), min(w["end"], clip_end)
        if e <= s or not w["word"].strip():
            continue
        rel.append({"start": s - clip_start, "end": e - clip_start, "word": w["word"].strip()})
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in rel:
        if cur and (
            len(cur) >= MAX_WORDS
            or w["start"] - cur[-1]["end"] > GROUP_GAP
            or cur[-1]["word"].endswith((".", "!", "?", ","))
        ):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[dict], clip_start: float, clip_end: float, style: str = "geel") -> str:
    primary, secondary = STYLE_COLORS.get(style, STYLE_COLORS["geel"])
    lines = [HEADER.format(primary=primary, secondary=secondary)]
    groups = group_words(words, clip_start, clip_end)
    for i, g in enumerate(groups):
        start = g[0]["start"]
        end = g[-1]["end"]
        nxt = groups[i + 1][0]["start"] if i + 1 < len(groups) else None
        if nxt is not None:
            end = min(max(end, start + 0.3), nxt)
        parts = []
        cursor = start
        for w in g:
            # wachttijd vóór het woord telt mee bij het vorige
            dur_cs = max(1, int(round((w["end"] - cursor) * 100)))
            parts.append(f"{{\\k{dur_cs}}}{escape_ass(w['word'].upper())}")
            cursor = w["end"]
        text = " ".join(parts)
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Karaoke,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def build_srt(words: list[dict], clip_start: float, clip_end: float) -> str:
    out = []
    # voor .srt: iets langere regels (max 7 woorden) voor leesbaarheid
    rel = [g for g in group_words(words, clip_start, clip_end)]
    merged: list[list[dict]] = []
    for g in rel:
        if merged and len(merged[-1]) + len(g) <= 7 and g[0]["start"] - merged[-1][-1]["end"] < GROUP_GAP \
                and not merged[-1][-1]["word"].endswith((".", "!", "?")):
            merged[-1].extend(g)
        else:
            merged.append(list(g))
    for i, g in enumerate(merged, 1):
        text = " ".join(w["word"] for w in g)
        out.append(f"{i}\n{srt_time(g[0]['start'])} --> {srt_time(g[-1]['end'])}\n{text}\n")
    return "\n".join(out)


def write_ass(path: Path, words: list[dict], clip_start: float, clip_end: float, style: str = "geel") -> Path:
    path.write_text(build_ass(words, clip_start, clip_end, style), encoding="utf-8")
    return path
