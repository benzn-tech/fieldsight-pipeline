"""item_locator: resolve the model's `said_at` stamp to ONE shown transcript turn, or refuse.

Every refusal is written from the side of "must NOT link": a stamp the model never saw,
a stamp two speakers share, a stamp from another topic's span.
"""
import json
from datetime import datetime, timedelta

import item_locator as loc
from repositories.claim_type_store import DST_TURN_KEYS

DATE = "2026-11-02"
F1 = "dev1_09-00-00_sidaaaa_c0000.json"
F2 = "dev1_09-05-00_sidaaaa_c0001.json"


def _turn(text, fname, start, at, speaker="spk_0"):
    """`at` is a datetime; abs_start_str is printed from it as the extractor does."""
    return {"text": text, "speaker": speaker, "source_filename": fname,
            "start_sec": start, "end_sec": start + 4.0,
            "abs_start": at, "abs_start_str": at.strftime("%H:%M:%S")}


def _dt(hh, mm, ss=0, day=2):
    return datetime(2026, 11, day, hh, mm, ss)


def _topic(time_range="09:00 – 09:10"):
    return {"topic_title": "synthetic", "time_range": time_range}


TURNS = [
    _turn("synthetic line alpha", F1, 10.0, _dt(9, 0, 10)),
    _turn("synthetic line bravo", F1, 50.0, _dt(9, 0, 50), speaker="spk_1"),
    _turn("synthetic line charlie", F2, 3.0, _dt(9, 5, 3)),
    _turn("synthetic line delta", F2, 40.0, _dt(9, 5, 40)),
]
ALL = set(range(len(TURNS)))


def _resolve(said_at, turns=TURNS, shown=None, time_range="09:00 – 09:10", absent=False):
    child = {"action": "synthetic action"}
    if not absent:
        child["said_at"] = said_at
    return loc.resolve(child, _topic(time_range), turns,
                       ALL if shown is None else shown, DATE)


def test_constants():
    assert loc.RULE_VERSION == "locator_v1"
    assert loc.STATUSES == ("linked", "no_locator", "unparseable", "not_shown", "no_line",
                            "ambiguous_stamp", "outside_topic")
    assert loc.LOCATOR_FIELD == "said_at"


def test_linked_exact_stamp_inside_range_has_slice1_locator_shape():
    s = _resolve("09:05:03")
    assert s["status"] == "linked"
    assert {k: s[k] for k in DST_TURN_KEYS} == {
        "source_stem": "dev1_09-05-00_sidaaaa_c0001", "start_sec": 3.0, "end_sec": 7.0,
        "speaker_label": "spk_0", "quote_status": None}
    assert s["method"] == "locator_v1"
    assert s["rule_version"] == "locator_v1"
    assert s["score"] is None
    assert s["range_checked"] is True
    assert set(s) == set(DST_TURN_KEYS) | {"status", "method", "rule_version", "score",
                                           "range_checked"}


def test_single_digit_hour_is_normalised():
    s = _resolve("9:05:03")
    assert s["status"] == "linked"
    assert s["start_sec"] == 3.0


def test_unparseable_stamps():
    for bad in ("9am", "09:05", "24:00:00", "09:60:00", "09:05:60", "09:05:03 ", " 09:05:03",
                "", 905, ["09:05:03"], "009:05:03", "09:5:03"):
        s = _resolve(bad)
        assert s["status"] == "unparseable", bad
        assert s["method"] == s["rule_version"] == "locator_v1"
        assert "source_stem" not in s


def test_null_and_absent_are_no_locator():
    assert _resolve(None)["status"] == "no_locator"
    assert _resolve(None, absent=True)["status"] == "no_locator"


def test_non_dict_child_is_no_locator():
    assert loc.resolve("plain string decision", _topic(), TURNS, ALL, DATE)["status"] \
        == "no_locator"


def test_stamp_only_in_elided_turn_is_not_shown():
    s = _resolve("09:05:03", shown={0, 1, 3})
    assert s["status"] == "not_shown"
    assert "source_stem" not in s


def test_no_match_anywhere_is_no_line():
    assert _resolve("09:07:07")["status"] == "no_line"


def test_two_shown_turns_same_stamp_is_ambiguous():
    turns = TURNS + [_turn("synthetic line echo", F2, 3.0, _dt(9, 5, 3), speaker="spk_2")]
    s = _resolve("09:05:03", turns=turns, shown=set(range(len(turns))))
    assert s["status"] == "ambiguous_stamp"
    assert "source_stem" not in s


