"""Unit: a supersession that is about to retire someone's check-off must say so.

`lambda_extract_session` computes `out_key` ONCE and writes both the live and the final tier
to that same key — the tier rides inside the artifact. `lambda_item_writer` supersedes rows
under that key before re-inserting (Track B Task 3: an UPDATE that stamps superseded_at, not
a DELETE), and the check-off IS `action_items.status`, a column on the now-superseded row.

So every final pass retires whatever a person ticked while the meeting was still running. The
row is not gone — Task 3 stopped that — but it is hidden from every read path until Task 4
carries it forward to its replacement by stable_id, so until Task 4 lands the effect a reader
sees is unchanged: the tick is unreachable.

This does not try to carry it forward yet. Carrying a human decision across a re-extraction
needs a rule, and the only safe rule is to match by stable_id — the model rewords, merges and
splits action items, so matching by text would risk putting a supervisor's tick on a
*different* item where nobody would ever see it. A missing tick is visible and recoverable; a
moved tick is neither.

What this function buys is a trace: a count that can be alarmed on and a log line that names
the key, so the next person to ask "did we lose ticks?" has an answer — and, once Task 4
lands, a count of exactly what it has to carry.

(Found while reconciling two span-deletion designs. I had asserted the opposite in an
earlier spec — "CASCADE has never fired for extraction topics" — and it was wrong. Track B
Task 3 then made the CASCADE itself moot: supersession is an UPDATE, so the row's children
never cascade away in the first place.)
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
    # The new wording (Track B Task 3): these are rows being SUPERSEDED, not destroyed --
    # a count of what Task 4 must carry forward, not a loss.
    assert any("superseded" in m for m in msgs), msgs
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
    clear = src.index("topics.supersede_topics_for_source(conn, extraction_key, run)")
    assert warn < clear, "the count must happen before the rows are superseded"
