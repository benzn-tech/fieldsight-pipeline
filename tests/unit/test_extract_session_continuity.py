"""Tests for `extract_session` running continuity behind DECLARE_CONTINUITY (Task 4).

Spec: docs/superpowers/specs/2026-09-30-extractor-declares-item-continuity-design.md, D3, D8, D9, section 4.

Style and fixtures follow tests/unit/test_lambda_extract_session.py -- its FakeS3 double and
`_fake_call_llm_returning` helper are imported rather than re-implemented, since they already
carry the exact shape `extract_session` expects (get_object/get_paginator/put_object).

The flag is flipped with `monkeypatch.setattr(les, "DECLARE_CONTINUITY", ...)` rather than
reloading the module, per the task brief -- reload leaks module-level state (site cache,
whatever else a later change adds) across tests in ways a plain attribute set does not.
"""
import io
import json
import logging
import uuid

import pytest

from tests.unit.test_lambda_extract_session import (
    BUCKET,
    FakeNoSuchKey,
    FakeS3,
    OUT_KEY,
    SEG1_KEY,
    SESSION_BASE,
    les,
    llm_utils,
    make_transcribe_json,
)

OLD_AT = "2020-01-01T00:00:00Z"


def _one_segment_s3(extra_objects=None):
    objects = {SEG1_KEY: json.dumps(make_transcribe_json("hello world"))}
    objects.update(extra_objects or {})
    return FakeS3(objects)


def _capturing_llm(payload):
    """Like test_lambda_extract_session's `_fake_call_llm_returning`, but also hands back the
    prompt the caller sent, so a test can inspect it after the call."""
    captured = {}

    def _fake(prompt, max_tokens=4096, force_json=False, enable_thinking=None, **kw):
        captured["prompt"] = prompt
        return json.dumps(payload), None

    return _fake, captured


def _all_children(extraction):
    for topic in extraction.get("topics", []):
        for list_name in ("action_items", "findings", "decisions", "questions"):
            for child in topic.get(list_name) or []:
                yield child


class _CountingS3(FakeS3):
    """Counts every get_object call (successful or not) so a test can assert the flag being
    off adds no extra S3 GETs -- a failed lookup (NoSuchKey) is still a real API call."""

    def __init__(self, objects=None):
        super().__init__(objects)
        self.get_count = 0

    def get_object(self, Bucket, Key):
        self.get_count += 1
        return super().get_object(Bucket=Bucket, Key=Key)


class _SequencedS3(FakeS3):
    """Serves a DIFFERENT body on each successive get_object call for one chosen key, to
    simulate a wider pass publishing between this pass's continuity read and its later
    re-read (spec D9). Every other key behaves like the ordinary FakeS3 double. The last body
    in the sequence repeats for any call past the end."""

    def __init__(self, objects, sequenced_key, bodies):
        super().__init__(objects)
        self._sequenced_key = sequenced_key
        self._bodies = list(bodies)
        self._calls = 0

    def get_object(self, Bucket, Key):
        if Key != self._sequenced_key:
            return super().get_object(Bucket=Bucket, Key=Key)
        idx = min(self._calls, len(self._bodies) - 1)
        self._calls += 1
        body = self._bodies[idx]
        if body is None:
            raise FakeNoSuchKey()
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}


# ---------------------------------------------------------------------------
# 1. Flag off: byte-identical behaviour, no item_id, no continuity key, no
#    extra S3 reads.
# ---------------------------------------------------------------------------

