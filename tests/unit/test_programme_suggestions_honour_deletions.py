"""Unit: a deleted recording's words must not survive in a programme suggestion.

`programme_progress_suggestions` (migration 0008) stores **frozen copies** of the topic's
text — `topic_title text NOT NULL`, `topic_summary text` — and its `topic_id` is
`ON DELETE SET NULL`. So the customer-facing delete could take the topic away and leave the
sentence sitting in this table, readable through the programme-feedback surface forever.

The feature's stated bar is that a *paraphrase* surviving in a summary is a failure. A
verbatim copy is worse. Found by a review of the span-deletion spec, which noticed that the
spec treated this table as an FK bookkeeping problem when the real issue is the denormalised
text beside the FK.

Both arms are required and the tests say why:

* the **topic** arm covers what is linked right now — but `topic_id` is SET NULL, so a
  suggestion whose topic was superseded has nothing left to match on;
* the **source** arm reads `source_s3_key`, which is NOT NULL here and is exactly what the
  recording tombstone holds, so it keeps matching after the link is gone.

Either one alone passes every test anyone writes today and leaks the moment the pipeline
supersedes that day.
"""
import os
import re
import sys

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(SRC, "repositories"))

ps = pytest.importorskip("repositories.programme_suggestions")


class _Cur:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append(sql)
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _Conn:
    def __init__(self):
        self.sql = []

    def cursor(self, row_factory=None):
        return _Cur(self.sql)


def test_the_predicate_carries_both_arms():
    p = ps.VISIBLE
    assert "target_id = programme_progress_suggestions.topic_id" in p, "topic arm missing"
    assert "source_s3_key LIKE r.target_key" in p, "source arm missing"
    assert p.count("scope = 'deleted'") == 1 and p.count("reverted_at IS NULL") == 1


def test_the_source_arm_is_what_survives_a_supersession():
    """`topic_id` is ON DELETE SET NULL (0008:7). Verified against the migration rather
    than assumed, because the whole argument for the second arm rests on it."""
    sql = open(os.path.join(SRC, "migrations", "0008_programme_suggestions.sql"),
               encoding="utf-8").read()
    assert re.search(r"topic_id\s+uuid REFERENCES topics\(id\) ON DELETE SET NULL", sql)
    assert re.search(r"topic_title\s+text NOT NULL", sql), \
        "the frozen copy is the reason this fix exists"
    assert re.search(r"source_s3_key\s+text NOT NULL", sql), \
        "the source arm needs this column to be present on every row"


def test_both_reads_apply_it():
    """A repository with one filtered read and one unfiltered one is the same leak with an
    extra step. `get()` is reached by the confirm/reject flow, not only by the list."""
    conn = _Conn()
    ps.list_for_site(conn, "s-1")
    ps.get(conn, "sug-1")
    assert len(conn.sql) == 2
    for sql in conn.sql:
        assert "redactions" in sql, f"unfiltered read: {sql[:90]}"


def test_no_read_in_this_module_is_left_unfiltered():
    """Enumerated rather than listed by hand: a read added later inherits the requirement,
    and this repo has shipped 'the guard exists but the new caller does not use it'."""
    src = open(os.path.join(SRC, "repositories", "programme_suggestions.py"),
               encoding="utf-8").read()
    selects = [m for m in re.findall(
        r"f?\"SELECT[^\"]*FROM programme_progress_suggestions.*?\"[,)]",
        src, re.S)]
    assert selects, "no SELECT found — did the query shape change?"
    for s in selects:
        assert "{VISIBLE}" in s or "VISIBLE" in s, f"unfiltered SELECT: {s[:100]}"
