import datetime as _dt

import pytest
import psycopg

from repositories import companies, sites, topics

pytestmark = pytest.mark.integration


def _seed_topic_with_action_item(db):
    co = companies.create_company(db, "Acme")
    s = sites.create_site(db, co["id"], "S1")
    t = topics.upsert_topic(
        db, s["id"], _dt.date(2026, 7, 2), "Concrete pour B2",
        action_items=[{"text": "Order rebar", "responsible": "Sam", "priority": "high"}],
    )
    ai = db.execute(
        "SELECT id, stable_id FROM action_items WHERE topic_id=%s", (t["id"],)
    ).fetchone()
    return co, s, t, ai


def test_action_item_keeps_its_stable_id_across_supersession(db):
    co, s, t, ai = _seed_topic_with_action_item(db)
    ai_id, stable_id = ai
    assert stable_id is not None

    db.execute("UPDATE topics SET superseded_at = now() WHERE id=%s", (t["id"],))

    row = db.execute(
        "SELECT stable_id FROM action_items WHERE id=%s", (ai_id,)
    ).fetchone()
    assert row is not None, "superseding the topic must not remove its children"
    assert row[0] == stable_id, "stable_id must survive supersession unchanged"


def test_deleting_the_topic_cascades_the_child_but_the_decision_record_survives(db):
    co, s, t, ai = _seed_topic_with_action_item(db)
    ai_id, stable_id = ai

    dr_id = db.execute(
        """INSERT INTO decision_records
               (company_id, site_id, kind, subject_type, subject_stable_id,
                provider, output, auto_outcome)
           VALUES (%s,%s,'programme_match','action_item',%s,'rule','{}'::jsonb,'accepted')
           RETURNING id""",
        (co["id"], s["id"], stable_id),
    ).fetchone()[0]

    db.execute("DELETE FROM topics WHERE id=%s", (t["id"],))

    remaining_ai = db.execute(
        "SELECT 1 FROM action_items WHERE id=%s", (ai_id,)
    ).fetchall()
    assert remaining_ai == [], "CASCADE from topics must remove the action_item"

    remaining_dr = db.execute(
        "SELECT 1 FROM decision_records WHERE id=%s", (dr_id,)
    ).fetchall()
    assert remaining_dr, (
        "decision_records is not FK-bound to the child it judged -- the record must "
        "outlive the row, by design"
    )


def test_live_source_index_hides_superseded_rows_from_a_partial_scan(db):
    co, s, t, ai = _seed_topic_with_action_item(db)
    db.execute("UPDATE topics SET superseded_at = now() WHERE id=%s", (t["id"],))
    live = db.execute(
        "SELECT id FROM topics WHERE source_s3_key = %s AND superseded_at IS NULL",
        (t["source_s3_key"],),
    ).fetchall()
    assert live == []


def test_findings_audience_check_rejects_a_bad_value(db):
    co, s, t, _ = _seed_topic_with_action_item(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO findings (topic_id, site_id, observation, audience) "
            "VALUES (%s,%s,'crack','everyone')",
            (t["id"], s["id"]),
        )
    db.rollback()


def test_findings_kind_check_rejects_a_bad_value(db):
    co, s, t, _ = _seed_topic_with_action_item(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO findings (topic_id, site_id, observation, kind) "
            "VALUES (%s,%s,'crack','made_up_kind')",
            (t["id"], s["id"]),
        )
    db.rollback()


def test_action_items_audience_check_rejects_a_bad_value(db):
    co, s, t, _ = _seed_topic_with_action_item(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO action_items (topic_id, site_id, text, audience) "
            "VALUES (%s,%s,'do X','everyone')",
            (t["id"], s["id"]),
        )
    db.rollback()


def test_a_row_inserted_without_the_new_columns_gets_the_defaults(db):
    co, s, t, _ = _seed_topic_with_action_item(db)
    row = db.execute(
        "INSERT INTO findings (topic_id, site_id, observation) VALUES (%s,%s,'crack in slab') "
        "RETURNING audience, kind, stable_id",
        (t["id"], s["id"]),
    ).fetchone()
    audience, kind, stable_id = row
    assert audience == "internal"
    assert kind == "observation"
    assert stable_id is not None
