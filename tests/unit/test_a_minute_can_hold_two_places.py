"""Two places named in one minute are told apart by the second.

Owner, 2026-10-05 (prod, Ben_Lin_Test): "Okay, back to the level one pre-pour
inspections" at 11:02:05, one photograph at 11:02:08, then "Moving up to level
two" at 11:02:14. The model marks places to the minute, so both were 11:02 and
the Level 1 photograph went to Level 2. The words carry their own times; the
quote on each marker says which words, so the marker can be timed to the
second, and the photograph (whose filename has seconds) compared at seconds.

THE test is `the photograph between two places of one minute goes to the
first`, built from that recording's real topics, markers and photo times.
"""
import json
from datetime import datetime

import pytest

import batch_stitch as bs
import photo_binding as pb
from transcript_utils import normalize_transcript

es = pytest.importorskip("lambda_extract_session")

# The real day, as extraction wrote it.
TOPICS = [{"time_range": "10:58 – 11:02"},     # 0 Level 1 pre-pour inspection
          {"time_range": "10:59 – 11:02"},     # 1 HVAC chat (the interruption)
          {"time_range": "11:02 – 11:03"},     # 2 Level 2 steel inspection
          {"time_range": "11:03 – 11:03"}]     # 3 Level 3, Te Kaha room
MODEL_MARKERS = [{"at": "10:58", "location": "level one"},
                 {"at": "11:02", "location": "level one"},
                 {"at": "11:02", "location": "level two"},
                 {"at": "11:03", "location": "level three"},
                 {"at": "11:03", "location": "Tikaha room"}]
TIMED = ["10:59:09", "11:02:05", "11:02:14", "11:03:42", "11:03:46"]   # from the words
MARKERS = [dict(m, at_s=t) for m, t in zip(MODEL_MARKERS, TIMED)]
PHOTOS = ["10:59:19", "10:59:29", "10:59:47", "11:02:08", "11:02:31", "11:02:36",
          "11:02:43", "11:03:32", "11:03:58", "11:04:04"]


def photos(times):
    return [{"key": t, "filename": t, "hhmm": t[:5], "hhmmss": t} for t in times]


def bound(markers):
    result = pb.photos_for_topics(photos(PHOTOS), TOPICS, markers=markers)
    return {i: [p["hhmmss"] for p in ps] for i, ps in result.items()}


def test_THE_the_photograph_between_two_places_of_one_minute_goes_to_the_first():
    assert "11:02:08" in bound(MODEL_MARKERS)[2], "the old answer, to the minute"
    assert bound(MARKERS) == {
        0: ["10:59:19", "10:59:29", "10:59:47", "11:02:08"],
        1: [],
        # 11:03:32 is after "Back to level two inspection" (11:03:28) and before
        # "Moving up to level three" (11:03:42): Level 2, by the words.
        2: ["11:02:31", "11:02:36", "11:02:43", "11:03:32"],
        3: ["11:03:58", "11:04:04"]}


def test_a_stays_topic_is_chosen_on_the_models_minute_not_the_words():
    """"Start the level one" was said at 10:59:09 in a turn stamped 10:58, the
    clock the topics are on. Keyed on 10:59 it would belong to the HVAC chat
    that starts at 10:59."""
    assert bound(MARKERS)[0][:3] == ["10:59:19", "10:59:29", "10:59:47"]


def test_without_seconds_nothing_changes():
    minute_only = [{"key": t, "filename": t, "hhmm": t[:5]} for t in PHOTOS]
    with_seconds = [dict(p, hhmmss=p["hhmm"] + ":59") for p in minute_only]
    def keys(ps):
        result = pb.photos_for_topics(ps, TOPICS, markers=MODEL_MARKERS)
        return {i: [p["key"] for p in v] for i, v in result.items()}
    assert keys(minute_only) == keys(with_seconds)


def test_place_at_reads_seconds():
    assert pb.place_at(MARKERS, "11:02:08") == "level one"
    assert pb.place_at(MARKERS, "11:02:20") == "level two"
    assert pb.place_at(MARKERS, "11:02") == "level two"         # a minute: its end
    assert pb.place_at(MARKERS, "10:58:30") is None              # before anything was said


def test_photo_time_has_seconds():
    assert pb.photo_time("Benl1_2026-10-05_11-02-08.jpg") == "11:02:08"
    assert pb.photo_hhmm("Benl1_2026-10-05_11-02-08.jpg") == "11:02"


# ---- extraction: the quote, found in the words --------------------------------------

STEM = "ben_lin_test_2026-10-05_11-02-01_sid37168e5632af4360b3784c752823f46c_c0007"
FILE = STEM + "_off0.0_to109.0_srcwav.json"


def transcript(words):
    """An AWS-shaped transcript: [(word, start_seconds)], one speaker."""
    return {"results": {
        "transcripts": [{"transcript": " ".join(w for w, _ in words)}],
        "items": [{"type": "pronunciation", "start_time": str(t), "end_time": str(t + 0.2),
                   "speaker_label": "spk_0",
                   "alternatives": [{"content": w, "confidence": "1.0"}]} for w, t in words]}}


# The real words of that file, in its own seconds.
WORDS = [("Okay,", 4.7), ("back", 4.9), ("to", 5.2), ("the", 5.4), ("level", 5.5),
         ("one", 5.8), ("pre-pour", 6.0), ("inspections.", 6.4),
         ("Moving", 14.0), ("up", 14.3), ("to", 14.5), ("level", 14.8), ("two,", 15.1),
         ("steel", 15.6), ("installation", 16.3), ("inspection.", 17.2),
         ("Back", 61.1), ("to", 61.3), ("level", 61.5), ("two", 62.5), ("inspection.", 63.0),
         ("Moving", 75.2), ("up", 75.5), ("to", 75.7), ("level", 75.8), ("three.", 76.4)]
