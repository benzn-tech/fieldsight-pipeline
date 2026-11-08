"""Integration: migration 0098 (event_links, claim_type) enforces its constraints on real Postgres."""
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
                 ai=None, fi=None, de=None, qu=None, dst_kind="turn", dst_stable_id=None,
                 dst_turn=_TURN):
    return db.execute(
        """INSERT INTO event_links
               (site_id, link_type, src_kind, src_stable_id, src_action_item_id,
                src_finding_id, src_decision_id, src_question_id,
                dst_kind, dst_stable_id, dst_turn, method, rule_version)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'quote_match','v1')
           RETURNING id""",
        (site_id, link_type, src_kind, src_stable_id, ai, fi, de, qu, dst_kind,
         dst_stable_id, dst_turn),
    ).fetchone()[0]


def _seed_decision_and_question(db, s, t):
    de_id, de_stable = db.execute(
        "INSERT INTO topic_decisions (topic_id, site_id, decision) VALUES (%s,%s,'d') "
        "RETURNING id, stable_id", (t["id"], s["id"])).fetchone()
    qu_id, qu_stable = db.execute(
        "INSERT INTO topic_questions (topic_id, site_id, question) VALUES (%s,%s,'q') "
        "RETURNING id, stable_id", (t["id"], s["id"])).fetchone()
    return de_id, de_stable, qu_id, qu_stable


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


def test_supported_by_decision_and_question_branches(db):
    s, t, ai_id, _, fi_id = _seed(db)
    de_id, de_stable, qu_id, qu_stable = _seed_decision_and_question(db, s, t)
    # Control: each matching kind + its own FK is accepted (finding too, for completeness).
    assert _insert_link(db, s["id"], src_kind="decision", src_stable_id=de_stable, de=de_id)
    assert _insert_link(db, s["id"], src_kind="question", src_stable_id=qu_stable, qu=qu_id)
    assert _insert_link(db, s["id"], src_kind="finding", src_stable_id=de_stable, fi=fi_id)
    # Wrong kind for the FK that is set.
    _rejected(db, site_id=s["id"], src_kind="question", src_stable_id=de_stable, de=de_id)
    _rejected(db, site_id=s["id"], src_kind="decision", src_stable_id=qu_stable, qu=qu_id)
    # The matching FK plus a second one.
    _rejected(db, site_id=s["id"], src_kind="decision", src_stable_id=de_stable,
              de=de_id, qu=qu_id)
    _rejected(db, site_id=s["id"], src_kind="question", src_stable_id=qu_stable,
              qu=qu_id, ai=ai_id)


def test_resolves_row_without_src_fk_is_accepted(db):
    """Only supported_by is bound to a row FK; the reserved link types point by stable_id."""
    s, _, _, ai_stable, _ = _seed(db)
    other = db.execute("SELECT gen_random_uuid()").fetchone()[0]
    assert _insert_link(db, s["id"], link_type="resolves", src_stable_id=ai_stable,
                        dst_kind="action_item", dst_stable_id=other, dst_turn=None)


@pytest.mark.parametrize("kind", ["finding", "decision", "question"])
def test_deleting_the_src_row_cascades_its_links_for_every_kind(db, kind):
    s, t, _, _, fi_id = _seed(db)
    de_id, de_stable, qu_id, qu_stable = _seed_decision_and_question(db, s, t)
    table, row_id, kw = {
        "finding": ("findings", fi_id, {"fi": fi_id}),
        "decision": ("topic_decisions", de_id, {"de": de_id}),
        "question": ("topic_questions", qu_id, {"qu": qu_id}),
    }[kind]
    link_id = _insert_link(db, s["id"], src_kind=kind, src_stable_id=de_stable, **kw)
    db.execute(f"DELETE FROM {table} WHERE id=%s", (row_id,))
    assert db.execute("SELECT 1 FROM event_links WHERE id=%s", (link_id,)).fetchall() == []


def test_reapplying_the_migration_sql_raises_nothing(db):
    """The file must be safe to execute again on a DB that already has it (a re-recorded
    or hand-applied run), so every statement is IF NOT EXISTS or pg_constraint-guarded."""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                        "0098_event_links.sql")
    with open(path, encoding="utf-8") as fh:
        db.execute(fh.read())
    n = db.execute(
        "SELECT count(*) FROM pg_constraint WHERE conname LIKE '%_claim_type_check'"
    ).fetchone()[0]
    assert n == 4
