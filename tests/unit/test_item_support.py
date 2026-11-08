"""item_support: link each final child item to the turn it came from, precision first.

A wrong link is worse than no link, so every test that can go wrong is written
from the side of "must NOT link".
"""
import json
from datetime import datetime

import pytest

import item_support as isup

DATE = "2026-11-02"
CAL = dict(floor=0.5, margin=0.1, nothing_close=0.2)


def _turn(text, fname, start, hh, mm, ss=0, speaker="spk_0"):
    return {"text": text, "speaker": speaker, "source_filename": fname,
            "start_sec": start, "end_sec": start + 4.0,
            "abs_start": datetime(2026, 11, 2, hh, mm, ss)}


def _ev(fname, offset, status="verified"):
    base = fname.rsplit(".", 1)[0]
    return {"at": "10:00:00", "quote": "synthetic quote", "status": status,
            "segment_key": f"audio_segments/u1/{DATE}/{base}.wav",
            "offset_sec": offset}


def _topic(evidence=(), time_range="10:00 – 10:05", **children):
    t = {"topic_title": "synthetic", "time_range": time_range,
         "evidence": list(evidence)}
    for k in ("action_items", "findings", "decisions", "questions"):
        t[k] = children.get(k, [])
    return t


F1 = "dev1_10-00-00_sidaaaa_c0000.json"
F2 = "dev1_10-03-00_sidaaaa_c0001.json"


def test_links_item_to_its_quoted_turn():
    turns = [_turn("pump the basement sump before the pour", F1, 12.0, 10, 0, 12),
             _turn("scaffold tie missing on north elevation", F1, 30.0, 10, 0, 30)]
    result = {"topics": [_topic(
        evidence=[_ev(F1, 30.2, "verified_fuzzy")],
        findings=[{"observation": "Scaffold tie missing on the north elevation"}])]}
    counts = isup.attach_support(result, turns, DATE, **CAL)
    s = result["topics"][0]["findings"][0]["support"]
    assert s["status"] == "linked"
    assert s["candidate_path"] == "quote"
    assert s["source_stem"] == "dev1_10-00-00_sidaaaa_c0000"
    assert s["start_sec"] == 30.0 and s["end_sec"] == 34.0
    assert s["speaker_label"] == "spk_0"
    assert s["quote_status"] == "verified_fuzzy"
    assert s["score"] == 1.0
    assert s["rule_version"] == isup.RULE_VERSION
    assert counts["linked"] == 1 and counts["by_path"]["quote"] == 1


def test_window_path_used_only_when_quote_path_fails():
    turns = [_turn("pump the basement sump before the pour", F1, 12.0, 10, 0, 12),
             _turn("handrail bracket loose on stair two", F2, 5.0, 10, 3, 5)]
    # Quote path resolves to the sump turn, which does not support the item.
    result = {"topics": [_topic(
        evidence=[_ev(F1, 12.0)],
        action_items=[{"action": "Fix loose handrail bracket on stair two"},
                      {"action": "Pump the basement sump before the pour"}])]}
    counts = isup.attach_support(result, turns, DATE, **CAL)
    a0, a1 = (c["support"] for c in result["topics"][0]["action_items"])
    assert a0["status"] == "linked" and a0["candidate_path"] == "window"
    assert a0["quote_status"] is None
    assert a0["start_sec"] == 5.0
    assert a1["status"] == "linked" and a1["candidate_path"] == "quote"
    assert counts["by_path"] == {"quote": 1, "window": 1}


def test_window_path_respects_time_range_and_hyphen():
    # Identical text in and out of the window: if the window leaked, this would tie.
    turns = [_turn("handrail bracket loose on stair two", F2, 5.0, 10, 5, 40),
             _turn("handrail bracket loose on stair two", F2, 50.0, 10, 6, 0)]
    result = {"topics": [_topic(time_range="10:00 - 10:05",
                                action_items=[{"action": "Fix handrail bracket stair two"}])]}
    isup.attach_support(result, turns, DATE, **CAL)
    s = result["topics"][0]["action_items"][0]["support"]
    assert s["status"] == "linked" and s["start_sec"] == 5.0   # end minute is inclusive


def test_unparseable_time_range_gives_no_window_candidates():
    turns = [_turn("handrail bracket loose on stair two", F2, 5.0, 10, 3)]
    result = {"topics": [_topic(time_range="morning",
                                action_items=[{"action": "Fix handrail bracket stair two"}])]}
    isup.attach_support(result, turns, DATE, **CAL)
    assert result["topics"][0]["action_items"][0]["support"]["status"] == "none_close"


