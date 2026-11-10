"""Integration: migration 0102 (nightly re-match) -- nightly_reemits, claim_recompute_failures
and the four partial unique indexes on event_links -- enforces its constraints on real
Postgres. A fake connection never executes the SQL. All text is synthetic.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as _dt
import json
import os
import uuid

import pytest
from psycopg.errors import CheckViolation, UniqueViolation

from repositories import companies, event_links, sites, topics, users

pytestmark = pytest.mark.integration

MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                         "0102_nightly_rematch.sql")
DAY = _dt.date(2026, 11, 9)
TEXT_COL = {"action_item": "text", "finding": "observation", "decision": "decision",
            "question": "question"}


def _apply(db):
    with open(MIGRATION, encoding="utf-8") as fh:
        db.execute(fh.read())


def _seed(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"Co-0102-{tag}")
    site = sites.create_site(db, co["id"], f"S-0102-{tag}")
    folder = f"F0102-{tag}"
    user = users.upsert_field_only_user(db, co["id"], folder, "Fol", "Der", "worker")
    topic = topics.upsert_topic(db, site["id"], DAY, "Synthetic topic", user_id=user["id"],
                                source_s3_key=f"extractions/{folder}/{DAY}/sid{'a' * 32}.json")
    return co, site, topic


def _item(db, site, topic, kind):
    table = event_links.KIND_TABLE[kind]
    return db.execute(
        f"INSERT INTO {table} (topic_id, site_id, {TEXT_COL[kind]}) VALUES (%s,%s,%s) "
        f"RETURNING id", (topic["id"], site["id"], f"synthetic {kind}")).fetchone()[0]


def _link(db, site, kind, row_id, method):
    col = event_links.KIND_SRC_COLUMN[kind]
    db.execute(
        f"INSERT INTO event_links (site_id, link_type, src_kind, src_stable_id, {col}, "
        f"dst_kind, dst_turn, method, rule_version) "
        f"VALUES (%s,'supported_by',%s,%s,%s,'turn',%s::jsonb,%s,'v1')",
        (site["id"], kind, uuid.uuid4(), row_id,
         json.dumps({"source_stem": "stem-a", "start_sec": 1.0}), method))


def test_migration_idempotent(db):
    _apply(db)
    _apply(db)          # second run: no error
    tables = {r[0] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
        "AND table_name IN ('nightly_reemits','claim_recompute_failures')").fetchall()}
    assert tables == {"nightly_reemits", "claim_recompute_failures"}
    idx = {r[0] for r in db.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'event_links' "
        "AND indexname LIKE 'uq_event_links_nightly_%%'").fetchall()}
    assert idx == {"uq_event_links_nightly_ai", "uq_event_links_nightly_fi",
                   "uq_event_links_nightly_de", "uq_event_links_nightly_qu"}


@pytest.mark.parametrize("kind", list(event_links.KIND_TABLE))
def test_one_nightly_link_per_row(db, kind):
    _co, site, topic = _seed(db)
    row = _item(db, site, topic, kind)
    _link(db, site, kind, row, "nightly_turn_pick_v1")
    with pytest.raises(UniqueViolation):
        with db.transaction():
            _link(db, site, kind, row, "nightly_turn_pick_v2")
    # A different row of the same kind is unaffected.
    _link(db, site, kind, _item(db, site, topic, kind), "nightly_turn_pick_v1")


@pytest.mark.parametrize("kind", list(event_links.KIND_TABLE))
def test_nightly_index_ignores_lexical_links(db, kind):
    _co, site, topic = _seed(db)
    row = _item(db, site, topic, kind)
    _link(db, site, kind, row, "lexical_v1")
    _link(db, site, kind, row, "lexical_v1")            # lexical repeats were always allowed
    _link(db, site, kind, row, "nightly_turn_pick_v1")  # one nightly beside them
    n = db.execute("SELECT count(*) FROM event_links WHERE "
                   f"{event_links.KIND_SRC_COLUMN[kind]} = %s", (row,)).fetchone()[0]
    assert n == 3


def _reemit(db, site, *, line="xday", n="extractions/F/2026-11-08/n.json",
            l="extractions/F/2026-11-07/l.json", night=DAY):
    db.execute("INSERT INTO nightly_reemits (site_id, line, n_extraction_key, "
               "l_extraction_key, artifact_key, night) VALUES (%s,%s,%s,%s,'a/k.json',%s)",
               (site["id"], line, n, l, night))


def test_reemit_unique_per_pair_and_dropped_per_night(db):
    _co, site, _t = _seed(db)
    _reemit(db, site)
    with pytest.raises(UniqueViolation):
        with db.transaction():
            _reemit(db, site, night=DAY + _dt.timedelta(days=1))
    _reemit(db, site, line="reconcile")                 # the other line is its own key
    _reemit(db, site, l=None)
    with pytest.raises(UniqueViolation):
        with db.transaction():
            _reemit(db, site, l=None)
    _reemit(db, site, l=None, night=DAY + _dt.timedelta(days=1))
    with pytest.raises(CheckViolation):
        with db.transaction():
            _reemit(db, site, line="status")


def test_recompute_failure_reason_check_and_key(db):
    co, _site, _t = _seed(db)
    db.execute("INSERT INTO claim_recompute_failures (company_id, session_base, reason) "
               "VALUES (%s, 's1', 'timed_out')", (co["id"],))
    with pytest.raises(UniqueViolation):
        with db.transaction():
            db.execute("INSERT INTO claim_recompute_failures (company_id, session_base, "
                       "reason) VALUES (%s, 's1', 'failed')", (co["id"],))
    with pytest.raises(CheckViolation):
        with db.transaction():
            db.execute("INSERT INTO claim_recompute_failures (company_id, session_base, "
                       "reason) VALUES (%s, 's2', 'other')", (co["id"],))
    row = db.execute("SELECT failed_at IS NOT NULL, resolved_at FROM claim_recompute_failures "
                     "WHERE company_id = %s", (co["id"],)).fetchone()
    assert row == (True, None)


def test_company_and_site_delete_cascade(db):
    _co, site, _t = _seed(db)
    co = companies.create_company(db, f"Co-0102-bare-{uuid.uuid4().hex[:8]}")
    _reemit(db, site)
    db.execute("INSERT INTO claim_recompute_failures (company_id, session_base, reason) "
               "VALUES (%s, 's1', 'failed')", (co["id"],))
    db.execute("DELETE FROM sites WHERE id = %s", (site["id"],))
    assert db.execute("SELECT count(*) FROM nightly_reemits WHERE site_id = %s",
                      (site["id"],)).fetchone()[0] == 0
    db.execute("DELETE FROM companies WHERE id = %s", (co["id"],))
    assert db.execute("SELECT count(*) FROM claim_recompute_failures WHERE company_id = %s",
                      (co["id"],)).fetchone()[0] == 0


def test_nightly_requested_items_table(db):
    _apply(db)
    co, site, _topic = _seed(db)
    db.execute("INSERT INTO nightly_requested_items (site_id, night, company_id, items) "
               "VALUES (%s,%s,%s,3)", (site["id"], DAY, co["id"]))
    with pytest.raises(UniqueViolation):
        with db.transaction():
            db.execute("INSERT INTO nightly_requested_items (site_id, night, company_id, "
                       "items) VALUES (%s,%s,%s,1)", (site["id"], DAY, co["id"]))
    with pytest.raises(CheckViolation):
        with db.transaction():
            db.execute("INSERT INTO nightly_requested_items (site_id, night, company_id, "
                       "items) VALUES (%s,%s,%s,-1)",
                       (site["id"], DAY + _dt.timedelta(days=1), co["id"]))
