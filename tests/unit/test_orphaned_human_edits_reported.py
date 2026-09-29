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

The functions under test moved from lambda_item_writer.py into carry_forward_apply.py on the
final wave (Ruling R19), so lambda_ingest's report path can share them -- import path only,
same functions, same behaviour.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

cfa = pytest.importorskip("carry_forward_apply", reason="requires psycopg (installed in CI)")

KEY = "extractions/Ben_UCPK2/2026-08-17/sid9f8c1e2a4b6d47f0a1b2c3d4e5f60718.json"


def _emf_lines(capsys):
    out = capsys.readouterr().out.strip()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_it_warns_and_emits_the_metric_when_something_was_orphaned(caplog, capsys):
    with caplog.at_level("WARNING"):
        cfa._report_orphaned_human_edits(KEY, 3)
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
        cfa._report_orphaned_human_edits(KEY, 0)
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

    monkeypatch.setattr(cfa.json, "dumps", _boom)
    with caplog.at_level("WARNING"):
        cfa._report_orphaned_human_edits(KEY, 1)   # must not raise
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

    n = cfa._carry_forward_one_table(None, _EmptyOldRepo, ["old-topic"], ["new-topic"], "site-1")
    assert n == 0
    assert calls == [["old-topic"]], "must ask for the OLD pool once and stop there"


# ---------------------------------------------------------------------------------------
# Fix round 1 -- Ruling R10: carry_forward must DEGRADE, not abort the pass.
# ---------------------------------------------------------------------------------------

class _FakeTxn:
    """Mirrors psycopg3's real nested-transaction (SAVEPOINT) context manager only as far as
    the caller relies on it: enter, and let an exception propagate out of the `with` block
    rather than swallowing it -- same posture as test_lambda_item_writer.FakeConn's own
    `_FakeTransaction`. What it does NOT model is Postgres actually rolling back only the
    savepoint's own statements while leaving the enclosing transaction alive; that guarantee
    only exists against a real database, proven in
    tests/integration/test_supersede_two_passes.py."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def transaction(self):
        return _FakeTxn()


def test_carry_forward_children_degrades_instead_of_raising_on_a_matcher_crash(
        monkeypatch, caplog, capsys):
    """Ruling R10: a bug inside carry_forward must not propagate past `_carry_forward_children`
    -- Postgres aborts the WHOLE enclosing transaction on a raised SQL error, and a bare
    try/except cannot un-abort it, so the SAVEPOINT (`conn.transaction()`) is what makes
    "degrade, don't abort" actually true. This pins the CONTROL FLOW at the unit level: the
    exception is caught at this boundary, never reaches the caller, an ERROR-level
    `logger.exception` names the key, and the metric reports the human-touched fallback count
    (not 0) because nothing was actually carried. Whether the pass's own new topics really do
    survive is proven for real against Postgres in test_supersede_two_passes.py."""
    def _boom(*a, **k):
        raise RuntimeError("matcher exploded")

    monkeypatch.setattr(cfa, "_carry_forward_one_table", _boom)
    monkeypatch.setattr(cfa, "_count_human_touched_old", lambda *a, **k: 2)

    with caplog.at_level("WARNING"):
        cfa._carry_forward_children(  # must not raise
            _FakeConn(), ["old-topic"], ["new-topic"], "site-1", KEY)

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("carry_forward failed" in r.getMessage() and KEY in r.getMessage()
              for r in error_records), error_records
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("2" in r.getMessage() and KEY in r.getMessage() for r in warning_records), (
        "the WARNING must report the FALLBACK count, not silence", warning_records)

    lines = _emf_lines(capsys)
    assert len(lines) == 1, lines
    assert lines[0]["OrphanedHumanEdits"] == 2, (
        "a crash must not read as 0 orphans -- nothing was carried, so every human-touched "
        "old row IS one")


def test_count_human_touched_old_sums_all_four_tables(monkeypatch):
    class _Repo:
        def __init__(self, rows):
            self._rows = rows

        def list_for_carry_forward(self, conn, topic_ids, site_id):
            return self._rows

    monkeypatch.setattr(cfa, "action_items", _Repo([
        {"id": "a1", "human_touched": True}, {"id": "a2", "human_touched": False}]))
    monkeypatch.setattr(cfa, "findings", _Repo([{"id": "f1", "human_touched": True}]))
    # Track B Task 5: two more child tables joined the sum -- decisions/questions rows.
    monkeypatch.setattr(cfa, "topic_decisions", _Repo([{"id": "d1", "human_touched": True}]))
    monkeypatch.setattr(cfa, "topic_questions", _Repo([
        {"id": "q1", "human_touched": True}, {"id": "q2", "human_touched": False}]))

    n = cfa._count_human_touched_old(None, ["old-topic"], "site-1", KEY)
    assert n == 4


def test_count_human_touched_old_never_raises(monkeypatch, caplog):
    """The fallback counter is itself instrumentation, one layer further down -- if IT fails
    too there is no better number left, and a raise here would defeat the whole point of
    Ruling R10's SAVEPOINT one function up."""
    class _BoomRepo:
        @staticmethod
        def list_for_carry_forward(conn, topic_ids, site_id):
            raise RuntimeError("db gone")

    monkeypatch.setattr(cfa, "action_items", _BoomRepo)
    monkeypatch.setattr(cfa, "findings", _BoomRepo)

    with caplog.at_level("ERROR"):
        n = cfa._count_human_touched_old(None, ["old-topic"], "site-1", KEY)   # must not raise
    assert n == 0
    assert any(r.levelname == "ERROR" for r in caplog.records)