def test_paths_constant_gates_window():
    turns = [_turn("handrail bracket loose on stair two", F2, 5.0, 10, 3)]
    result = {"topics": [_topic(action_items=[{"action": "Fix handrail bracket stair two"}])]}
    orig = isup.PATHS
    try:
        isup.PATHS = ("quote",)
        isup.attach_support(result, turns, DATE, **CAL)
    finally:
        isup.PATHS = orig
    assert result["topics"][0]["action_items"][0]["support"]["status"] == "none_close"


def test_tie_within_margin_is_ambiguous_not_linked():
    turns = [_turn("level two slab pour friday", F1, 10.0, 10, 0, 10),
             _turn("level two slab pour friday confirmed", F1, 20.0, 10, 0, 20)]
    result = {"topics": [_topic(
        evidence=[_ev(F1, 10.0), _ev(F1, 20.0)],
        decisions=[{"decision": "Level two slab pour on Friday"}])]}
    counts = isup.attach_support(result, turns, DATE, **CAL)
    s = result["topics"][0]["decisions"][0]["support"]
    assert s["status"] == "ambiguous"
    assert s["best"] == 1.0 and s["second"] == 1.0
    assert counts["ambiguous"] == 1 and counts["linked"] == 0


def test_two_quotes_on_one_turn_do_not_tie_with_itself():
    turns = [_turn("level two slab pour friday", F1, 10.0, 10, 0, 10)]
    result = {"topics": [_topic(
        evidence=[_ev(F1, 10.0, "verified_fuzzy"), _ev(F1, 10.3, "verified")],
        decisions=[{"decision": "Level two slab pour on Friday"}])]}
    isup.attach_support(result, turns, DATE, **CAL)
    s = result["topics"][0]["decisions"][0]["support"]
    assert s["status"] == "linked"
    assert s["quote_status"] == "verified"


def test_unverified_evidence_is_not_a_quote_candidate():
    turns = [_turn("level two slab pour friday", F1, 10.0, 9, 0)]  # outside window too
    result = {"topics": [_topic(
        evidence=[_ev(F1, 10.0, "unverified")],
        decisions=[{"decision": "Level two slab pour on Friday"}])]}
    isup.attach_support(result, turns, DATE, **CAL)
    assert result["topics"][0]["decisions"][0]["support"]["status"] == "none_close"


def test_twin_items_never_link_to_each_others_turn():
    t3 = _turn("unit 3 pre-line is done", F1, 10.0, 10, 0, 10)
    t4 = _turn("unit 4 pre-line is done", F1, 20.0, 10, 0, 20)
    result = {"topics": [_topic(
        evidence=[_ev(F1, 10.0), _ev(F1, 20.0)],
        findings=[{"observation": "unit 3 pre-line done"},
                  {"observation": "unit 4 pre-line done"}])]}
    isup.attach_support(result, [t3, t4], DATE, floor=0.5, margin=0.1, nothing_close=0.2)
    s3, s4 = (c["support"] for c in result["topics"][0]["findings"])
    assert s3["status"] == "linked" and s3["start_sec"] == 10.0
    assert s4["status"] == "linked" and s4["start_sec"] == 20.0


def test_chinese_item_links_to_chinese_turn():
    turns = [_turn("二楼的防水膜明天会到", F1, 10.0, 10, 0, 10),
             _turn("三楼脚手架下午拆除", F1, 20.0, 10, 0, 20)]
    result = {"topics": [_topic(
        evidence=[_ev(F1, 10.0), _ev(F1, 20.0)],
        action_items=[{"action": "二楼防水膜明天到"}])]}
    isup.attach_support(result, turns, DATE, **CAL)
    s = result["topics"][0]["action_items"][0]["support"]
    assert s["status"] == "linked" and s["start_sec"] == 10.0
    assert s["score"] >= 0.5


def test_nothing_close_marks_none_close():
    turns = [_turn("crane booked for tuesday morning", F1, 10.0, 10, 1)]
    result = {"topics": [_topic(
        evidence=[_ev(F1, 10.0)],
        findings=[{"observation": "Fire stopping incomplete in riser cupboard"}])]}
    counts = isup.attach_support(result, turns, DATE, **CAL)
    s = result["topics"][0]["findings"][0]["support"]
    assert s["status"] == "none_close" and s["best"] < 0.2
    assert counts["none_close"] == 1


def test_between_nothing_close_and_floor_is_below_floor():
    turns = [_turn("crane booked tuesday", F1, 10.0, 10, 1)]
    result = {"topics": [_topic(
        evidence=[_ev(F1, 10.0)],
        action_items=[{"action": "crane delivery permit council signoff"}])]}  # 1/5 = 0.2
    isup.attach_support(result, turns, DATE, **CAL)
    s = result["topics"][0]["action_items"][0]["support"]
    assert s["status"] == "below_floor" and s["best"] == 0.2


