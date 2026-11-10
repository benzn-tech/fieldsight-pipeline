"""Item quote locator wired into extract_session (spec 2026-11-10 4.2 Strip, 4.3).

`said_at` is a model-facing stamp: attach_support reads it (locator path) and the extractor
pops it from every artifact, whatever the flags say, after continuity and the linker.

Fixtures follow tests/unit/test_extract_session_item_support_flag.py and
tests/unit/test_extract_session_linker.py (FakeS3 + monkeypatched flags + call_llm double).
"""
import copy
import json
import uuid

import item_continuity
import item_locator
import item_support
from tests.unit.test_lambda_extract_session import (
    BUCKET,
    FakeS3,
    OUT_KEY,
    SEG1_KEY,
    SESSION_BASE,
    les,
    llm_utils,
    make_transcribe_json,
)

DATE = "2026-07-06"
TRANSCRIPT = "we need to fix the scaffold on level two before Friday"
# The one turn in SEG1 starts at the segment's base time.
STAMP = "10:00:00"


def _payload(said_at=STAMP):
    def s(child):
        if said_at is not None:
            child["said_at"] = said_at
        return child
    return {"topics": [{
        "topic_title": "Scaffold",
        "time_range": "10:00 – 10:01",
        "action_items": [s({"action": "Fix the scaffold on level two", "responsible": "Sam",
                            "deadline": "Friday", "priority": "high"})],
        "findings": [s({"observation": "Scaffold on level two is unsafe", "domain": "safety",
                        "severity": "major", "entity": {"name": None, "trade": None},
                        "recommended_action": None})],
        "decisions": [s({"decision": "Fix before Friday"})],
        "questions": [s({"question": "Who signs off the scaffold?"})],
    }], "declared_site": None}


