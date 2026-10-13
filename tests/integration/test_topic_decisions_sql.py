"""Integration: decisions survive a real round trip through Aurora.

The unit suite drives FakeConn, which does not run the SQL, does not apply the
migration, and does not serialise jsonb. Everything the unit tests for this
column assert would pass there whether or not the column exists — the placeholder
count is the only thing they can check, and a count is not a type.

Two things only a real database can answer, and both have bitten this repo:

  * the INSERT now binds sixteen placeholders against sixteen columns. An
    off-by-one is green under FakeConn and a `ProgrammingError` in production.
  * `decisions` stores the extractor's OBJECTS, not strings. If the bind is
    wrong the dict comes back as a string and every reader downstream sees
    something that is not a decision — which is exactly the failure mode the
    payload-narrowing in lambda_org_api is designed around.

Mirrors test_topic_evidence_sql.py, including the rollback.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")

DECISIONS = [{"decision": "Door replacement in two phases: floors 1-3 by Tuesday",
              "rationale": "Access to level 4 is blocked until the hoist moves",
              "decided_by": "Site manager"}]


def _a_site(conn):
    row = conn.execute("SELECT id FROM sites LIMIT 1").fetchone()
    if not row:
        pytest.skip("no sites in this database")
    return row[0]


def test_decisions_round_trip_as_jsonb_objects():
    from repositories import topics
    with psycopg.connect(DSN) as conn:
        row = topics.upsert_topic(conn, _a_site(conn), "2026-09-16", "Doors",
                                  decisions=DECISIONS)
        assert row["decisions"] == DECISIONS, \
            "a list of dicts must come back a list of dicts — if it comes back a string the bind is wrong"
        assert row["decisions"][0]["rationale"], \
            "rationale is the reason this column stores objects rather than strings"
        conn.rollback()


def test_a_report_paths_plain_strings_round_trip_too():
    """One column holds both shapes: the report path passes strings."""
    from repositories import topics
    with psycopg.connect(DSN) as conn:
        row = topics.upsert_topic(conn, _a_site(conn), "2026-09-16", "Doors",
                                  decisions=["Phase the door replacement"])
        assert row["decisions"] == ["Phase the door replacement"]
        conn.rollback()


def test_omitting_decisions_stores_null():
    """The distinction the whole column design rests on: NULL means the
    extraction never captured any, `[]` would mean none were made."""
    from repositories import topics
    with psycopg.connect(DSN) as conn:
        row = topics.upsert_topic(conn, _a_site(conn), "2026-09-16", "Doors")
        assert row["decisions"] is None
        conn.rollback()


def test_an_empty_list_is_stored_as_an_empty_list():
    from repositories import topics
    with psycopg.connect(DSN) as conn:
        row = topics.upsert_topic(conn, _a_site(conn), "2026-09-16", "Doors",
                                  decisions=[])
        assert row["decisions"] == []
        conn.rollback()
