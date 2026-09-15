"""Unit: under the authority flip, a late extraction replaces the day's report topics.

Two guards met and deadlocked, found on TEST on 2026-09-15 while re-driving 2 and 3
Sep for Ben_UCPK2:

  item-writer   "nightly report already ingested -- late session extraction
                superseded": if report-sourced topics exist for (date, user),
                skip the extraction entirely.
  ingest        under AUTHORITY_FLIP, defer to extraction topics only if they
                ALREADY exist; otherwise delete them and write report topics.

Each waits for the other to have written first. A day whose nightly report was
ingested before its extraction -- a late upload, a re-extraction, any TEST day
ingested while the flip was off -- could therefore never become session-scoped
again: no meeting picker, no per-meeting report, whatever order the two ran in.

The guard's own comment states what the flip was meant to guarantee: once
AUTHORITY_FLIP defers, report topics "only exist for zero-extraction fallback
days". A day that HAS an extraction is by definition not one of those. So under
the flip the extraction wins: the report's topics for that day are removed and
the extraction is written. Report chunks survive -- chunks.topic_id is ON DELETE
SET NULL -- and the next ingest of that day's report re-links them to the
extraction topics on its defer path.

With the flip OFF nothing changes; test_lambda_item_writer.py pins that.

And the flag has to reach the function. item-writer reads it through
lambda_ingest.AUTHORITY_FLIP, which reads the environment, and the template only
ever gave AUTHORITY_FLIP to IngestFunction. Without the template line this fix
is dead on every deployed stack while every test above stays green.
"""
import json
import pathlib

import pytest

from tests.unit.test_lambda_item_writer import (  # noqa: F401
    EXTRACTION_KEY, FakeConn, iw, wired,
)

REPORT_KEY = "reports/2026-07-06/Jarley_Trainor/daily_report.json"


def _record(wired_mp, conn):
    wired_mp.setattr(iw, "get_connection", lambda *a, **k: conn)
    calls = []
    wired_mp.setattr(iw.topics, "delete_topics_for_source",
                     lambda c, key: calls.append(("delete", key)) or 0)
    wired_mp.setattr(iw.topics, "upsert_topic",
                     lambda *a, **k: calls.append(("upsert",)) or {"id": "t-new"})
    return calls


def test_under_the_flip_a_late_extraction_replaces_the_report_topics(wired):
    """THE test. Before the fix this returned skipped and wrote nothing."""
    wired.setattr(iw.lambda_ingest, "AUTHORITY_FLIP", True)
    calls = _record(wired, FakeConn(report_already_ingested=True))

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert not (isinstance(result, dict) and result.get("skipped")), result
    assert ("delete", REPORT_KEY) in calls, calls
    assert ("upsert",) in calls, "the extraction must actually be written"


def test_the_report_topics_go_before_the_extraction_is_written(wired):
    """Order matters: deleting after the upsert could only ever remove report rows,
    but deleting them first is what leaves the day with exactly one set."""
    wired.setattr(iw.lambda_ingest, "AUTHORITY_FLIP", True)
    calls = _record(wired, FakeConn(report_already_ingested=True))

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert calls.index(("delete", REPORT_KEY)) < calls.index(("upsert",)), calls


def test_only_that_days_report_for_that_person_is_removed(wired):
    """Keyed on the nightly report's own source key for (date, user) -- never a
    prefix, never another person's or another day's."""
    wired.setattr(iw.lambda_ingest, "AUTHORITY_FLIP", True)
    calls = _record(wired, FakeConn(report_already_ingested=True))

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    removed = [k for op, *rest in calls if op == "delete" for k in rest]
    assert set(removed) <= {REPORT_KEY, EXTRACTION_KEY}, removed


def test_without_the_flip_the_late_extraction_is_still_superseded(wired):
    """Unchanged behaviour when the flag is off -- the day stays report-sourced."""
    wired.setattr(iw.lambda_ingest, "AUTHORITY_FLIP", False)
    calls = _record(wired, FakeConn(report_already_ingested=True))

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result.get("skipped") is True
    assert calls == []


def test_a_day_with_no_report_topics_is_untouched_by_the_change(wired):
    """The ordinary case: no report ingested yet, so nothing report-sourced to remove."""
    wired.setattr(iw.lambda_ingest, "AUTHORITY_FLIP", True)
    calls = _record(wired, FakeConn(report_already_ingested=False))

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert ("delete", REPORT_KEY) not in calls


def _resource_body(name):
    text = (pathlib.Path(__file__).resolve().parents[2] / "src" / "template.yaml").read_text(encoding="utf-8")
    start = text.index(f"\n  {name}:\n")
    nxt = text.find("\n  ", start + 1)
    # advance to the next top-level resource (two-space indent followed by a letter)
    import re
    m = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    end = start + 1 + m.start() if m else len(text)
    return text[start:end]


def test_the_item_writer_function_is_given_the_flag():
    """Dead on every stack without this: the code reads the environment."""
    body = _resource_body("ItemWriterFunction")
    assert "AUTHORITY_FLIP: !Ref AuthorityFlip" in body, \
        "ItemWriterFunction must receive AUTHORITY_FLIP, as IngestFunction does"


def test_ingest_still_has_it_too():
    assert "AUTHORITY_FLIP: !Ref AuthorityFlip" in _resource_body("IngestFunction")
