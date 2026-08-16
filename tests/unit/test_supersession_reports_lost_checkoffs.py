"""Unit: a supersession that discards someone's check-off must say so.

`lambda_extract_session` computes `out_key` ONCE and writes both the live and the final tier
to that same key — the tier rides inside the artifact. `lambda_item_writer` clears by that
key before re-inserting, which CASCADEs `action_items`, and the check-off IS
`action_items.status`, a column on the cascaded row.

So every final pass destroys whatever a person ticked while the meeting was still running.
That happens on prod today, with no deletion feature involved.

This does not try to prevent it. Carrying human decisions across a re-extraction needs a
rule, and the only safe rule is to ask a person — the model rewords, merges and splits
action items, and a confident wrong match puts a supervisor's tick on a *different* item
where nobody would ever see it. A missing tick is visible and recoverable; a moved tick is
neither.

What was missing is any trace at all: a silent permanent loss and "nothing was ticked"
produced identical output. Nobody can look for a problem that leaves nothing behind.

(Found while reconciling two span-deletion designs. I had asserted the opposite in an
earlier spec — "CASCADE has never fired for extraction topics" — and it was wrong.)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

iw = pytest.importorskip("lambda_item_writer")

KEY = "extractions/Ben_UCPK2/2026-08-17/sid9f8c1e2a4b6d47f0a1b2c3d4e5f60718.json"


class _Conn:
    def __init__(self, count):
        self.count = count
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        outer = self

        class _Cur:
            def fetchone(self):
                return [outer.count]
        return _Cur()


def test_it_warns_when_ticked_items_are_about_to_be_discarded(caplog):
    conn = _Conn(3)
    with caplog.at_level("WARNING"):
        iw._warn_if_discarding_checkoffs(conn, KEY)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("3" in m and KEY in m for m in msgs), msgs
    assert "status <> 'open'" in conn.sql[0], "closed is the only state worth reporting"


def test_it_is_silent_when_nothing_was_ticked(caplog):
    """The common case by far. A warning on every supersession would be noise, and noise is
    how the next real one gets ignored."""
    conn = _Conn(0)
    with caplog.at_level("WARNING"):
        iw._warn_if_discarding_checkoffs(conn, KEY)
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_a_failed_count_never_stops_the_extraction(caplog):
    """This is instrumentation. An extraction that cannot land because a COUNT failed would
    be a far worse outcome than the loss it is reporting."""
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

    with caplog.at_level("ERROR"):
        iw._warn_if_discarding_checkoffs(_Boom(), KEY)   # must not raise
    assert [r for r in caplog.records if r.levelname == "ERROR"], \
        "a failed count must still leave a trace"


def test_the_check_runs_before_the_clear_not_after():
    """After the CASCADE there is nothing left to count. Pinned by reading the source
    because the ordering is the whole of the correctness here, and a later edit that moves
    one line past the other would leave a check that always reports zero."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "src", "lambda_item_writer.py"), encoding="utf-8").read()
    warn = src.index("_warn_if_discarding_checkoffs(conn, extraction_key)")
    clear = src.index("topics.delete_topics_for_source(conn, extraction_key)")
    assert warn < clear, "the count must happen before the rows are gone"
