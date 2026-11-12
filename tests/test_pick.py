import json

import httpx
import pytest

from app.pipeline import pick
from app.pipeline.errors import LLMError
from app.pipeline.pick import LLMConfig, dedupe, heuristic_pick, llm_pick, pick_clips, sentences, snap_clip


def words_from(text, start=0.0, step=0.5):
    out, t = [], start
    for w in text.split():
        out.append({"start": round(t, 2), "end": round(t + step * 0.8, 2), "word": w})
        t += step
    return out


def long_transcript(n_sentences=40):
    segs, t = [], 0.0
    for i in range(n_sentences):
        ws = words_from(f"Dit is zin nummer {i} met wat extra woorden erin.", t)
        segs.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": " ".join(w["word"] for w in ws), "words": ws})
        t = ws[-1]["end"] + 0.3
    return {"duration": t, "segments": segs}


def test_sentences_split_on_punctuation_and_gap():
    ws = words_from("Hallo daar. Hoe gaat het?") + words_from("Lange pauze hier", start=10)
    s = sentences(ws)
    assert len(s) == 3
    assert s[0] == (0.0, ws[1]["end"])
    assert s[2][0] == 10.0


def test_snap_to_sentence_boundaries():
    sents = [(0.0, 4.8), (5.0, 9.8), (10.0, 14.8), (15.0, 19.8), (20.0, 24.8)]
    assert snap_clip(5.6, 14.0, sents, 5, 30) == (5.0, 14.8)
    # te lang -> ingekort op een zinsgrens binnen max
    assert snap_clip(0.2, 24.0, sents, 5, 12) == (0.0, 9.8)
    # te kort -> verlengd tot min
    assert snap_clip(5.0, 7.0, sents, 9, 20) == (5.0, 14.8)
    assert snap_clip(10, 5, sents, 5, 20) is None


def test_dedupe_keeps_highest_score():
    clips = [
        {"start": 0, "end": 20, "_score": 5},
        {"start": 10, "end": 30, "_score": 8},
        {"start": 40, "end": 60, "_score": 3},
    ]
    kept = dedupe(clips)
    assert [(c["start"], c["end"]) for c in kept] == [(10, 30), (40, 60)]


def test_heuristic_pick_respects_duration_and_no_overlap():
    t = long_transcript()
    clips = heuristic_pick(t, 3, 15, 30)
    assert 1 <= len(clips) <= 3
    for c in clips:
        assert 15 <= c["end"] - c["start"] <= 30
        assert c["source"] == "heuristiek"
    for a in clips:
        for b in clips:
            if a is not b:
                assert pick.overlap((a["start"], a["end"]), (b["start"], b["end"])) < 0.5


def _llm_response(clips):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"clips": clips})}}]})


def test_llm_pick_parses_snaps_and_dedupes():
    t = long_transcript()
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["response_format"] == {"type": "json_object"}
        return _llm_response([
            {"start": 0.3, "end": 20.0, "title": "Een", "hook": "h", "reason": "r", "category": "tip",
             "hook_sterkte": 8, "emotie_of_verrassing": 7, "begrijpelijkheid_zonder_context": 9, "afronding": 6},
            {"start": 5, "end": 25, "title": "Overlap", "hook_sterkte": 2, "emotie_of_verrassing": 2,
             "begrijpelijkheid_zonder_context": 2, "afronding": 2},
            {"start": 60, "end": 85, "title": "Twee", "category": "onzin", "hook_sterkte": "11"},
        ])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    clips = llm_pick(t, 5, 15, 30, LLMConfig("https://x/v1", "m", "k"), client=client, sleep=lambda s: None)
    assert [c["title"] for c in clips] == ["Een", "Twee"]
    assert clips[1]["category"] == "overig" and clips[1]["subscores"]["hook_sterkte"] == 10
    for c in clips:
        assert 15 <= c["end"] - c["start"] <= 30


def test_chunking_large_transcript():
    t = long_transcript(600)
    chunks = pick._chunks(t)
    assert len(chunks) > 1 and all(len(c) <= pick.CHUNK_CHARS + 200 for c in chunks)


def test_retries_on_429_and_5xx():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        if len(attempts) == 2:
            return httpx.Response(503)
        return _llm_response([])

    sleeps = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = pick.chat_json(LLMConfig("https://x/v1", "m", "k"), [], client=client, sleep=sleeps.append)
    assert out == {"clips": []} and len(attempts) == 3 and len(sleeps) == 2


def test_invalid_key_raises_without_leaking_key():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(LLMError) as exc:
        pick.chat_json(LLMConfig("https://x/v1", "m", "gsk_geheim_123"), [], client=client, sleep=lambda s: None)
    assert exc.value.kind == "invalid" and "gsk_geheim_123" not in str(exc.value)


def test_pick_clips_falls_back_to_heuristic():
    t = long_transcript()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    clips, method, warning = pick_clips(t, 2, 15, 30, LLMConfig("https://x/v1", "m", "k"), client=client, sleep=lambda s: None)
    assert method == "heuristiek" and clips and "heuristiek" in warning
    clips, method, warning = pick_clips(t, 2, 15, 30, None)
    assert method == "heuristiek" and "Geen LLM-sleutel" in warning


def test_parse_json_with_fences_and_text():
    assert pick.parse_json_content('```json\n{"clips": []}\n```') == {"clips": []}
    assert pick.parse_json_content('Hier is het:\n{"clips": [1]}\nSucces!') == {"clips": [1]}
    with pytest.raises(ValueError):
        pick.parse_json_content("geen json")


def test_openrouter_request_and_402():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["title"] = request.headers.get("x-title")
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = LLMConfig("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct", "sk-or-v1-geheim")
    with pytest.raises(LLMError) as exc:
        pick.chat_json(cfg, [], client=client, sleep=lambda s: None)
    assert exc.value.kind == "no_credit" and "sk-or-v1-geheim" not in str(exc.value)
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions" and seen["title"] == "Shortcutter"