def test_single_token_item_is_never_linked():
    turns = [_turn("bay 2 is clear", F1, 10.0, 10, 1)]
    result = {"topics": [_topic(evidence=[_ev(F1, 10.0)],
                                action_items=[{"action": "2"}])]}
    isup.attach_support(result, turns, DATE, **CAL)
    s = result["topics"][0]["action_items"][0]["support"]
    assert s["status"] == "below_floor" and s["best"] == 1.0


def test_margin_exactly_met_links():
    # Item has 10 tokens; turns score 0.7 and 0.6 -> diff 0.1 == margin.
    item = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    turns = [_turn("alpha bravo charlie delta echo foxtrot golf", F1, 10.0, 10, 0, 10),
             _turn("alpha bravo charlie delta echo foxtrot", F1, 20.0, 10, 0, 20)]
    result = {"topics": [_topic(evidence=[_ev(F1, 10.0), _ev(F1, 20.0)],
                                findings=[{"observation": item}])]}
    isup.attach_support(result, turns, DATE, floor=0.5, margin=0.1, nothing_close=0.2)
    s = result["topics"][0]["findings"][0]["support"]
    assert s["status"] == "linked" and s["start_sec"] == 10.0


def test_string_decisions_and_questions_get_support():
    turns = [_turn("we will use the precast stair option", F1, 10.0, 10, 0, 10),
             _turn("who signs off the fire doors", F1, 20.0, 10, 0, 20)]
    result = {"topics": [_topic(
        evidence=[_ev(F1, 10.0), _ev(F1, 20.0)],
        decisions=["Use the precast stair option"],
        questions=["Who signs off the fire doors?"])]}
    isup.attach_support(result, turns, DATE, **CAL)
    topic = result["topics"][0]
    d, q = topic["decisions"][0], topic["questions"][0]
    assert d["decision"] == "Use the precast stair option"
    assert q["question"] == "Who signs off the fire doors?"
    assert d["support"]["status"] == "linked" and d["support"]["start_sec"] == 10.0
    assert q["support"]["status"] == "linked" and q["support"]["start_sec"] == 20.0


@pytest.mark.parametrize("missing", ["floor", "margin", "nothing_close"])
def test_uncalibrated_constants_refuse(missing):
    kw = dict(CAL)
    kw[missing] = None
    result = {"topics": [_topic(findings=[{"observation": "x y"}])]}
    with pytest.raises(ValueError):
        isup.attach_support(result, [], DATE, **kw)
    assert "support" not in result["topics"][0]["findings"][0]
    assert isup.FLOOR is None and isup.MARGIN is None and isup.NOTHING_CLOSE is None
    with pytest.raises(ValueError):
        isup.attach_support(result, [], DATE, floor=isup.FLOOR, margin=isup.MARGIN,
                            nothing_close=isup.NOTHING_CLOSE)


def _sample():
    turns = [_turn("unit 3 pre-line is done", F1, 10.0, 10, 0, 10),
             _turn("unit 4 pre-line is done", F1, 20.0, 10, 0, 20),
             _turn("handrail bracket loose on stair two", F2, 5.0, 10, 3, 5)]
    result = {"topics": [_topic(
        evidence=[_ev(F1, 10.0), _ev(F1, 20.0)],
        findings=[{"observation": "unit 3 pre-line done"}],
        action_items=[{"action": "Fix handrail bracket stair two"}],
        decisions=["Level two slab pour Friday"],
        questions=[{"question": "unit pre-line done?"}])]}
    return result, turns


def test_deterministic_across_two_runs():
    r1, t1 = _sample()
    r2, t2 = _sample()
    c1 = isup.attach_support(r1, t1, DATE, **CAL)
    c2 = isup.attach_support(r2, t2, DATE, **CAL)
    assert json.dumps(r1, sort_keys=True, default=str) == json.dumps(r2, sort_keys=True, default=str)
    assert c1 == c2


def test_no_text_in_support_object():
    result, turns = _sample()
    isup.attach_support(result, turns, DATE, **CAL)
    texts = [t["text"] for t in turns]
    for topic in result["topics"]:
        for kind, field in (("action_items", "action"), ("findings", "observation"),
                            ("decisions", "decision"), ("questions", "question")):
            for child in topic[kind]:
                texts.append(child[field])
    seen = 0
    for topic in result["topics"]:
        for kind in ("action_items", "findings", "decisions", "questions"):
            for child in topic[kind]:
                s = child["support"]
                seen += 1
                for v in s.values():
                    assert not isinstance(v, (list, dict))
                    if isinstance(v, str):
                        assert v not in texts
                        assert not any(v in t for t in texts if len(v) > 3 and v not in
                                       ("linked", "quote", "window", "verified",
                                        "verified_fuzzy", isup.RULE_VERSION))
    assert seen == 4
