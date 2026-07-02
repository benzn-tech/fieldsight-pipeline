import pytest

pytestmark = pytest.mark.integration


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_readmodel_tables_exist(db):
    assert {"id", "site_id", "report_date", "category", "title", "summary"} <= _columns(db, "topics")
    assert {"id", "topic_id", "site_id", "text", "status", "deadline"} <= _columns(db, "action_items")
    assert {"id", "topic_id", "site_id", "observation", "risk_level"} <= _columns(db, "safety_observations")
    assert {"id", "topic_id", "s3_key", "caption_text"} <= _columns(db, "topic_photos")


from repositories import companies, sites, topics


def test_upsert_topic_with_children(db):
    co = companies.create_company(db, "Acme")
    s = sites.create_site(db, co["id"], "S1")
    t = topics.upsert_topic(
        db, s["id"], "2026-07-02", "Concrete pour B2",
        category="progress", summary="Poured level B2 slab.",
        action_items=[{"text": "Order rebar", "responsible": "Sam", "priority": "high"}],
        safety=[{"observation": "Edge unprotected", "risk_level": "high"}],
        photos=[{"s3_key": "reports/2026-07-02/x/p1.jpg", "caption_text": "slab"}],
    )
    assert t["title"] == "Concrete pour B2"

    listed = topics.list_site_topics(db, s["id"], "2026-07-02")
    assert len(listed) == 1 and listed[0]["id"] == t["id"]

    ai = db.execute("SELECT text FROM action_items WHERE topic_id=%s", (t["id"],)).fetchall()
    sf = db.execute("SELECT observation FROM safety_observations WHERE topic_id=%s", (t["id"],)).fetchall()
    ph = topics.get_topic_photos(db, t["id"])
    assert ai == [("Order rebar",)]
    assert sf == [("Edge unprotected",)]
    assert ph[0]["s3_key"].endswith("p1.jpg")
