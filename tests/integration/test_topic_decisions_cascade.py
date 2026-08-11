"""Integration: deleting a topic takes its decisions with it.

Item-writer's idempotency is delete-the-topics-then-reinsert, scoped by
source_s3_key. Decisions have no dedup of their own, so that idempotency holds
ONLY if the cascade fires -- otherwise every re-processed extraction leaves its
previous decisions behind and they accumulate silently.

`FakeConn` does not enforce foreign keys. The programme_tasks cascade defect
passed 1,598 unit tests before AND after the fix, which is why this one is here
and not in tests/unit.

Skips cleanly without TEST_DATABASE_URL (the test cluster is VPC-private); CI
provides one via its Postgres service.
"""
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


def test_deleting_a_topic_deletes_its_decisions():
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM topics LIMIT 1")
            row = cur.fetchone()
            if row is None:
                pytest.skip("no topics in the test database")
            topic_id = row[0]
            did = uuid.uuid4()
            cur.execute(
                "INSERT INTO topic_decisions (id, topic_id, decision) "
                "VALUES (%s,%s,%s)", (did, topic_id, "cascade probe"))
            cur.execute("SELECT count(*) FROM topic_decisions WHERE id = %s", (did,))
            assert cur.fetchone()[0] == 1, "the probe row did not insert"

            cur.execute("DELETE FROM topics WHERE id = %s", (topic_id,))
            cur.execute("SELECT count(*) FROM topic_decisions WHERE id = %s", (did,))
            assert cur.fetchone()[0] == 0, \
                "the cascade did not fire -- item-writer would accumulate " \
                "decisions on every re-processed extraction"
        conn.rollback()


def test_a_decision_cannot_point_at_a_topic_that_does_not_exist():
    """The foreign key, checked rather than assumed. Without it a mis-keyed
    write lands orphaned rows that no read path will ever surface."""
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cur.execute(
                    "INSERT INTO topic_decisions (topic_id, decision) "
                    "VALUES (%s,%s)", (uuid.uuid4(), "orphan"))
        conn.rollback()