class _Calls:
    """call_llm double: records prompts, answers from a list in order (last one repeats)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt, max_tokens=4096, force_json=False, enable_thinking=None, **kw):
        self.prompts.append(prompt)
        answer = self.answers[min(len(self.prompts), len(self.answers)) - 1]
        return json.dumps(answer), None


def _setup(monkeypatch, *, locator, support, answers, prior=None, continuity=False,
           linker=False):
    objects = {SEG1_KEY: json.dumps(make_transcribe_json(TRANSCRIPT))}
    if prior is not None:
        objects[OUT_KEY] = json.dumps(prior)
    fake_s3 = FakeS3(objects)
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", continuity)
    monkeypatch.setattr(les, "CONTINUITY_LINKER", linker)
    monkeypatch.setattr(les, "STATUS_SUGGESTIONS", False)
    monkeypatch.setattr(les, "EMIT_EVIDENCE", False)
    monkeypatch.setattr(les, "EMIT_ITEM_SUPPORT", support)
    monkeypatch.setattr(les, "EMIT_ITEM_LOCATOR", locator)
    fake = _Calls(answers)
    monkeypatch.setattr(llm_utils, "call_llm", fake)
    return fake_s3, fake


def _written(fake_s3):
    return json.loads(fake_s3.objects[OUT_KEY])


def _children(extraction):
    for topic in extraction.get("topics", []):
        for list_name in ("action_items", "findings", "decisions", "questions"):
            for child in topic.get(list_name) or []:
                yield child


def _no_said_at(fake_s3):
    return "said_at" not in fake_s3.objects[OUT_KEY]


def _record_resolve(monkeypatch):
    calls = []
    real = item_locator.resolve

    def _spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(item_locator, "resolve", _spy)
    return calls


# ---------------------------------------------------------------------------

def test_locator_and_support_on_link_and_strip(monkeypatch):
    fake_s3, fake = _setup(monkeypatch, locator=True, support=True, answers=[_payload()])

    les.extract_session(BUCKET, "Benl1", DATE, SESSION_BASE, final=True)

    assert "said_at" in fake.prompts[0]          # the final prompt asked for it
    children = list(_children(_written(fake_s3)))
    assert len(children) == 4
    for child in children:
        assert child["support"]["method"] == "locator_v1"
        assert child["support"]["status"] == "linked"
        assert child["support"]["start_sec"] == 0.0
        assert "said_at" not in child
    assert _no_said_at(fake_s3)


def test_locator_off_volunteered_said_at_is_stripped(monkeypatch):
    fake_s3, fake = _setup(monkeypatch, locator=False, support=False, answers=[_payload()])

    les.extract_session(BUCKET, "Benl1", DATE, SESSION_BASE, final=True)

    assert "said_at" not in fake.prompts[0]
    assert _no_said_at(fake_s3)


def test_locator_off_support_on_lexical_failure_still_strips(monkeypatch):
    # Uncalibrated lexical rule raises (all or nothing): no support, and the strip still runs.
    fake_s3, _ = _setup(monkeypatch, locator=False, support=True, answers=[_payload()])

    les.extract_session(BUCKET, "Benl1", DATE, SESSION_BASE, final=True)

    assert all("support" not in c for c in _children(_written(fake_s3)))
    assert _no_said_at(fake_s3)


def test_locator_on_support_off_stores_nothing(monkeypatch):
    fake_s3, fake = _setup(monkeypatch, locator=True, support=False, answers=[_payload()])
    calls = _record_resolve(monkeypatch)

    les.extract_session(BUCKET, "Benl1", DATE, SESSION_BASE, final=True)

    assert "said_at" in fake.prompts[0]
    assert calls == []
    assert all("support" not in c for c in _children(_written(fake_s3)))
    assert _no_said_at(fake_s3)


def test_shown_turns_are_passed_as_a_set(monkeypatch):
    _setup(monkeypatch, locator=True, support=True, answers=[_payload()])
    seen = []
    real = item_support.attach_support

    def _spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(item_support, "attach_support", _spy)

    les.extract_session(BUCKET, "Benl1", DATE, SESSION_BASE, final=True)

    assert seen[0]["locator"] is True
    assert seen[0]["shown"] == {0}


def test_continuity_fallback_path_strips(monkeypatch):
    # TrackB ask 1: normalise_children raising sends continuity to its best-effort fallback.
    fake_s3, _ = _setup(monkeypatch, locator=True, support=True, answers=[_payload()],
                        continuity=True)

    def _boom(topics):
        raise TypeError("synthetic")

    monkeypatch.setattr(item_continuity, "normalise_children", _boom)

    out = les.extract_session(BUCKET, "Benl1", DATE, SESSION_BASE, final=True)

    assert out["continuity"]["error"] == "TypeError"
    children = list(_children(_written(fake_s3)))
    assert len(children) == 4 and all("item_id" in c for c in children)
    assert _no_said_at(fake_s3)


def _prior(prior_ids):
    return {"extracted_at": "2020-01-01T00:00:00Z",
            "topics": [{"topic_title": "t", "action_items": [
                {"action": "Book the crane for Tuesday", "item_id": prior_ids[0],
                 "responsible": None, "deadline": None, "priority": None},
                {"action": "Order the scaffold boards", "item_id": prior_ids[1],
                 "responsible": None, "deadline": None, "priority": None},
            ]}]}


def _linker_answer(said_at):
    """Child 1 is reworded (needs the linker), child 2 a new item, child 3 exact text."""
    items = [{"action": "Crane booked for Tuesday morning"},
             {"action": "Sweep the level two deck"},
             {"action": "Order the scaffold boards"}]
    for it in items:
        it.update(responsible=None, deadline=None, priority=None)
        if said_at:
            it["said_at"] = STAMP
    return {"topics": [{"topic_title": "t", "time_range": "10:00 – 10:01",
                        "action_items": items}], "declared_site": None}


LINKS = {"links": [{"ref": "N1", "continues": {"id": "A1",
                                               "starts": "Book the crane for Tuesday"}}]}
PRIOR_IDS = ["11111111-1111-1111-1111-111111111111",
             "22222222-2222-2222-2222-222222222222"]


def _run_linked(monkeypatch, *, locator):
    fake_s3, _ = _setup(monkeypatch, locator=locator, support=True,
                        answers=[_linker_answer(said_at=locator)],
                        prior=_prior(PRIOR_IDS), continuity=True, linker=True)
    ref_texts = {}

    def _stub_linker(prior, topics, transcript_text, budget_deadline=None):
        refs = item_continuity.assign_refs(topics)
        for ref, _ln, child in refs:
            ref_texts[ref] = child.get("action")
        item_continuity.attach_links(copy.deepcopy(LINKS), refs)
        return {"status": "ok", "ms": 0, "dropped": 0, "model": None}

    monkeypatch.setattr(les, "_run_linker", _stub_linker)
    les.extract_session(BUCKET, "Benl1", DATE, SESSION_BASE, final=True)
    by_text = {c["action"]: c["item_id"] for c in _children(_written(fake_s3))}
    return by_text, ref_texts, fake_s3


def test_linker_links_land_on_the_same_children_with_locator_on(monkeypatch):
    # TrackB ask 2: CONTINUITY_LINKER + DECLARE_CONTINUITY + locator on.
    off_ids, off_refs, _ = _run_linked(monkeypatch, locator=False)
    on_ids, on_refs, on_s3 = _run_linked(monkeypatch, locator=True)

    assert on_refs and on_refs == off_refs
    assert on_ids["Crane booked for Tuesday morning"] == PRIOR_IDS[0]
    assert off_ids["Crane booked for Tuesday morning"] == PRIOR_IDS[0]
    assert on_ids["Order the scaffold boards"] == PRIOR_IDS[1]
    assert off_ids["Order the scaffold boards"] == PRIOR_IDS[1]
    # The unlinked new item gets a fresh id either way, never a prior one.
    for ids in (on_ids, off_ids):
        assert ids["Sweep the level two deck"] not in PRIOR_IDS
        uuid.UUID(ids["Sweep the level two deck"])
    assert _no_said_at(on_s3)


def test_live_tier_with_locator_on_asks_nothing_and_resolves_nothing(monkeypatch):
    fake_s3, fake = _setup(monkeypatch, locator=True, support=True,
                           answers=[_payload(said_at=None)])
    calls = _record_resolve(monkeypatch)

    live = les.extract_session(BUCKET, "Benl1", DATE, SESSION_BASE)

    assert live["tier"] == les.TIER_LIVE
    assert "said_at" not in fake.prompts[0]
    assert calls == []
    assert all("support" not in c for c in _children(_written(fake_s3)))


def test_live_tier_volunteered_said_at_is_stripped(monkeypatch):
    fake_s3, _ = _setup(monkeypatch, locator=True, support=True, answers=[_payload()])

    les.extract_session(BUCKET, "Benl1", DATE, SESSION_BASE)

    assert _no_said_at(fake_s3)