def test_flag_off_writes_no_item_ids_and_no_continuity_key(monkeypatch):
    fake_off = _CountingS3({SEG1_KEY: json.dumps(make_transcribe_json("hello world"))})
    monkeypatch.setattr(les, "s3", lambda: fake_off)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", False)
    fake_llm, captured = _capturing_llm({
        "topics": [{"topic_title": "t", "action_items": [
            {"action": "Fix scaffold", "responsible": "Sam", "deadline": None, "priority": "high"},
        ]}],
        "declared_site": None,
    })
    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert "continuity" not in extraction
    for child in _all_children(extraction):
        assert "item_id" not in child
    assert "<<<PRIOR_ITEMS>>>" not in captured["prompt"]
    off_count = fake_off.get_count

    # Same scenario, flag on: exactly one extra GET (the new continuity read after the
    # gather). Everything else in the pass — the throttle's own early read, the segment
    # transcript read, the post-LLM re-read — happens either way.
    fake_on = _CountingS3({SEG1_KEY: json.dumps(make_transcribe_json("hello world"))})
    monkeypatch.setattr(les, "s3", lambda: fake_on)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(llm_utils, "call_llm", _capturing_llm({
        "topics": [{"topic_title": "t", "action_items": [
            {"action": "Fix scaffold", "responsible": "Sam", "deadline": None, "priority": "high"},
        ]}],
        "declared_site": None,
    })[0])

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert fake_on.get_count == off_count + 1


# ---------------------------------------------------------------------------
# 2. Flag on, first pass: no prior published -> no block, fresh ids everywhere.
# ---------------------------------------------------------------------------

def test_flag_on_first_pass_assigns_fresh_ids_without_a_block(monkeypatch):
    fake_s3 = _one_segment_s3()
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    fake_llm, captured = _capturing_llm({
        "topics": [{
            "topic_title": "t",
            "action_items": [
                {"action": "Fix scaffold", "responsible": "Sam", "deadline": None, "priority": "high"},
            ],
            "findings": [
                {"observation": "Missing guardrail", "domain": "safety", "severity": "major",
                 "entity": {"name": None, "trade": None}, "recommended_action": None},
            ],
            "decisions": ["Pour Tuesday"],   # plain-string form -- must be normalised too
        }],
        "declared_site": None,
    })
    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert "<<<PRIOR_ITEMS>>>" not in captured["prompt"]
    children = list(_all_children(extraction))
    assert len(children) == 3
    for child in children:
        assert uuid.UUID(child["item_id"])   # every child, every kind, a real uuid
    assert extraction["continuity"]["prior_count"] == 0
    assert extraction["continuity"]["prior_extracted_at"] is None
    assert extraction["continuity"]["prior_stale"] is False
    assert extraction["continuity"]["claims"] == []

    written = json.loads(fake_s3.objects[OUT_KEY])
    assert written["continuity"] == extraction["continuity"]


# ---------------------------------------------------------------------------
# 3. Flag on, second pass: the prior block is sent and an accepted claim
#    carries the prior item_id forward.
# ---------------------------------------------------------------------------

def test_flag_on_second_pass_sends_the_block_and_carries_an_accepted_claim(monkeypatch):
    prior_id = str(uuid.uuid4())
    prior_extraction = {
        "extracted_at": OLD_AT,
        "topics": [{
            "topic_title": "t",
            "action_items": [
                {"action": "Platform initial login using temporary password",
                 "item_id": prior_id, "responsible": None, "deadline": None, "priority": None},
            ],
        }],
    }
    fake_s3 = _one_segment_s3({OUT_KEY: json.dumps(prior_extraction)})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    fake_llm, captured = _capturing_llm({
        "topics": [{
            "topic_title": "t",
            "action_items": [
                {"action": "Platform login using temporary password then change it",
                 "responsible": None, "deadline": None, "priority": None,
                 "continues": {"id": "A1", "starts": "Platform initial login using"}},
            ],
        }],
        "declared_site": None,
    })
    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                     min_interval_s=0)

    assert "<<<PRIOR_ITEMS>>>" in captured["prompt"]
    assert '"alias": "A1"' in captured["prompt"]

    child = extraction["topics"][0]["action_items"][0]
    assert child["item_id"] == prior_id
    assert "continues" not in child

    claims = extraction["continuity"]["claims"]
    assert len(claims) == 1
    assert claims[0]["outcome"] == "accepted"
    assert claims[0]["alias"] == "A1"
    assert claims[0]["prior_item_id"] == prior_id
    assert claims[0]["new_item_id"] == prior_id


