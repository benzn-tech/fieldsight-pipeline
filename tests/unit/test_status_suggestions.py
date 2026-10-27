"""Status suggestions from speech (spec 2026-10-27, v1 same-session).

The model may propose that an action item was reported done / closed / blocked / started in this
session. Code decides what survives: shape, carried by IDENTITY from the earlier pass (Part C --
never by text), and a quote that is really in the transcript. Nothing here writes a status.
"""
import json
import uuid

import status_suggestions as ss
from item_continuity import PriorItem
from tests.unit.test_extract_session_continuity import _capturing_llm, _one_segment_s3
from tests.unit.test_lambda_extract_session import (
    BUCKET, FakeS3, OUT_KEY, SEG1_KEY, SESSION_BASE, les, llm_utils, make_transcribe_json)

PRIOR_ID = str(uuid.uuid4())
PRIOR = [PriorItem("A1", "action_items", "Sarah to send the drawings", PRIOR_ID)]
GOOD = {"status": "done", "at": "10:00:00",
        "quote": "Sarah sent the drawings over so that one is done"}


def _topics(**suggestion_on):
    child = {"action": "Sarah to send the drawings", "item_id": PRIOR_ID}
    child.update(suggestion_on)
    return [{"topic_title": "t", "action_items": [child]}]


def _verified(quote, at):
    return {"status": "verified", "segment_key": "seg-1", "offset_sec": 12.0}


def test_a_carried_item_with_a_verified_quote_keeps_its_suggestion():
    topics = _topics(status_suggestion=dict(GOOD))
    counts = ss.filter_suggestions(topics, PRIOR, _verified)
    kept = topics[0]["action_items"][0]["status_suggestion"]
    assert kept["status"] == "done" and kept["segment_key"] == "seg-1"
    assert kept["offset_sec"] == 12.0 and kept["evidence"] == "verified"
    assert counts == {"offered": 1, "kept": 1, "dropped_by": {}}


def test_a_fresh_item_id_drops_the_suggestion_part_c3():
    topics = _topics(status_suggestion=dict(GOOD))
    topics[0]["action_items"][0]["item_id"] = str(uuid.uuid4())   # resolve gave a fresh id
    counts = ss.filter_suggestions(topics, PRIOR, _verified)
    assert "status_suggestion" not in topics[0]["action_items"][0]
    assert counts["dropped_by"] == {"not_carried": 1}


def test_a_matching_text_is_not_carried_without_the_identity():
    """Part C.1: same words, different id is still a different item."""
    topics = [{"action_items": [{"action": "Sarah to send the drawings",
                                 "item_id": str(uuid.uuid4()),
                                 "status_suggestion": dict(GOOD)}]}]
    assert ss.filter_suggestions(topics, PRIOR, _verified)["kept"] == 0


def test_shape_and_kind_are_checked_before_anything_else():
    bad = [{"status": "finished", "at": "10:00:00", "quote": "x"},
           {"status": "done", "at": "", "quote": "x"},
           {"status": "done", "at": "10:00:00", "quote": "  "},
           "done"]
    for s in bad:
        topics = _topics(status_suggestion=s)
        assert ss.filter_suggestions(topics, PRIOR, _verified)["dropped_by"] == {"shape": 1}, s
    topics = [{"findings": [{"observation": "o", "item_id": PRIOR_ID,
                             "status_suggestion": dict(GOOD)}]}]
    assert ss.filter_suggestions(topics, PRIOR, _verified)["dropped_by"] == {"wrong_kind": 1}


def test_only_verified_or_fuzzy_quotes_survive():
    for status, kept in (("verified_fuzzy", 1), ("weak", 0), ("unverified", 0),
                         ("unchecked", 0), (None, 0)):
        topics = _topics(status_suggestion=dict(GOOD))
        counts = ss.filter_suggestions(topics, PRIOR, lambda q, a, s=status: {"status": s})
        assert counts["kept"] == kept, status
        if not kept:
            assert counts["dropped_by"] == {status or "unchecked": 1}


def test_counts_never_carry_quote_text():
    topics = _topics(status_suggestion=dict(GOOD))
    counts = ss.filter_suggestions(topics, PRIOR, lambda q, a: {"status": "unverified"})
    assert "drawings" not in json.dumps(counts)


def test_strip_all_removes_every_suggestion():
    topics = _topics(status_suggestion=dict(GOOD))
    topics[0]["findings"] = [{"observation": "o", "status_suggestion": dict(GOOD)}]
    assert ss.strip_all(topics) == 2
    assert "status_suggestion" not in json.dumps(topics)


# ------------------------------------------------------------ wired into extract_session

LINE = "Sarah sent the drawings over this morning so that one is done"


def _second_pass(monkeypatch, suggestion, *, flag=True):
    prior = {"extracted_at": "2020-01-01T00:00:00Z",
             "topics": [{"topic_title": "t", "action_items": [
                 {"action": "Sarah to send the drawings", "item_id": PRIOR_ID}]}]}
    fake_s3 = FakeS3({SEG1_KEY: json.dumps(make_transcribe_json(LINE)),
                      OUT_KEY: json.dumps(prior)})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(les, "STATUS_SUGGESTIONS", flag)
    fake_llm, captured = _capturing_llm({"topics": [{"topic_title": "t", "action_items": [{
        "action": "Sarah to send the drawings", "responsible": "Sarah", "deadline": None,
        "priority": None, "continues": {"id": "A1", "starts": "Sarah to send the drawings"},
        "status_suggestion": suggestion}]}], "declared_site": None})
    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)
    out = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, min_interval_s=0)
    return out, captured["prompt"]


def test_extract_session_keeps_a_verified_suggestion_on_the_carried_item(monkeypatch):
    out, prompt = _second_pass(monkeypatch, {"status": "done", "at": "10:00:00", "quote": LINE})
    child = out["topics"][0]["action_items"][0]
    assert child["item_id"] == PRIOR_ID
    assert child["status_suggestion"]["status"] == "done"
    assert child["status_suggestion"]["segment_key"]
    assert out["status_suggestions"]["kept"] == 1
    assert "status_suggestion" in prompt          # the instruction rides on the prior block


def test_extract_session_drops_an_invented_quote(monkeypatch):
    out, _ = _second_pass(monkeypatch, {"status": "done", "at": "10:00:00",
                                        "quote": "we cancelled the crane booking for good now"})
    assert "status_suggestion" not in out["topics"][0]["action_items"][0]
    assert out["status_suggestions"]["kept"] == 0
    # Dropped by the quote check, not by an earlier rule passing this for the wrong reason.
    assert set(out["status_suggestions"]["dropped_by"]) == {"unverified"}


def test_flag_off_strips_and_leaves_no_trace(monkeypatch):
    out, prompt = _second_pass(monkeypatch, {"status": "done", "at": "10:00:00", "quote": LINE},
                               flag=False)
    assert "status_suggestion" not in json.dumps(out["topics"])
    assert "status_suggestions" not in out
    assert "status_suggestion" not in prompt


def test_no_prior_pass_means_no_instruction_and_no_suggestion(monkeypatch):
    fake_s3 = _one_segment_s3()
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(les, "STATUS_SUGGESTIONS", True)
    fake_llm, captured = _capturing_llm({"topics": [{"topic_title": "t", "action_items": [{
        "action": "x", "status_suggestion": {"status": "done", "at": "10:00:00",
                                             "quote": "hello world"}}]}],
        "declared_site": None})
    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)
    out = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, min_interval_s=0)
    assert "status_suggestion" not in captured["prompt"]
    assert "status_suggestion" not in json.dumps(out["topics"])
