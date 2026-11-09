"""Integration: migration 0099 (site decision loop P0) -- site_events, event_runs, run_events,
decision_scan_seen, decision_cards, decision_ledger -- enforces its constraints on real
Postgres. A fake connection never executes the SQL.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import os
import uuid

import psycopg
import pytest

from repositories import companies, sites

pytestmark = pytest.mark.integration

MIGRATION_0099 = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                              "0099_site_decision_loop.sql")


def _apply(db):
    with open(MIGRATION_0099, encoding="utf-8") as fh:
        db.execute(fh.read())


def _seed(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"Co-0099-{tag}")
    s = sites.create_site(db, co["id"], f"S-0099-{tag}")
    return co["id"], s["id"]


def _event(db, co, site, *, dedupe_key, status="queued"):
    return db.execute(
        """INSERT INTO site_events (company_id, site_id, source, type, subject_ref,
                                    occurred_at, summary, dedupe_key, status, rule_id)
           VALUES (%s,%s,'checklist','checklist.gap','report:1', now(), 'gap', %s, %s,
                   'checklist.gap')
           RETURNING event_id""",
        (co, site, dedupe_key, status)).fetchone()[0]


def _card(db, co, site, *, card_key, status="open", kind="fact", shadow_label=None):
    ev = _event(db, co, site, dedupe_key="dk-" + uuid.uuid4().hex)
    return db.execute(
        """INSERT INTO decision_cards (event_id, company_id, site_id, rule_id, subject_ref,
                                       purpose, card_key, kind, question, consequence,
                                       options, evidence, urgency, recipients, status,
                                       shadow_label)
           VALUES (%s,%s,%s,'checklist.gap','report:1','gap',%s,%s,'Q?','C',
                   '[]'::jsonb,'[]'::jsonb,'normal','[]'::jsonb,%s,%s)
           RETURNING card_id""",
        (ev, co, site, card_key, kind, status, shadow_label)).fetchone()[0]


def _ledger(db, co, site, *, kind="decided", source="card"):
    return db.execute(
        """INSERT INTO decision_ledger (company_id, site_id, subject_ref, decision, kind,
                                        authority_basis, evidence, source)
           VALUES (%s,%s,'report:1','pour on Friday',%s,'pm',%s::jsonb,%s)
           RETURNING entry_id""",
        (co, site, kind, json.dumps([{"quote": "Friday"}]), source)).fetchone()[0]


def test_migration_is_idempotent(db):
    _apply(db)
    _apply(db)          # second run: no error
    tables = {r[0] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
        "AND table_name IN ('site_events','event_runs','run_events','decision_scan_seen',"
        "'decision_cards','decision_ledger')").fetchall()}
    assert tables == {"site_events", "event_runs", "run_events", "decision_scan_seen",
                      "decision_cards", "decision_ledger"}


def test_ledger_rejects_update_and_delete(db):
    co, site = _seed(db)
    entry = _ledger(db, co, site)
    with db.transaction():
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with db.transaction():
                db.execute("UPDATE decision_ledger SET decision='x' WHERE entry_id=%s", (entry,))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with db.transaction():
                db.execute("DELETE FROM decision_ledger WHERE entry_id=%s", (entry,))
    assert db.execute("SELECT decision FROM decision_ledger WHERE entry_id=%s",
                      (entry,)).fetchone()[0] == "pour on Friday"


def test_ledger_supersession_is_a_new_row(db):
    co, site = _seed(db)
    old = _ledger(db, co, site)
    new = db.execute(
        """INSERT INTO decision_ledger (company_id, site_id, subject_ref, decision, kind,
                                        source, supersedes_entry_id)
           VALUES (%s,%s,'report:1','pour on Monday','decided','meeting',%s)
           RETURNING entry_id, card_id, decided_at, evidence""",
        (co, site, old)).fetchone()
    assert new[1] is None              # card_id nullable (meeting decisions)
    assert new[2] is not None          # decided_at defaults to now()
    assert new[3] == []                # evidence defaults to an empty list


def test_ledger_rejects_truncate(db):
    co, site = _seed(db)
    _ledger(db, co, site)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with db.transaction():
            db.execute("TRUNCATE decision_ledger")


def test_run_events_carry_tenancy_not_null(db):
    cols = dict(db.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'run_events'").fetchall())
    assert cols["company_id"] == "NO" and cols["site_id"] == "NO"
    co, site = _seed(db)
    ev = _event(db, co, site, dedupe_key="dk-rt-" + uuid.uuid4().hex)
    run = db.execute(
        "INSERT INTO event_runs (company_id, site_id, handler) VALUES (%s,%s,'h') "
        "RETURNING run_id", (co, site)).fetchone()[0]
    with pytest.raises(psycopg.errors.NotNullViolation):
        with db.transaction():
            db.execute("INSERT INTO run_events (run_id, event_id) VALUES (%s,%s)", (run, ev))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.transaction():
            db.execute("INSERT INTO run_events (run_id, event_id, company_id, site_id) "
                       "VALUES (%s,%s,%s,%s)", (run, ev, uuid.uuid4(), site))


def test_decision_cards_event_index_exists(db):
    assert db.execute("SELECT to_regclass('idx_decision_cards_event') IS NOT NULL"
                      ).fetchone()[0]


def test_dedupe_index_allows_duplicate_rows_only_as_duplicate(db):
    co, site = _seed(db)
    _event(db, co, site, dedupe_key="dk-same")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            _event(db, co, site, dedupe_key="dk-same", status="queued")
    _event(db, co, site, dedupe_key="dk-same", status="duplicate")
    _event(db, co, site, dedupe_key="dk-same", status="duplicate")
    assert db.execute("SELECT count(*) FROM site_events WHERE dedupe_key='dk-same'"
                      ).fetchone()[0] == 3


def test_site_event_defaults_and_seq_order(db):
    co, site = _seed(db)
    a = _event(db, co, site, dedupe_key="dk-a-" + uuid.uuid4().hex)
    b = _event(db, co, site, dedupe_key="dk-b-" + uuid.uuid4().hex)
    rows = db.execute(
        "SELECT event_id, seq, bucket, chain_depth, attempts, received_at FROM site_events "
        "WHERE event_id IN (%s,%s) ORDER BY seq", (a, b)).fetchall()
    assert [r[0] for r in rows] == [a, b]
    assert rows[0][1] < rows[1][1]
    assert all(r[2] == "" and r[3] == 0 and r[4] == 0 and r[5] is not None for r in rows)


def test_one_open_card_per_key(db):
    co, site = _seed(db)
    _card(db, co, site, card_key="ck-1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            _card(db, co, site, card_key="ck-1")
    _card(db, co, site, card_key="ck-1", status="superseded")
    row = db.execute(
        "SELECT count(*), min(version), min(delivered_refs::text) FROM decision_cards "
        "WHERE card_key='ck-1'").fetchone()
    assert row == (2, 1, "[]")


def test_run_events_join_and_scan_seen(db):
    co, site = _seed(db)
    ev = _event(db, co, site, dedupe_key="dk-run-" + uuid.uuid4().hex)
    run = db.execute(
        "INSERT INTO event_runs (company_id, site_id, attempt, handler, outcome) "
        "VALUES (%s,%s,1,'checklist.gap','done') RETURNING run_id, model_calls",
        (co, site)).fetchone()
    assert run[1] == 0
    ins = ("INSERT INTO run_events (run_id, event_id, company_id, site_id) "
           "VALUES (%s,%s,%s,%s)")
    db.execute(ins, (run[0], ev, co, site))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute(ins, (run[0], ev, co, site))
    rid = uuid.uuid4()
    db.execute("INSERT INTO decision_scan_seen (report_id, result_key) VALUES (%s,'r1')", (rid,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute("INSERT INTO decision_scan_seen (report_id, result_key) VALUES (%s,'r1')",
                       (rid,))


@pytest.mark.parametrize("what", [
    "event_status", "run_outcome", "card_kind", "card_status", "shadow_label",
    "ledger_kind", "ledger_source"])
def test_check_constraints(db, what):
    co, site = _seed(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            if what == "event_status":
                _event(db, co, site, dedupe_key="dk-x-" + uuid.uuid4().hex, status="bogus")
            elif what == "run_outcome":
                db.execute("INSERT INTO event_runs (company_id, site_id, attempt, handler, "
                           "outcome) VALUES (%s,%s,1,'h','bogus')", (co, site))
            elif what == "card_kind":
                _card(db, co, site, card_key="ck-x", kind="bogus")
            elif what == "card_status":
                _card(db, co, site, card_key="ck-x", status="bogus")
            elif what == "shadow_label":
                _card(db, co, site, card_key="ck-x", shadow_label="bogus")
            elif what == "ledger_kind":
                _ledger(db, co, site, kind="bogus")
            else:
                _ledger(db, co, site, source="bogus")


def test_shadow_label_accepts_null_and_the_three_labels(db):
    co, site = _seed(db)
    for label in (None, "needed", "not_needed", "wrong_evidence"):
        _card(db, co, site, card_key="ck-l-" + uuid.uuid4().hex, shadow_label=label)
