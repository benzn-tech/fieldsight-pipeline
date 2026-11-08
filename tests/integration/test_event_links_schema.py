"""Integration: migration 0098 (event_links, claim_type) enforces its constraints on real Postgres."""
import datetime as _dt
import json

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
    t = topics.upsert_topic(
        db, s["id"], _dt.date(2026, 7, 2), "Concrete pour B2",
        action_items=[{"text": "Order rebar", "responsible": "Sam", "priority": "high"}],
    )
    ai_id, ai_stable = db.execute(
        "SELECT id, stable_id FROM action_items WHERE topic_id=%s", (t["id"],)
    ).fetchone()
    fi_id = db.execute(
        "INSERT INTO findings (topic_id, site_id, observation) VALUES (%s,%s,'crack') "
        "RETURNING id", (t["id"], s["id"]),
    ).fetchone()[0]
    return s, t, ai_id, ai_stable, fi_id


def _insert_link(db, site_id, *, src_kind="action_item", src_stable_id, link_type="supported_by",
                 ai=None, fi=None, dst_kind="turn", dst_stable_id=None, dst_turn=_TURN):
    return db.execute(
        """INSERT INTO event_links
               (site_id, link_type, src_kind, src_stable_id, src_action_item_id,
                src_finding_id, dst_kind, dst_stable_id, dst_turn, method, rule_version)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'quote_match','v1')
           RETURNING id""",
        (site_id, link_type, src_kind, src_stable_id, ai, fi, dst_kind, dst_stable_id,
         dst_turn),
    ).fetchone()[0]


def _rejected(db, **kw):
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():  # savepoint: keep the seed rows for the next assertion
            _insert_link(db, **kw)


def test_supported_by_requires_exactly_the_matching_row_fk(db):
    s, _, ai_id, ai_stable, fi_id = _seed(db)
    # Control: the matching kind + exactly one FK is accepted.
    assert _insert_link(db, s["id"], src_stable_id=ai_stable, ai=ai_id)
    # Wrong kind for the FK that is set.
    _rejected(db, site_id=s["id"], src_kind="finding", src_stable_id=ai_stable, ai=ai_id)
    # Two FKs set.
    _rejected(db, site_id=s["id"], src_stable_id=ai_stable, ai=ai_id, fi=fi_id)
    # No FK set.
    _rejected(db, site_id=s["id"], src_stable_id=ai_stable)


def test_turn_link_rejects_dst_stable_id(db):
    s, _, ai_id, ai_stable, _ = _seed(db)
    _rejected(db, site_id=s["id"], src_stable_id=ai_stable, ai=ai_id,
              dst_kind="turn", dst_stable_id=ai_stable)
    # A turn link without its turn payload is also rejected.
    _rejected(db, site_id=s["id"], src_stable_id=ai_stable, ai=ai_id,
              dst_kind="turn", dst_turn=None)


def test_item_link_requires_dst_stable_id(db):
    s, _, ai_id, ai_stable, _ = _seed(db)
    _rejected(db, site_id=s["id"], src_stable_id=ai_stable, ai=ai_id,
              dst_kind="finding", dst_stable_id=None, dst_turn=None)
    # Control: with dst_stable_id and no turn payload it is accepted.
    assert _insert_link(db, s["id"], src_stable_id=ai_stable, ai=ai_id,
                        dst_kind="finding", dst_stable_id=ai_stable, dst_turn=None)


def test_deleting_an_action_item_row_cascades_its_links(db):
    s, _, ai_id, ai_stable, _ = _seed(db)
    link_id = _insert_link(db, s["id"], src_stable_id=ai_stable, ai=ai_id)
    db.execute("DELETE FROM action_items WHERE id=%s", (ai_id,))
    assert db.execute("SELECT 1 FROM event_links WHERE id=%s", (link_id,)).fetchall() == []


def test_superseding_a_topic_keeps_its_links(db):
    s, t, ai_id, ai_stable, _ = _seed(db)
    link_id = _insert_link(db, s["id"], src_stable_id=ai_stable, ai=ai_id)
    db.execute("UPDATE topics SET superseded_at = now() WHERE id=%s", (t["id"],))
    row = db.execute(
        "SELECT src_action_item_id FROM event_links WHERE id=%s", (link_id,)
    ).fetchone()
    assert row is not None and row[0] == ai_id


@pytest.mark.parametrize("table", ["action_items", "findings", "topic_decisions",
                                   "topic_questions"])
def test_bad_claim_type_rejected(db, table):
    s, t, ai_id, _, fi_id = _seed(db)
    if table == "topic_decisions":
        row_id = db.execute(
            "INSERT INTO topic_decisions (topic_id, site_id, decision) VALUES (%s,%s,'d') "
            "RETURNING id", (t["id"], s["id"])).fetchone()[0]
    elif table == "topic_questions":
        row_id = db.execute(
            "INSERT INTO topic_questions (topic_id, site_id, question) VALUES (%s,%s,'q') "
            "RETURNING id", (t["id"], s["id"])).fetchone()[0]
    else:
        row_id = ai_id if table == "action_items" else fi_id
    # Control: a valid value and NULL (unknown) are accepted.
    db.execute(f"UPDATE {table} SET claim_type='stated' WHERE id=%s", (row_id,))
    db.execute(f"UPDATE {table} SET claim_type=NULL WHERE id=%s", (row_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(f"UPDATE {table} SET claim_type='guessed' WHERE id=%s", (row_id,))
