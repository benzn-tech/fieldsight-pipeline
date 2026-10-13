"""Inside one place, the first words after a photograph say what it shows.

Owner, 2026-10-12 (TEST, Ben_Lin_test2): the whole pre-pour walk was one place,
"level two slab", announced in the first topic -- so both formwork photographs
(10:53:10, 10:53:14) went to "Concrete pour booking and approvals", which had
ended at 10:52. "Formwork's set up fine" was said at 10:53:19. The owner chose
the signal: the first sentence said after the photo.

Every clock rule tried before broke the 2026-10-01 interruption case, so the
conditions below are each pinned by the replayed day that broke without them.

THE test is the first one, built from that recording's real topics, markers,
photo times and quote times (the quotes timed from its words).
"""
import pytest

import photo_binding as pb
from transcript_utils import normalize_transcript

es = pytest.importorskip("lambda_extract_session")


def q(at_s, quote="..."):
    return {"at": at_s, "at_s": at_s, "quote": quote}


# TEST Ben_Lin_test2 2026-10-12, as extraction wrote it (at_s from the words).
TOPICS = [
    {"time_range": "10:51 – 10:52", "evidence": [q("10:51:58"), q("10:52:29")]},  # 0 pour booking
    {"time_range": "10:53 – 10:54", "evidence": [q("10:53:19"), q("10:53:36")]},  # 1 formwork
    {"time_range": "10:54 – 10:54", "evidence": [q("10:54:19"), q("10:54:27")]},  # 2 "hey, Dave"
    {"time_range": "10:54 – 10:54", "evidence": [q("10:54:37")]},                 # 3 stair core
    {"time_range": "10:54 – 10:55", "evidence": [q("10:54:52"), q("10:55:14")]},  # 4 slab rebar
    {"time_range": "10:55 – 10:55", "evidence": [q("10:55:52")]},                 # 5 MEP
    {"time_range": "10:55 – 10:56", "evidence": [q("10:56:05"), q("10:56:18")]}]  # 6 pour logistics
MARKERS = [{"at": "10:51", "at_s": "10:51:58", "location": "level two slab"},
           {"at": "10:54", "at_s": "10:54:37", "location": "stair core"}]


def photos(*times):
    return [{"key": t, "filename": t, "hhmm": t[:5], "hhmmss": t} for t in times]


def bound(topics, markers, *times):
    why = {}
    result = pb.photos_for_topics(photos(*times), topics, markers=markers, explain=why)
    return {t: next((i for i, ps in result.items() if any(p["key"] == t for p in ps)), None)
            for t in times}, why


def test_THE_the_formwork_photos_go_to_the_formwork():
    where, why = bound(TOPICS, MARKERS, "10:53:10", "10:53:14")
    assert where == {"10:53:10": 1, "10:53:14": 1}
    assert why == {"10:53:10": "words", "10:53:14": "words"}


def test_without_cited_quotes_the_place_decides_as_before():
    """Prod extracts without citations (EMIT_EVIDENCE=false): nothing changes."""
    bare = [{"time_range": t["time_range"]} for t in TOPICS]
    where, why = bound(bare, MARKERS, "10:53:10", "10:53:14")
    assert where == {"10:53:10": 0, "10:53:14": 0}
    assert set(why.values()) == {"location"}


def test_the_quotes_aurora_stores_are_read_too():
    """list_day_topics_for_binding hands over the {"status", "quotes"} object."""
    stored = [dict(t, evidence={"status": "verified", "quotes": t["evidence"]}) for t in TOPICS]
    assert bound(stored, MARKERS, "10:53:10")[0] == {"10:53:10": 1}


def test_silence_after_the_photo_leaves_it_with_the_place():
    """TEST 2026-10-01: nothing was said within 19 s of the Level 1 photos, and
    the first words after one were members of the public asking the way."""
    where, why = bound(TOPICS, MARKERS, "10:53:00")        # first words 19 s later
    assert where == {"10:53:00": 0} and why == {"10:53:00": "location"}


# Prod Ben_Lin_Test 2026-10-05 (see test_a_minute_can_hold_two_places).
DAY_1005 = [
    {"time_range": "10:58 – 11:02", "evidence": [q("11:02:05")]},                # 0 Level 1 pre-pour
    {"time_range": "10:59 – 11:02", "evidence": [q("10:59:53"), q("11:00:06")]},  # 1 HVAC chat
    {"time_range": "11:02 – 11:03", "evidence": [q("11:02:14"), q("11:03:28")]},  # 2 Level 2 steel
    {"time_range": "11:03 – 11:03", "evidence": [q("11:03:42")]}]                # 3 Level 3
MARKERS_1005 = [{"at": "10:58", "at_s": "10:59:09", "location": "level one"},
                {"at": "11:02", "at_s": "11:02:05", "location": "level one"},
                {"at": "11:02", "at_s": "11:02:14", "location": "level two"},
                {"at": "11:03", "at_s": "11:03:42", "location": "level three"}]


def test_while_the_places_topic_goes_on_what_comes_next_is_an_interruption():
    """A Level 1 photo at 10:59:47, then "How was your HVAC installation, level
    three?" 6 s later. The Level 1 inspection was still under way."""
    where, why = bound(DAY_1005, MARKERS_1005, "10:59:47")
    assert where == {"10:59:47": 0} and why == {"10:59:47": "location"}


