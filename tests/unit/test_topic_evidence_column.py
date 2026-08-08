"""Unit: evidence has to cross the Aurora boundary (P1-2 Task 5).

The S3 artifact is additive-tolerant -- a new key just appears and every reader
uses .get(). Aurora is the opposite: `upsert_topic` has an explicit INSERT
column list and `_TOPIC_COLS` an explicit SELECT list, so an `evidence` key in
the extraction JSON is SILENTLY DROPPED at the database boundary unless both are
changed. No error, no log; the topic simply arrives without its citations.

The /live-items precedent that suggests otherwise (findings appearing with no
serializer change) was about the REPOSITORY attaching a child dict -- the
generic serializer, not the SQL.
"""
import pytest

from tests.unit.test_meeting_session_repo import FakeConn

t = pytest.importorskip("repositories.topics", reason="requires psycopg")

iw = pytest.importorskip("lambda_item_writer", reason="requires the lambda deps")

EV = {"status": "verified",
      "quotes": [{"quote": "the slab pour is pushed to Thursday",
                  "status": "verified", "offset_sec": 4.0,
                  "segment_key": "audio_segments/Ben/2026-08-07/x.wav"}]}


def _insert_sql(conn):
    return conn.calls[0]["sql"].split("VALUES")[0]


def test_evidence_is_in_the_insert_columns():
    conn = FakeConn(results=[[{"id": "t1"}]])
    t.upsert_topic(conn, "site", "2026-08-07", "Slab", evidence=EV)
    assert "evidence" in _insert_sql(conn), \
        "the column list is explicit — without this the citations vanish silently"


def test_the_placeholder_count_still_matches_the_columns():
    # The failure mode of adding a column and forgetting its %s is a runtime
    # ProgrammingError on every single write, which FakeConn would not raise.
    conn = FakeConn(results=[[{"id": "t1"}]])
    t.upsert_topic(conn, "site", "2026-08-07", "Slab", evidence=EV)
    sql = conn.calls[0]["sql"]
    head, tail = sql.split("VALUES", 1)
    n_cols = len(head[head.index("(") + 1:head.rindex(")")].split(","))
    n_ph = tail[tail.index("(") + 1:tail.index(")")].count("%s")
    assert n_cols == n_ph, f"{n_cols} columns vs {n_ph} placeholders"
    assert len(conn.calls[0]["params"]) == n_ph


def test_evidence_is_bound_as_jsonb_not_a_python_object():
    # Every other jsonb column here goes through Jsonb(); passing a bare list
    # makes psycopg guess, and it guesses ARRAY.
    conn = FakeConn(results=[[{"id": "t1"}]])
    t.upsert_topic(conn, "site", "2026-08-07", "Slab", evidence=EV)
    assert any(type(p).__name__ == "Jsonb" for p in conn.calls[0]["params"])


def test_no_evidence_binds_null_rather_than_an_empty_json_list():
    # NULL means "this extraction predates the feature, or ran with the flag
    # off". `[]` would mean "cited nothing", which is a different claim.
    conn = FakeConn(results=[[{"id": "t1"}]])
    t.upsert_topic(conn, "site", "2026-08-07", "Slab")
    assert not any(type(p).__name__ == "Jsonb" and p.obj is not None
                   for p in conn.calls[0]["params"] if hasattr(p, "obj"))


def test_topic_cols_selects_evidence_back():
    assert "evidence" in t._TOPIC_COLS, \
        "written but never read back is the same as not written"


# ---- the payload the writer builds ------------------------------------

def test_an_unmeasured_topic_stores_null_not_an_empty_object():
    # Every topic extracted before this shipped, and every topic on prod while
    # the flag is off. If these stored {"status": None, "quotes": []} they would
    # read as "measured, cited nothing" and any coverage number would be wrong.
    assert iw._evidence_payload({"topic_title": "Slab"}) is None


def test_a_topic_that_cited_nothing_is_distinguishable_from_unmeasured():
    payload = iw._evidence_payload({"evidence_status": "absent"})
    assert payload == {"status": "absent", "quotes": []}


def test_the_rolled_up_status_travels_with_the_quotes():
    # Stored beside them, not re-derived: roll_up lives in evidence_match and
    # nothing reading the database has it.
    payload = iw._evidence_payload(
        {"evidence_status": "unverified",
         "evidence": [{"quote": "x", "status": "unverified"}]})
    assert payload["status"] == "unverified"
    assert len(payload["quotes"]) == 1