QUOTED = [{"at": "11:02", "location": "level one",
           "quote": "Okay, back to the level one pre-pour inspections."},
          {"at": "11:02", "location": "level two",
           "quote": "Moving up to level two, steel installation inspection."}]


def turns_of(name, words):
    return normalize_transcript(transcript(words), name)["speaker_turns"]


def test_a_quote_is_timed_to_the_second_it_was_said():
    out = es.time_location_markers(es.clean_location_markers(QUOTED), turns_of(FILE, WORDS))
    assert [(m["at"], m.get("at_s")) for m in out] == [("11:02", "11:02:05"),
                                                       ("11:02", "11:02:15")]


def test_a_batched_file_is_timed_through_its_map(monkeypatch):
    """The file is four clips joined; the silence cut between them is not in its
    seconds. "Moving up to level three" is 75.2 s into the file and 11:03:42 on
    the clock -- adding 75.2 s to 11:02:01 says 11:03:16."""
    batch = bs.build_batch_name(STEM, 4, 0.0, 109.0)
    doc = bs.build_map("37168e5632af4360b3784c752823f46c", [
        bs.member(7, "k7", "2026-10-05T11:02:01", 0.0, 30.0),
        bs.member(8, "k8", "2026-10-05T11:02:57", 0.0, 30.0),
        bs.member(9, "k9", "2026-10-05T11:03:25", 2.0, 28.0),
        bs.member(10, "k10", "2026-10-05T11:03:53", 2.0, 28.0)], sealed_by="arrival")
    key = "transcripts/Ben_Lin_Test/2026-10-05/" + batch

    class S3:
        def get_object(self, Bucket, Key):
            assert Key == bs.map_key_for_transcript(key)

            class Body:
                def read(self):
                    return json.dumps(doc).encode()
            return {"Body": Body()}

    monkeypatch.setattr(es, "s3", lambda: S3())
    norm = es._rebase_batch_turns("b", key, normalize_transcript(transcript(WORDS), batch))
    marker = [{"at": "11:03", "location": "level three", "quote": "Moving up to level three."}]
    assert es.time_location_markers(marker, norm["speaker_turns"])[0]["at_s"] == "11:03:42"


def test_a_quote_said_late_in_a_long_turn_is_found_after_its_minute():
    words = [("Hello", 0.5)] + [("chat", 1.0 + i) for i in range(70)] + \
            [("Moving", 75.2), ("up", 75.5), ("to", 75.7), ("level", 75.8), ("three.", 76.4)]
    marker = [{"at": "11:02", "location": "level three", "quote": "Moving up to level three"}]
    out = es.time_location_markers(marker, turns_of(FILE, words))
    assert out[0]["at_s"] == "11:03:16" and out[0]["at"] == "11:02"


def test_a_quote_that_is_not_there_keeps_the_minute():
    marker = [{"at": "11:02", "location": "level four", "quote": "Heading over to level four now"}]
    assert es.time_location_markers(marker, turns_of(FILE, WORDS)) == marker


def test_a_quote_said_too_far_from_its_minute_is_not_borrowed():
    marker = [{"at": "10:40", "location": "level two", "quote": "Moving up to level two"}]
    assert es.time_location_markers(marker, turns_of(FILE, WORDS)) == marker


def test_a_chinese_quote_is_timed_by_its_characters():
    words = [("好", 1.0), ("的", 1.2), ("去", 3.0), ("二", 3.2), ("楼", 3.4), ("看", 3.6), ("看", 3.8)]
    marker = [{"at": "11:02", "location": "二楼", "quote": "去二楼看看"}]
    assert es.time_location_markers(marker, turns_of(FILE, words))[0]["at_s"] == "11:02:04"


def test_timing_that_fails_keeps_the_markers():
    out = es._timed_markers(QUOTED, [{"abs_start": "not a time", "words": [("x", 1.0)]}])
    assert out == es.clean_location_markers(QUOTED)


def test_turns_carry_their_words_in_file_seconds():
    turn = turns_of(FILE, WORDS[:3])[0]
    assert turn["words"] == [("Okay,", 4.7), ("back", 4.9), ("to", 5.2)]
    assert turn["abs_start"] == datetime(2026, 10, 5, 11, 2, 5, 700000)


# ---- wiring: the extraction artifact carries the timed markers --------------------

def test_the_extraction_artifact_carries_the_seconds(monkeypatch):
    import io
    import llm_utils

    key = "transcripts/Ben_Lin_Test/2026-10-05/" + FILE

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
    monkeypatch.setattr(es.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: (json.dumps(
        {"topics": [], "declared_site": None, "location_markers": QUOTED}), None))

    out = es.extract_session("b", "Ben_Lin_Test", "2026-10-05", "sid37168e5632af4360b3784c752823f46c",
                             final=True)

    assert [m.get("at_s") for m in out["location_markers"]] == ["11:02:05", "11:02:15"]


# ---- the day view's photo groups agree with the binding ----------------------------

def test_the_day_views_groups_read_seconds_too():
    from repositories import location_markers as lm
    assert lm.locate(MARKERS, "11:02:08") == "level one"
    assert lm.locate(MARKERS, "11:02:31") == "level two"
    assert lm.locate(MARKERS, "11:03:32") == "level two"
    assert lm.locate(MARKERS, "11:03:58") == "Tikaha room"
    assert lm.locate(MODEL_MARKERS, "11:02:08") == "level two"    # the minute rule, unchanged
    for t in PHOTOS:
        assert (lm.locate(MARKERS, t) or "").lower() == pb.place_at(MARKERS, t), t
