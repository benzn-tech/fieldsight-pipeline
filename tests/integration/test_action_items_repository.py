import datetime as _dt

import pytest

from repositories import action_items, companies, sites, topics

pytestmark = pytest.mark.integration


def test_get_and_update_action_item_roundtrip(db):
    co = companies.create_company(db, "A")
    s = sites.create_site(db, co["id"], "S")
    t = topics.upsert_topic(db, s["id"], _dt.date(2026, 7, 18), "t")
    aid = db.execute(
        "INSERT INTO action_items (topic_id, site_id, text) VALUES (%s,%s,'do X') RETURNING id",
        (t["id"], s["id"]),
    ).fetchone()[0]

    row = action_items.get_action_item(db, str(aid))
    assert str(row["company_id"]) == str(co["id"])      # tenant guard column present
    assert row["status"] == "open"                       # default

    updated = action_items.update_action_item_fields(
        db, str(aid), {"status": "done", "priority": "high",
                       "responsible": "Neo Tan", "deadline": _dt.date(2026, 7, 20)}, "sub-9")
    assert updated["status"] == "done" and updated["priority"] == "high"
    assert updated["responsible"] == "Neo Tan"
    assert updated["updated_by"] == "sub-9" and updated["updated_at"] is not None


def test_get_action_item_malformed_uuid_is_none(db):
    assert action_items.get_action_item(db, "not-a-uuid") is None
