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