def test_duplicate_stamp_with_one_elided_twin_links_the_shown_one():
    turns = TURNS + [_turn("synthetic line echo", F2, 3.0, _dt(9, 5, 3), speaker="spk_2")]
    s = _resolve("09:05:03", turns=turns, shown=ALL)  # index 4 not shown
    assert s["status"] == "linked"
    assert s["speaker_label"] == "spk_0"


def test_range_widened_by_one_minute_each_side():
    turns = [
        _turn("synthetic before far", F1, 1.0, _dt(8, 58, 0)),     # 2 min before 09:00
        _turn("synthetic before near", F1, 2.0, _dt(8, 59, 10)),   # 50 s before
        _turn("synthetic after near", F2, 3.0, _dt(9, 11, 50)),    # 50 s past the end minute
        _turn("synthetic after far", F2, 4.0, _dt(9, 13, 0)),      # 2 min past end minute
    ]
    shown = set(range(len(turns)))
    assert _resolve("08:58:00", turns=turns, shown=shown)["status"] == "outside_topic"
    assert _resolve("08:59:10", turns=turns, shown=shown)["status"] == "linked"
    assert _resolve("09:11:50", turns=turns, shown=shown)["status"] == "linked"
    far = _resolve("09:13:00", turns=turns, shown=shown)
    assert far["status"] == "outside_topic"
    assert far["range_checked"] is True
    assert "source_stem" not in far


def test_range_crossing_midnight():
    turns = [
        _turn("synthetic late", F1, 1.0, _dt(23, 57, 30)),
        _turn("synthetic later", F1, 60.0, _dt(23, 59, 0)),
        _turn("synthetic past midnight", F2, 5.0, _dt(0, 1, 10, day=3)),
    ]
    s = _resolve("00:01:10", turns=turns, shown={0, 1, 2}, time_range="23:58 – 00:03")
    assert s["status"] == "linked"
    assert s["start_sec"] == 5.0
    assert s["range_checked"] is True


def test_unparseable_time_range_skips_check():
    for tr in ("morning", None, ""):
        s = _resolve("09:05:03", time_range=tr)
        assert s["status"] == "linked"
        assert s["range_checked"] is False


def test_support_carries_no_text():
    for stamp, shown in (("09:05:03", ALL), ("09:05:03", {0}), ("09:07:07", ALL), ("9am", ALL)):
        s = _resolve(stamp, shown=shown)
        texts = {t["text"] for t in TURNS} | {"synthetic action"}
        assert not any(v in texts for v in s.values() if isinstance(v, str))


def _topics_for_strip():
    status = {"status": "done", "quote": "synthetic", "at": "09:05:03"}
    return [
        {"topic_title": "t1",
         "action_items": [{"action": "a1", "said_at": "09:00:10", "status_suggestion": status},
                          {"action": "a2", "said_at": None}],
         "findings": [{"observation": "f1", "said_at": "09:05:03"}],
         "decisions": ["plain string decision", {"decision": "d1", "said_at": "9am"}],
         "questions": [{"question": "q1"}]},
        {"topic_title": "t2", "findings": "a string, not a list",
         "questions": [{"question": "q2", "said_at": "09:05:40"}]},
        "not a topic",
        {"topic_title": "t3", "action_items": None},
    ]


def test_strip_removes_said_at_from_all_kinds_and_counts():
    topics = _topics_for_strip()
    n = loc.strip(topics)
    assert n == 5
    assert "said_at" not in json.dumps(topics)


def test_strip_leaves_status_suggestion_byte_identical():
    topics = _topics_for_strip()
    before = json.dumps(topics[0]["action_items"][0]["status_suggestion"], sort_keys=True)
    loc.strip(topics)
    after = json.dumps(topics[0]["action_items"][0]["status_suggestion"], sort_keys=True)
    assert before == after
    assert topics[0]["action_items"][0]["status_suggestion"]["at"] == "09:05:03"


def test_strip_keeps_list_identity_and_order():
    topics = _topics_for_strip()
    acts = topics[0]["action_items"]
    a1, a2 = acts
    decs = topics[0]["decisions"]
    loc.strip(topics)
    assert topics[0]["action_items"] is acts
    assert acts[0] is a1 and acts[1] is a2
    assert topics[0]["decisions"] is decs
    assert decs[0] == "plain string decision"
    assert decs[1] == {"decision": "d1"}


def test_strip_survives_malformed_shapes():
    assert loc.strip(None) == 0
    assert loc.strip("nope") == 0
    assert loc.strip([{"findings": "string"}, 3, None]) == 0
