"""Unit: the OrphanedHumanEdits report, once carry_forward has already tried to match.

Track B Task 4 replaces the old ex-ante warning (`_warn_if_discarding_checkoffs`, which
counted every closed action item about to be superseded, most of which would go on to find
their successor once carry_forward existed) with `_report_orphaned_human_edits`: a report of
what carry_forward's match actually failed to place -- an old, human-touched row with no
successor among this pass's new rows.

This file tests ONLY that reporting function (log + metric), plus `_carry_forward_one_table`'s
cheap-skip when a table's old pool is empty -- not the matching itself
(tests/unit/test_carry_forward.py) or the full repository wiring (the Step 5 integration test
in tests/integration/test_supersede_two_passes.py). Same granularity the file this replaces
tested at.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

iw = pytest.importorskip("lambda_item_writer")

KEY = "extractions/Ben_UCPK2/2026-08-17/sid9f8c1e2a4b6d47f0a1b2c3d4e5f60718.json"


def _emf_lines(capsys):
    out = capsys.readouterr().out.strip()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_it_warns_and_emits_the_metric_when_something_was_orphaned(caplog, capsys):
    with caplog.at_level("WARNING"):
        iw._report_orphaned_human_edits(KEY, 3)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("3" in m and KEY in m for m in msgs), msgs
    assert any("carry_forward" in m for m in msgs), msgs

    lines = _emf_lines(capsys)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line["OrphanedHumanEdits"] == 3
    assert line["key"] == KEY
    assert line["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "FieldSight/Pipeline"
    assert line["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Stage"]]
    assert line["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Name"] == "OrphanedHumanEdits"
    assert "Stage" in line


def test_zero_is_still_emitted_but_silent(caplog, capsys):
    """Ruling R5: a pass that retired something but carried everything forward cleanly must
    still print the metric AT ZERO -- otherwise "0 for that key" and "this key never ran" are
    indistinguishable on a dashboard. It must NOT warn: a WARNING on every ordinary
    supersession is exactly the noise the old function was built to avoid, and noise is how
    the next real orphan gets ignored."""
    with caplog.at_level("WARNING"):
        iw._report_orphaned_human_edits(KEY, 0)
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []

    lines = _emf_lines(capsys)
    assert len(lines) == 1, lines
    assert lines[0]["OrphanedHumanEdits"] == 0
    assert lines[0]["key"] == KEY


def test_a_failed_metric_emission_never_stops_the_extraction(monkeypatch, caplog):
    """This is instrumentation, same posture as the function it replaces: by the time this
    runs, the pass's rows are already written in the same transaction, so a metric that fails
    to print must not become a second failure on top of a working write."""
    def _boom(*a, **k):
        raise RuntimeError("stdout gone")

    monkeypatch.setattr(iw.json, "dumps", _boom)
    with caplog.at_level("WARNING"):
        iw._report_orphaned_human_edits(KEY, 1)   # must not raise
    assert any("could not emit" in r.getMessage() for r in caplog.records)


def test_carry_forward_one_table_skips_the_new_query_when_nothing_old_was_touched():
    """A topic that only ever produced findings (no action items) must cost the action_items
    table zero queries for the NEW pool -- there is nothing an empty old pool could match, so
    asking for `new_topic_ids` at all would be a wasted round-trip on the common case."""
    calls = []

    class _EmptyOldRepo:
        @staticmethod
        def list_for_carry_forward(conn, topic_ids, site_id):
            calls.append(list(topic_ids))
            return []

    n = iw._carry_forward_one_table(None, _EmptyOldRepo, ["old-topic"], ["new-topic"], "site-1")
    assert n == 0
    assert calls == [["old-topic"]], "must ask for the OLD pool once and stop there"
