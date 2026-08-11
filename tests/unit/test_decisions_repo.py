"""Unit: the decisions repository.

The extraction has asked the model for decisions since the schema was written,
and the model supplies them -- measured at 101 of 1,127 topics across 90 real
extractions. Nothing stored them: no column, no table, no reference in
item-writer. They survived only inside the S3 artifact.
"""
import pytest

dec = pytest.importorskip("repositories.decisions", reason="requires psycopg")

TOPIC = "11111111-1111-1111-1111-111111111111"


class FakeCursor:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append((sql, params))
        return self

    def fetchone(self):
        return {"id": "row", "topic_id": TOPIC}

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.calls = []

    def cursor(self, **kw):
        return FakeCursor(self.calls)


def test_each_decision_becomes_one_row():
    conn = FakeConn()
    rows = dec.insert_decisions(conn, TOPIC, [
        {"decision": "Seal over the fibre panel", "rationale": "no corrosion risk",
         "decided_by": "Mark"},
        {"decision": "Raise an RFI on the panel scope", "rationale": None,
         "decided_by": None},
    ])
    assert len(rows) == 2
    assert len(conn.calls) == 2
    assert "INSERT INTO topic_decisions" in conn.calls[0][0]
    assert conn.calls[0][1] == (TOPIC, "Seal over the fibre panel",
                                "no corrosion risk", "Mark")


def test_an_empty_list_touches_the_database_not_at_all():
    """Legacy extraction JSON has no `decisions` key, and the report/ingest
    path never has one."""
    conn = FakeConn()
    assert dec.insert_decisions(conn, TOPIC, []) == []
    assert conn.calls == []


def test_a_row_with_no_decision_text_is_skipped_not_inserted():
    """`insert_findings` passes `observation` straight into a NOT NULL column,
    so one malformed row aborts the whole topics transaction. Do not inherit
    that: a decision with nothing in it is dropped, and the rest still land."""
    conn = FakeConn()
    rows = dec.insert_decisions(conn, TOPIC, [
        {"decision": "", "rationale": "x"},
        {"rationale": "no decision key at all"},
        {"decision": "   ", "rationale": "whitespace only"},
        {"decision": "The real one", "rationale": None, "decided_by": None},
    ])
    assert len(rows) == 1
    assert conn.calls[0][1][1] == "The real one"


def test_a_non_dict_entry_does_not_crash_the_batch():
    conn = FakeConn()
    rows = dec.insert_decisions(conn, TOPIC, ["a bare string", None,
                                              {"decision": "kept"}])
    assert len(rows) == 1


def test_the_batched_read_is_one_query_for_many_topics():
    """N+1 across a day's topics is what this pattern exists to avoid."""
    conn = FakeConn()
    dec.list_for_topics(conn, [TOPIC, TOPIC])
    assert len(conn.calls) == 1
    assert "topic_id = ANY(%s)" in conn.calls[0][0]