def test_words_said_in_the_next_place_do_not_take_the_photo_there():
    """A Level 1 photo at 11:02:08, then "Moving up to level two" at 11:02:14:
    where he went next, not what he photographed. Pinned with the Level 1
    topic ended, so only the place rule is holding it."""
    ended = [dict(DAY_1005[0], time_range="10:58 – 11:01")] + DAY_1005[1:]
    where, why = bound(ended, MARKERS_1005, "11:02:08")
    assert where == {"11:02:08": 0} and why == {"11:02:08": "location"}


def test_a_photo_known_only_to_the_minute_has_no_after():
    result = pb.photos_for_topics([{"key": "k", "filename": "k", "hhmm": "10:53"}],
                                  TOPICS, markers=MARKERS)
    assert [i for i, ps in result.items() if ps] == [0]


def test_a_full_topic_does_not_lose_the_photo():
    """At the cap, the photo stays with its place rather than dropping."""
    full = [dict(t) for t in TOPICS]
    cap = pb.PHOTOS_PER_TOPIC_CAP
    try:
        pb.PHOTOS_PER_TOPIC_CAP = 1
        result = pb.photos_for_topics(photos("10:53:10", "10:53:14"), full, markers=MARKERS)
    finally:
        pb.PHOTOS_PER_TOPIC_CAP = cap
    assert [p["key"] for p in result[1]] == ["10:53:10"]
    assert [p["key"] for p in result[0]] == ["10:53:14"]


# ---- extraction: each cited quote timed to the second ------------------------------

FILE = ("ben_lin_test2_2026-10-12_10-52-29_sid57468fd84a6945b994f5594c2d0f94be_c0001"
        "_off0.0_to114.0_srcwav.json")
WORDS = [("Yeah,", 44.0), ("that's", 44.3), ("being", 44.6), ("fixed", 44.9), ("now.", 45.2),
         ("Formwork's", 50.5), ("set", 50.9), ("up", 51.1), ("fine.", 51.3),
         ("Checked", 53.0), ("the", 53.3), ("levels", 53.5),
         ("Oh,", 110.0), ("hey,", 110.3), ("Dave.", 110.6), ("Yeah,", 111.0), ("the", 111.2),
         ("scaffolding's", 111.5), ("coming", 112.0), ("Friday.", 112.4)]


def transcript(words):
    return {"results": {
        "transcripts": [{"transcript": " ".join(w for w, _ in words)}],
        "items": [{"type": "pronunciation", "start_time": str(t), "end_time": str(t + 0.2),
                   "speaker_label": "spk_0",
                   "alternatives": [{"content": w, "confidence": "1.0"}]} for w, t in words]}}


def test_two_quotes_from_one_line_get_the_seconds_they_were_said():
    """Both quotes start in the line stamped 10:53:19 -- the model copies the
    line's time -- but Dave was greeted a minute later."""
    turns = normalize_transcript(transcript(WORDS), FILE)["speaker_turns"]
    result = {"topics": [
        {"evidence": [{"at": "10:53:19", "quote": "Formwork's set up fine. Checked the levels"}]},
        {"evidence": [{"at": "10:53:19", "quote": "Oh, hey, Dave. Yeah, the scaffolding's coming Friday."}]}]}
    assert es.time_evidence(result, turns) == 2
    assert [t["evidence"][0]["at_s"] for t in result["topics"]] == ["10:53:19", "10:54:19"]
    assert result["topics"][0]["evidence"][0]["at"] == "10:53:19", "the model's time is kept"


def test_the_extraction_artifact_carries_the_quotes_seconds(monkeypatch):
    """Wired where the words are: the final extraction, with citations on."""
    import io
    import json
    import llm_utils

    key = "transcripts/Ben_Lin_test2/2026-10-12/" + FILE

    class S3:
        def get_object(self, Bucket, Key):
            if Key != key:
                raise KeyError(Key)
            return {"Body": io.BytesIO(json.dumps(transcript(WORDS)).encode())}

        def get_paginator(self, op):
            class P:
                def paginate(self, Bucket, Prefix):
                    yield {"Contents": [{"Key": key}] if key.startswith(Prefix) else []}
            return P()

        def put_object(self, **kw):
            return {}

    monkeypatch.setattr(es, "s3", lambda: S3())
    monkeypatch.setattr(es, "_sites_cache", None)
    monkeypatch.setattr(es, "EMIT_EVIDENCE", True)
    monkeypatch.setattr(es.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: (json.dumps(
        {"topics": [{"topic_title": "Scaffolding", "time_range": "10:54 – 10:54",
                     "evidence": [{"at": "10:53:19", "quote": "Oh, hey, Dave. Yeah, the scaffolding's"}]}],
         "declared_site": None, "location_markers": []}), None))

    out = es.extract_session("b", "Ben_Lin_test2", "2026-10-12",
                             "sid57468fd84a6945b994f5594c2d0f94be", final=True)
    assert out["topics"][0]["evidence"][0]["at_s"] == "10:54:19"


def test_a_quote_that_is_not_in_the_words_gets_no_seconds():
    turns = normalize_transcript(transcript(WORDS), FILE)["speaker_turns"]
    result = {"topics": [{"evidence": [{"at": "10:53:19", "quote": "Pump is booked for seven"}]}]}
    assert es.time_evidence(result, turns) == 0
    assert "at_s" not in result["topics"][0]["evidence"][0]
