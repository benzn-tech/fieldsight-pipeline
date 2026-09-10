"""Unit: on an authority-flip day, `lambda_item_writer` is the only writer.

Migration 0055 gave `topics` an `open_questions` column, `lambda_ingest` passes
the report's key through, `_TOPIC_COLS_JOINED` selects it back and
`render_report_shape` serializes it. All four were necessary and none of them
runs on most days.

Under the authority flip, a `(user, date)` with extraction topics makes ingest
DEFER -- it writes no report topics at all. Every topic in Aurora on that day
came through `lambda_item_writer`, and the extraction schema's key for this is
`questions` (`[{"question": ...}]`), which is what `chunking.py` and
`lambda_ask_agent.py` have always read.

So wiring the column, the ingest pass-through and the serializer while leaving
this one argument out produces exactly the same empty Open Questions section,
one layer further in. The defect does not die, it moves -- which is the reason
this file exists separately from the ingest test rather than as another case
inside it.

The test that missed it asserted that a LINE OF SOURCE existed in
`lambda_ingest`. Grepping one module for a spelling says nothing about a
different module's behaviour. These drive the writer.
"""
import json

import pytest

import lambda_item_writer as iw
from tests.unit.test_lambda_item_writer import (      # noqa: F401  (fixture)
    EXTRACTION_KEY, FakeS3, make_extraction, wired,
)


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


def test_the_extraction_questions_reach_the_upsert(wired):
    captured = _capture(wired, [_topic(questions=[
        {"question": "Is 3604 a 150 or a 200?"},
        {"question": "Who signs off the pour?"},
    ])])
    assert len(captured) == 1
    assert captured[0]["open_questions"] == [
        "Is 3604 a 150 or a 200?", "Who signs off the pour?"]


def test_a_plain_string_question_is_accepted_too(wired):
    """The meeting path produces strings; the extraction schema produces dicts.
    One column holds both, so the writer must not assume a shape."""
    captured = _capture(wired, [_topic(questions=["Is 3604 a 150?"])])
    assert captured[0]["open_questions"] == ["Is 3604 a 150?"]


def test_a_topic_with_no_questions_writes_null_not_an_empty_list(wired):
    """NULL means "not captured"; `[]` would claim the model was asked and
    found none. Every pre-0055 row is NULL and that is the honest value."""
    captured = _capture(wired, [_topic()])
    assert captured[0]["open_questions"] is None


def test_an_empty_question_list_is_also_null(wired):
    captured = _capture(wired, [_topic(questions=[])])
    assert captured[0]["open_questions"] is None


def test_blank_questions_are_dropped_rather_than_stored(wired):
    """A `{"question": ""}` renders as a bullet with nothing in it."""
    captured = _capture(wired, [_topic(questions=[
        {"question": ""}, {"question": None}, {}, {"question": "Real one"},
    ])])
    assert captured[0]["open_questions"] == ["Real one"]


def test_legacy_extractions_without_the_key_still_write(wired):
    """Pre-schema artifacts in S3 have no `questions` key at all. `.get` ->
    None, never a KeyError, exactly as time_range/participants behave."""
    captured = _capture(wired, [{"topic_title": "Old one", "category": "progress",
                                 "summary": "s", "action_items": [],
                                 "safety_flags": []}])
    assert captured[0]["open_questions"] is None
