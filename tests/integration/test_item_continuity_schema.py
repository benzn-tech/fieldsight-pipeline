"""0076 applied for real: item_id exists, is nullable, and stores a UUID on every child table."""
import uuid

import pytest

from repositories import companies, sites, topics

pytestmark = pytest.mark.integration


def _seed(db):
    co = companies.create_company(db, "Continuity-Schema-Co")
    s = sites.create_site(db, co["id"], "Continuity-Schema-Site")
    t = topics.upsert_topic(db, s["id"], "2026-09-30", "T")
    return s, t


@pytest.mark.parametrize("table,text_col", [
    ("action_items", "text"), ("findings", "observation"),
    ("topic_decisions", "decision"), ("topic_questions", "question")])
def test_item_id_round_trips_and_defaults_to_null(db, table, text_col):
    s, t = _seed(db)
    iid = uuid.uuid4()
    db.execute(f"INSERT INTO {table} (topic_id, site_id, {text_col}, item_id) VALUES (%s,%s,%s,%s)",
               (t["id"], s["id"], "with id", iid))
    db.execute(f"INSERT INTO {table} (topic_id, site_id, {text_col}) VALUES (%s,%s,%s)",
               (t["id"], s["id"], "without id"))
    rows = dict(db.execute(f"SELECT {text_col}, item_id FROM {table} WHERE topic_id=%s",
                           (t["id"],)).fetchall())
    assert rows["with id"] == iid
    assert rows["without id"] is None
