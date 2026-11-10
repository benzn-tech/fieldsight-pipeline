"""Integration: migration 0101 makes a `continues` edge unique per (src, dst) on real Postgres.

The cross-day continuity writer inserts with ON CONFLICT DO NOTHING on this index, so a
re-run never duplicates an edge. Other link types (supported_by) must stay unaffected.
"""
import datetime as _dt
import json
import os

import pytest
import psycopg

from repositories import companies, sites, topics

pytestmark = pytest.mark.integration

_TURN = json.dumps({
    "source_stem": "rec_0001", "start_sec": 1.0, "end_sec": 3.5,
    "speaker_label": "spk_0", "quote_status": "exact",
})


def _seed(db):
    co = companies.create_company(db, "Acme")
    s = sites.create_site(db, co["id"], "S1")
    day1 = topics.upsert_topic(
        db, s["id"], _dt.date(2026, 7, 2), "Concrete pour B2",
        action_items=[{"text": "Order rebar", "responsible": "Sam", "priority": "high"}],
    )
    day2 = topics.upsert_topic(
        db, s["id"], _dt.date(2026, 7, 3), "Concrete pour B2",
        action_items=[{"text": "Order rebar", "responsible": "Sam", "priority": "high"}],
    )
    ai_id, ai_stable = db.execute(
        "SELECT id, stable_id FROM action_items WHERE topic_id=%s", (day2["id"],)
    ).fetchone()
    earlier_stable = db.execute(
        "SELECT stable_id FROM action_items WHERE topic_id=%s", (day1["id"],)
    ).fetchone()[0]
    return s, ai_id, ai_stable, earlier_stable


def _continues(db, site_id, src_stable_id, dst_stable_id):
    return db.execute(
        """INSERT INTO event_links
               (site_id, link_type, src_kind, src_stable_id, dst_kind, dst_stable_id,
                method, rule_version)
           VALUES (%s,'continues','action_item',%s,'action_item',%s,'xday','v1')
           RETURNING id""",
        (site_id, src_stable_id, dst_stable_id),
    ).fetchone()[0]


def _supported_by(db, site_id, src_stable_id, ai_id):
    return db.execute(
        """INSERT INTO event_links
               (site_id, link_type, src_kind, src_stable_id, src_action_item_id,
                dst_kind, dst_turn, method, rule_version)
           VALUES (%s,'supported_by','action_item',%s,%s,'turn',%s::jsonb,'quote_match','v1')
           RETURNING id""",
        (site_id, src_stable_id, ai_id, _TURN),
    ).fetchone()[0]


def test_duplicate_continues_edge_is_rejected(db):
    s, _, src, dst = _seed(db)
    assert _continues(db, s["id"], src, dst)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():  # savepoint: keep the first row for the next assertion
            _continues(db, s["id"], src, dst)
    # Control: the same src continuing a different item is a different edge.
    other = db.execute("SELECT gen_random_uuid()").fetchone()[0]
    assert _continues(db, s["id"], src, other)


def test_on_conflict_do_nothing_targets_the_index(db):
    s, _, src, dst = _seed(db)
    _continues(db, s["id"], src, dst)
    sql = """INSERT INTO event_links
                 (site_id, link_type, src_kind, src_stable_id, dst_kind, dst_stable_id,
                  method, rule_version)
             VALUES (%s,'continues','action_item',%s,'action_item',%s,'xday','v1')
             ON CONFLICT (src_stable_id, dst_stable_id) WHERE link_type = 'continues'
             DO NOTHING RETURNING id"""
    assert db.execute(sql, (s["id"], src, dst)).fetchall() == []
    n = db.execute(
        "SELECT count(*) FROM event_links WHERE link_type='continues' AND src_stable_id=%s",
        (src,),
    ).fetchone()[0]
    assert n == 1


def test_supported_by_rows_with_the_same_src_are_unaffected(db):
    s, ai_id, src, _ = _seed(db)
    assert _supported_by(db, s["id"], src, ai_id)
    assert _supported_by(db, s["id"], src, ai_id)
    n = db.execute(
        "SELECT count(*) FROM event_links WHERE link_type='supported_by' AND src_stable_id=%s",
        (src,),
    ).fetchone()[0]
    assert n == 2


def test_reapplying_the_migration_sql_raises_nothing(db):
    path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                        "0101_event_links_continues_unique.sql")
    with open(path, encoding="utf-8") as fh:
        db.execute(fh.read())
    n = db.execute(
        "SELECT count(*) FROM pg_indexes WHERE indexname = 'ux_event_links_continues'"
    ).fetchone()[0]
    assert n == 1