# ---------------------------------------------------------------------------
# 4. Final pass reads the published extraction after the gather (today it
#    never does).
# ---------------------------------------------------------------------------

def test_final_pass_reads_the_published_extraction_after_the_gather(monkeypatch):
    prior_id = str(uuid.uuid4())
    prior_extraction = {
        "extracted_at": OLD_AT,
        "topics": [{
            "topic_title": "t",
            "action_items": [
                {"action": "Book the crane for Tuesday", "item_id": prior_id,
                 "responsible": None, "deadline": None, "priority": None},
            ],
        }],
    }
    fake_s3 = _one_segment_s3({OUT_KEY: json.dumps(prior_extraction)})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    fake_llm, captured = _capturing_llm({"topics": [], "declared_site": None})
    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                     final=True)

    assert "<<<PRIOR_ITEMS>>>" in captured["prompt"]
    assert '"alias": "A1"' in captured["prompt"]
    assert extraction["continuity"]["prior_count"] == 1


# ---------------------------------------------------------------------------
# 5. UNKNOWN published extraction: no block, fresh ids, WARNING, final pass
#    still writes.
# ---------------------------------------------------------------------------

def test_unknown_published_extraction_sends_no_block_and_does_not_skip_final(monkeypatch, caplog):
    fake_s3 = _one_segment_s3()
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(les, "read_existing_extraction", lambda bucket, key: les.UNKNOWN)
    fake_llm, captured = _capturing_llm({
        "topics": [{"topic_title": "t", "action_items": [
            {"action": "Fix scaffold", "responsible": None, "deadline": None, "priority": None},
        ]}],
        "declared_site": None,
    })
    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)
    caplog.set_level(logging.WARNING)

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                     final=True)

    assert extraction is not None
    assert "<<<PRIOR_ITEMS>>>" not in captured["prompt"]
    assert extraction["continuity"]["prior_count"] == 0
    child = extraction["topics"][0]["action_items"][0]
    assert uuid.UUID(child["item_id"])
    assert any("continuity" in r.getMessage() and "cannot read the published extraction" in r.getMessage()
              for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. prior_stale: the published extraction changes between the continuity
#    read and the write-time re-read.
# ---------------------------------------------------------------------------

def test_prior_stale_is_set_when_the_published_extraction_changes_before_the_write(monkeypatch):
    t1 = json.dumps({"extracted_at": OLD_AT, "topics": []})
    t2 = json.dumps({"extracted_at": "2020-01-01T00:05:00Z", "topics": []})
    # Calls to OUT_KEY, in order: (1) throttle's own early read, (2) the new continuity
    # read after the gather, (3) the post-LLM re-read reused for prior_stale.
    fake_s3 = _SequencedS3(
        {SEG1_KEY: json.dumps(make_transcribe_json("hello world"))},
        sequenced_key=OUT_KEY, bodies=[t1, t1, t2],
    )
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(llm_utils, "call_llm",
                        _capturing_llm({"topics": [], "declared_site": None})[0])

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                     min_interval_s=0)

    assert extraction["continuity"]["prior_extracted_at"] == OLD_AT
    assert extraction["continuity"]["prior_stale"] is True


def test_prior_stale_is_false_when_nothing_changed(monkeypatch):
    """The negative companion to the True case above -- on its own this test cannot catch a
    deleted prior_stale assignment (both start and stay False), so it only means something read
    alongside that one. Also covers 'nothing published before and nothing now' via the first-pass
    scenario in test 2 above (both None)."""
    body = json.dumps({"extracted_at": OLD_AT, "topics": []})
    fake_s3 = _SequencedS3(
        {SEG1_KEY: json.dumps(make_transcribe_json("hello world"))},
        sequenced_key=OUT_KEY, bodies=[body, body, body],
    )
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(llm_utils, "call_llm",
                        _capturing_llm({"topics": [], "declared_site": None})[0])

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                     min_interval_s=0)

    assert extraction["continuity"]["prior_stale"] is False


