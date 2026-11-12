from app.pipeline.captions import ass_time, build_ass, build_srt, escape_ass, group_words


def w(word, s, e):
    return {"word": word, "start": s, "end": e}


def test_escape():
    assert escape_ass("a{b}c") == "a\\{b\\}c"
    out = escape_ass("x\\N")
    assert "\\N" not in out  # geen regeleinde-tag
    assert "{" not in escape_ass("{\\b1}").replace("\\{", "")


def test_groups_max_three_words():
    words = [w(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(10)]
    groups = group_words(words, 0, 10)
    assert all(1 <= len(g) <= 3 for g in groups)
    assert sum(len(g) for g in groups) == 10


def test_build_ass_karaoke():
    words = [w("Hallo", 10.0, 10.4), w("{wereld}", 10.5, 11.0), w("test\\n", 11.1, 11.5), w("vier", 11.6, 12.0)]
    ass = build_ass(words, 10.0, 15.0, "geel")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "DejaVu Sans" in ass
    assert "&H0000E5FF" in ass  # gele highlight
    events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 2
    assert events[0].startswith("Dialogue: 0,0:00:00.00,")
    assert "{\\k" in events[0] and "HALLO" in events[0]
    assert "\\{WERELD\\}" in events[0]
    # alleen \k-tags, geen ongewenste tags uit de tekst
    text = events[0].split(",,", 1)[1]
    assert text.count("{\\k") == 3


def test_ass_time_and_srt():
    assert ass_time(3725.456) == "1:02:05.46"
    srt = build_srt([w("Hallo", 1.0, 1.5), w("daar.", 1.6, 2.0)], 1.0, 5.0)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:01,000\nHallo daar.")
