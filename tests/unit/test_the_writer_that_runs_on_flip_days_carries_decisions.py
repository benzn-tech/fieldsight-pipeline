"""Unit: on an authority-flip day, `lambda_item_writer` is the only writer.

The same blind spot that hid the open-questions defect one layer in applies here
unchanged, so it is written out rather than assumed.

Under the authority flip, a `(user, date)` with extraction topics makes ingest
DEFER — it writes no report topics at all. Every topic in Aurora on that day came
through `lambda_item_writer`. Wiring the column, the ingest pass-through and the
org-api serializer while leaving this one argument out produces exactly the same
empty Decisions section, one layer further in. The defect does not die, it moves.

The extraction schema's key is `decisions`, as a list of
`{decision, rationale, decided_by}` (`lambda_extract_session.py:939`). The report
path's key is `key_decisions`, as a list of strings. One column holds both, so
the writer must not assume a shape — and it must not flatten, because the whole
reason for a jsonb column is that `rationale` and `decided_by` are worth keeping
even though v1 does not send them.
"""
import json

import pytest

import lambda_item_writer as iw
from tests.unit.test_lambda_item_writer import (      # noqa: F401  (fixture)
    EXTRACTION_KEY, FakeS3, make_extraction, wired,
)

DECISION = "Door replacement in two phases: floors 1-3 by next Tuesday"


def _topic(**over):
    base = {
        "topic_title": "Ceiling grid",
        "category": "quality",
        "summary": "Walked level three.",
        "action_items": [],
        "safety_flags": [],
    }
    base.update(over)
    return base


def _capture(wired, topics):
    wired.setattr(iw, "_s3_client",
                  FakeS3({EXTRACTION_KEY: json.dumps(make_extraction(topics=topics))}))
    captured = []
    wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            captured.append(kw) or {"id": "topic-uuid-0"},
    )
    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)
    return captured


def test_the_extraction_decisions_reach_the_upsert(wired):
    captured = _capture(wired, [_topic(decisions=[
        {"decision": DECISION, "rationale": "Level 4 is blocked",
         "decided_by": "Site manager"},
    ])])
    assert len(captured) == 1
    assert captured[0]["decisions"] == [
        {"decision": DECISION, "rationale": "Level 4 is blocked",
         "decided_by": "Site manager"}]


def test_the_rationale_and_who_decided_are_not_thrown_away(wired):
    """v1 does not SEND them (no renderer reads them), which is not a reason to
    discard them on the way in. Re-extracting a superseded session to recover
    them is not possible — the transcript window may be gone."""
    captured = _capture(wired, [_topic(decisions=[
        {"decision": DECISION, "rationale": "Level 4 is blocked",
         "decided_by": "Site manager"},
    ])])
    stored = captured[0]["decisions"][0]
    assert stored["rationale"] == "Level 4 is blocked"
    assert stored["decided_by"] == "Site manager"


def test_a_plain_string_decision_is_accepted_too(wired):
    """The report path produces strings; the extraction schema produces dicts.
    One column holds both, so the writer must not assume a shape."""
    captured = _capture(wired, [_topic(decisions=[DECISION])])
    assert captured[0]["decisions"] == [DECISION]


def test_a_topic_with_no_decisions_writes_null_not_an_empty_list(wired):
    """NULL means "not captured"; `[]` would claim the model was asked and found
    none. Every pre-0058 row is NULL and that is the honest value."""
    captured = _capture(wired, [_topic()])
    assert captured[0]["decisions"] is None


def test_an_empty_decision_list_is_also_null(wired):
    captured = _capture(wired, [_topic(decisions=[])])
    assert captured[0]["decisions"] is None


def test_blank_decisions_are_dropped_rather_than_stored(wired):
    """A `{"decision": ""}` renders as a bullet with nothing in it."""
    captured = _capture(wired, [_topic(decisions=[
        {"decision": ""}, {"decision": None}, {}, "",
        {"decision": DECISION},
    ])])
    assert captured[0]["decisions"] == [{"decision": DECISION}]


def test_legacy_extractions_without_the_key_still_write(wired):
    """Pre-schema artifacts in S3 have no `decisions` key at all. `.get` ->
    None, never a KeyError, exactly as time_range/participants behave."""
    captured = _capture(wired, [{"topic_title": "Old one", "category": "progress",
                                 "summary": "s", "action_items": [],
                                 "safety_flags": []}])
    assert captured[0]["decisions"] is None