# ---------------------------------------------------------------------------
# 7. The continuity write is logged at WARNING (prod drops INFO).
# ---------------------------------------------------------------------------

def test_the_continuity_write_log_line_is_warning_level(monkeypatch, caplog):
    fake_s3 = _one_segment_s3()
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(llm_utils, "call_llm",
                        _capturing_llm({
                            "topics": [{"topic_title": "t", "action_items": [
                                {"action": "Fix scaffold", "responsible": None,
                                 "deadline": None, "priority": None},
                            ]}],
                            "declared_site": None,
                        })[0])
    caplog.set_level(logging.WARNING)

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    matches = [r for r in caplog.records
              if r.getMessage().startswith("continuity_write key=")]
    assert len(matches) == 1
    assert matches[0].levelname == "WARNING"
    assert f"key={OUT_KEY}" in matches[0].getMessage()
    assert "claims=0 accepted=0" in matches[0].getMessage()
    assert extraction["continuity"]["claims"] == []


# ---------------------------------------------------------------------------
# A live pass that stands down must return exactly what's published — no
# continuity mutation leaks into that return value.
# ---------------------------------------------------------------------------

def test_a_standing_down_live_pass_returns_the_published_extraction_unmutated(monkeypatch):
    published = {
        "extracted_at": OLD_AT, "tier": les.TIER_LIVE,
        "source_transcripts": [SEG1_KEY.rsplit("/", 1)[-1]],
        "topics": [],
    }
    fake_s3 = _one_segment_s3({OUT_KEY: json.dumps(published)})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(llm_utils, "call_llm",
                        _capturing_llm({"topics": [], "declared_site": None})[0])

    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                 min_interval_s=0)

    assert result == published                # unchanged: no item_id, no continuity key
    assert len(fake_s3.put_calls) == 0


# ---------------------------------------------------------------------------
# Fix round 1: a malformed child field (the model sends a bare int/string instead of a
# list) must not lose the whole extraction. normalise_children/resolve are optional, model-
# output-shaped steps -- same posture as verify_evidence and _find_self_introductions.
# ---------------------------------------------------------------------------

def test_a_malformed_child_field_falls_back_to_fresh_ids_instead_of_losing_the_extraction(
        monkeypatch, caplog):
    fake_s3 = _one_segment_s3()
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", True)
    monkeypatch.setattr(llm_utils, "call_llm", _capturing_llm({
        "topics": [
            {
                "topic_title": "malformed",
                "decisions": 5,              # not a list -- normalise_children iterates it
                "action_items": "text",      # not a list either, different shape again
            },
            {
                "topic_title": "fine",
                "action_items": [
                    {"action": "Fix scaffold", "responsible": None, "deadline": None,
                     "priority": None},
                ],
            },
        ],
        "declared_site": None,
    })[0])
    caplog.set_level(logging.ERROR)

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert extraction is not None
    assert len(fake_s3.put_calls) == 1
    # The well-formed sibling topic's child still gets an item_id -- one malformed field
    # elsewhere does not stop the rest of the pass.
    good_child = extraction["topics"][1]["action_items"][0]
    assert uuid.UUID(good_child["item_id"])
    # The malformed fields themselves are left as the model sent them -- untouched, not guessed.
    assert extraction["topics"][0]["decisions"] == 5
    assert extraction["topics"][0]["action_items"] == "text"
    assert extraction["continuity"]["claims"] == []
    assert extraction["continuity"]["error"] == "TypeError"
    assert any(r.levelname == "ERROR" and "continuity" in r.getMessage()
              for r in caplog.records)

    written = json.loads(fake_s3.objects[OUT_KEY])
    assert written["continuity"]["error"] == "TypeError"
