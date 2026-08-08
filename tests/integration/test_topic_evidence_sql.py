"""Integration: evidence survives a real round trip through Aurora.

The unit suite drives FakeConn, which does not run the SQL, does not apply the
migration, and does not serialise jsonb. Everything this file asserts would pass
there whether or not the column exists.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")

PAYLOAD = {"status": "verified",
           "quotes": [{"quote": "the slab pour is pushed to Thursday",
                       "status": "verified", "offset_sec": 4.0,
                       "segment_key": "audio_segments/a/2026-08-07/b.wav"}]}


def _a_site(conn):
    row = conn.execute("SELECT id FROM sites LIMIT 1").fetchone()
    if not row:
        pytest.skip("no sites in this database")
    return row[0]


def test_evidence_round_trips_as_jsonb():
    from repositories import topics
    with psycopg.connect(DSN) as conn:
        site = _a_site(conn)
        row = topics.upsert_topic(conn, site, "2026-08-07", "Slab",
                                  evidence=PAYLOAD)
        assert row["evidence"] == PAYLOAD, \
            "a dict must come back a dict — if it comes back a string the bind is wrong"
        conn.rollback()


def test_omitting_evidence_stores_null():
    # The distinction the whole column design rests on.
    from repositories import topics
    with psycopg.connect(DSN) as conn:
        row = topics.upsert_topic(conn, _a_site(conn), "2026-08-07", "Slab")
        assert row["evidence"] is None
        conn.rollback()
