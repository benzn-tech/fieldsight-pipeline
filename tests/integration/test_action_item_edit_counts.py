"""Integration: the action-item edit count is valid SQL and counts the right rows.

The unit doubles record the SQL string and never parse it, so three properties of
the count can only be answered here: the `::uuid[]` cast (ids are passed as str;
uuid = text has no operator), the table_name filter (content_edits is polymorphic
-- a findings edit that happens to share nothing but a row_id must not count), and
GROUP BY returning no row for an unedited item.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import uuid

import pytest

from repositories import companies, sites, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-09-01"


def _seed(db):
    co = companies.create_company(db, f"Ver-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Ver-Site")
    u = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "v@x.nz", company_id=co["id"])
    src = f"extractions/VerFolder/{DATE}/sid{uuid.uuid4().hex}.json"
    t = topics.upsert_topic(
        db, s["id"], DATE, "Slab", user_id=u["id"], source_s3_key=src,
        action_items=[{"text": "Order timber"}, {"text": "Book pump"}])
    return co, s, t


def _edit(db, company_id, table, row_id, field="status"):
    db.execute(
        "INSERT INTO content_edits (company_id, table_name, row_id, field, before_text, after_text) "
        "VALUES (%s, %s, %s, %s, 'open', 'done')",
        (company_id, table, row_id, field))


def _items(rows):
    return {a["text"]: a for r in rows for a in r["action_items"]}


def test_unedited_items_count_zero(db):
    _co, s, _t = _seed(db)
    items = _items(topics.list_topics_for_date(db, [s["id"]], DATE))
    assert items["Order timber"]["edit_count"] == 0
    assert items["Book pump"]["edit_count"] == 0


def test_three_edits_on_any_field_count_three_in_both_reads(db):
    co, s, _t = _seed(db)
    aid = _items(topics.list_topics_for_date(db, [s["id"]], DATE))["Order timber"]["id"]
    for field in ("text", "deadline", "status"):
        _edit(db, co["id"], "action_items", aid, field)

    by_date = _items(topics.list_topics_for_date(db, [s["id"]], DATE))
    by_prefix = _items(topics.list_topics_for_source_prefix(db, f"extractions/VerFolder/{DATE}/"))
    assert by_date["Order timber"]["edit_count"] == 3
    assert by_prefix["Order timber"]["edit_count"] == 3
    assert by_date["Book pump"]["edit_count"] == 0


def test_an_edit_on_another_table_with_the_same_row_id_does_not_count(db):
    co, s, _t = _seed(db)
    aid = _items(topics.list_topics_for_date(db, [s["id"]], DATE))["Order timber"]["id"]
    _edit(db, co["id"], "findings", aid)
    assert _items(topics.list_topics_for_date(db, [s["id"]], DATE))["Order timber"]["edit_count"] == 0


def test_get_topic_full_carries_no_count(db):
    _co, _s, t = _seed(db)
    full = topics.get_topic_full(db, t["id"])
    assert full["action_items"] and all("edit_count" not in a for a in full["action_items"])
