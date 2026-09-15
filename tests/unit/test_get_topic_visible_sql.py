"""`get_topic_visible` -- the guards that run before SQL, and the SQL's text.

This file proves NOTHING about what the SQL returns: a fake connection records a
string and parses none of it. Semantics (non_work, redacted, NULL user_id,
deleted recording) are asserted against real Postgres in
tests/integration/test_get_topic_visible.py and, before merge, through the RDS
Data API in a rolled-back transaction (plan Task 2). What is pinned here is the
wiring: that every exclusion the spec lists is present in the statement that
runs, so deleting one is red without a database.
"""
import datetime
import uuid

import pytest

topics = pytest.importorskip("repositories.topics", reason="requires psycopg (installed in CI)")

TOPIC_ID = "df023596-1111-4222-8333-444455556666"
SITE_ID = "5c0e8d7a-1111-4222-8333-444455556666"
USER_ID = "0a0b0c0d-1111-4222-8333-444455556666"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        return self

    def fetchone(self):
        return self.conn.row

    def fetchall(self):
        return list(self.conn.items)


class FakeConn:
    def __init__(self, row=None, items=()):
        self.executed = []
        self.row = row
        self.items = items

    def cursor(self, row_factory=None):
        return FakeCursor(self)


@pytest.mark.parametrize("bad", ["not-a-uuid", "", None, 42, "df023596"])
def test_a_malformed_id_returns_none_without_touching_the_database(bad):
    """A non-uuid through `%(id)s::uuid` raises in Postgres, and a raise in
    rag-search surfaces as 'Search service temporarily unavailable'."""
    conn = FakeConn()
    assert topics.get_topic_visible(conn, bad, [SITE_ID], None) is None
    assert conn.executed == []


def test_no_reachable_sites_returns_none_without_a_query():
    conn = FakeConn()
    assert topics.get_topic_visible(conn, TOPIC_ID, [], None) is None
    assert conn.executed == []


def test_the_statement_carries_every_exclusion_the_spec_lists():
    sql = topics._TOPIC_VISIBLE_SQL
    assert "t.id = %(id)s::uuid" in sql
    assert "t.site_id = ANY(%(site_ids)s::uuid[])" in sql
    assert "%(author_ids)s::uuid[] IS NULL OR t.user_id = ANY(%(author_ids)s::uuid[])" in sql
    # both deleted-recording arms (deleted_predicates.visible_topics_predicate)
    assert "r.target_type = 'topic'" in sql and "r.target_type = 'recording'" in sql
    assert "t.work_class IS DISTINCT FROM 'non_work'" in sql
    # any active redaction, whatever its scope (redactions.company_excluded_topic_ids rule)
    assert ("NOT EXISTS (SELECT 1 FROM redactions ra WHERE ra.target_type = 'topic' "
            "AND ra.target_id = t.id AND ra.reverted_at IS NULL)") in sql


def test_action_items_use_the_visible_child_rule():
    assert topics.CHILD_OF_VISIBLE_TOPIC.format(alias="action_items") in \
        topics._TOPIC_VISIBLE_ACTION_ITEMS_SQL


def test_params_and_row_are_json_safe():
    row = {"id": uuid.UUID(TOPIC_ID), "title": "Scaffold handover", "summary": "Signed off.",
           "report_date": datetime.date(2026, 9, 3), "site_id": uuid.UUID(SITE_ID),
           "site_name": "UC PK", "user_id": uuid.UUID(USER_ID), "time_range": "09:10–09:40"}
    items = [{"text": "Send tag photos", "responsible": "Ben",
              "deadline": datetime.date(2026, 9, 5), "status": "open"}]
    conn = FakeConn(row=row, items=items)

    got = topics.get_topic_visible(conn, TOPIC_ID.upper(), [SITE_ID], [USER_ID])

    params = conn.executed[0][1]
    assert params == {"id": TOPIC_ID, "site_ids": [SITE_ID], "author_ids": [USER_ID]}
    assert conn.executed[1][1] == (TOPIC_ID,)
    assert got == {"id": TOPIC_ID, "title": "Scaffold handover", "summary": "Signed off.",
                   "report_date": "2026-09-03", "site_id": SITE_ID, "site_name": "UC PK",
                   "user_id": USER_ID, "time_range": "09:10–09:40",
                   "action_items": [{"text": "Send tag photos", "responsible": "Ben",
                                     "deadline": "2026-09-05", "status": "open"}]}


def test_a_null_author_stays_none():
    row = {"id": uuid.UUID(TOPIC_ID), "title": "t", "summary": None,
           "report_date": datetime.date(2026, 9, 3), "site_id": uuid.UUID(SITE_ID),
           "site_name": None, "user_id": None, "time_range": None}
    got = topics.get_topic_visible(FakeConn(row=row), TOPIC_ID, [SITE_ID], None)
    assert got["user_id"] is None
    assert got["action_items"] == []


def test_not_found_returns_none_and_skips_the_children_query():
    conn = FakeConn(row=None)
    assert topics.get_topic_visible(conn, TOPIC_ID, [SITE_ID], None) is None
    assert len(conn.executed) == 1
