"""Integration: migration 0095 -- action pack tables (spec 2026-11-01 §3 + §14 D4/S9/S16).

Checks against real Postgres what 0095 adds: the three tables, their CHECKs, the unique
keys, the one-active-pack-per-creator partial unique index, the sweep/list indexes, and
that the file is safe to run twice (the whole-directory apply/re-apply is
test_migrations_apply.py, which also picks this file up).

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import os
import uuid

import psycopg
import pytest

from repositories import companies, sites, users

pytestmark = pytest.mark.integration

MIGRATION_0095 = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                              "0095_action_packs.sql")


def _world(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"Co-0095-{tag}")
    site = sites.create_site(db, co["id"], f"Site-0095-{tag}")
    u = users.upsert_user(db, f"sub-0095-{tag}", f"u-{tag}@example.com", company_id=co["id"])
    return co, site, u


def _pack(db, co, site, u, state="queued", failure=None):
    return db.execute(
        "INSERT INTO action_packs (company_id, site_id, created_by, instruction, state, failure) "
        "VALUES (%s,%s,%s,'close #69',%s,%s) RETURNING id",
        (co["id"], site["id"], u["id"], state, failure)).fetchone()[0]


def _raises(db, exc, sql, params):
    with pytest.raises(exc):
        with db.transaction():
            db.execute(sql, params)


def test_pack_defaults_and_columns(db):
    co, site, u = _world(db)
    pid = _pack(db, co, site, u)
    row = db.execute(
        "SELECT state, failure, title, reading, conclusion, run_trace, models, as_of, "
        "heartbeat_at, archived_at, created_at IS NOT NULL, updated_at IS NOT NULL "
        "FROM action_packs WHERE id=%s", (pid,)).fetchone()
    assert row[:8] == ("queued", None, None, None, None, None, None, None)
    assert row[8] is not None                    # heartbeat_at defaults to now(): always sweepable
    assert row[9:] == (None, True, True)


def test_pack_checks_and_fks(db):
    co, site, u = _world(db)
    ins = ("INSERT INTO action_packs (company_id, site_id, created_by, instruction, state, failure) "
           "VALUES (%s,%s,%s,'x',%s,%s)")
    _raises(db, psycopg.errors.CheckViolation, ins, (co["id"], site["id"], u["id"], "done", None))
    _raises(db, psycopg.errors.CheckViolation, ins,
            (co["id"], site["id"], u["id"], "failed", "oops"))
    # failure is set exactly when state='failed'
    _raises(db, psycopg.errors.CheckViolation, ins, (co["id"], site["id"], u["id"], "failed", None))
    _raises(db, psycopg.errors.CheckViolation, ins,
            (co["id"], site["id"], u["id"], "ready", "budget"))
    for reason in ("budget", "model", "tools", "verifier", "internal", "company_inactive"):
        _pack(db, co, site, u, state="failed", failure=reason)
    for st in ("ready", "cancelled"):
        _pack(db, co, site, u, state=st)
    _raises(db, psycopg.errors.NotNullViolation,
            "INSERT INTO action_packs (company_id, site_id, created_by, instruction) "
            "VALUES (%s,NULL,%s,'x')", (co["id"], u["id"]))
    _raises(db, psycopg.errors.ForeignKeyViolation, ins,
            (co["id"], uuid.uuid4(), u["id"], "queued", None))
    _raises(db, psycopg.errors.ForeignKeyViolation, ins,
            (co["id"], site["id"], uuid.uuid4(), "queued", None))
    _raises(db, psycopg.errors.ForeignKeyViolation, ins,
            (uuid.uuid4(), site["id"], u["id"], "queued", None))


def test_one_active_pack_per_creator(db):
    co, site, u = _world(db)
    _pack(db, co, site, u, state="queued")
    with pytest.raises(psycopg.errors.UniqueViolation) as ei:
        with db.transaction():
            _pack(db, co, site, u, state="running")
    assert ei.value.diag.constraint_name == "action_packs_one_active_per_creator"
    # Finished packs do not count.
    _pack(db, co, site, u, state="ready")
    _pack(db, co, site, u, state="failed", failure="internal")
    _pack(db, co, site, u, state="cancelled")


def test_sources_checks_and_unique_ref(db):
    co, site, u = _world(db)
    pid = _pack(db, co, site, u)
    ins = "INSERT INTO action_pack_sources (pack_id, ref, kind, provenance) VALUES (%s,%s,%s,%s)"
    for i, (k, p) in enumerate((("topic", "records"), ("chunk", "records"), ("item", "records"),
                                ("document", "documents"), ("web_answer", "web"),
                                ("web_source", "web"))):
        db.execute(ins, (pid, f"R{i}", k, p))
    _raises(db, psycopg.errors.UniqueViolation, ins, (pid, "R0", "topic", "records"))
    _raises(db, psycopg.errors.CheckViolation, ins, (pid, "X1", "photo", "records"))
    _raises(db, psycopg.errors.CheckViolation, ins, (pid, "X2", "topic", "email"))
    # web provenance only for web kinds, and web kinds only with web provenance
    _raises(db, psycopg.errors.CheckViolation, ins, (pid, "X3", "topic", "web"))
    _raises(db, psycopg.errors.CheckViolation, ins, (pid, "X4", "web_source", "records"))
    _raises(db, psycopg.errors.ForeignKeyViolation, ins, (uuid.uuid4(), "R1", "topic", "records"))


def test_actions_checks_and_unique_seq(db):
    co, site, u = _world(db)
    pid = _pack(db, co, site, u)
    ins = ("INSERT INTO action_pack_actions (pack_id, seq, type, target_stable_id, refs, verifier, "
           "state) VALUES (%s,%s,%s,%s,%s,%s,%s)")
    sid = uuid.uuid4()
    db.execute(ins, (pid, 1, "close_item", sid, ["R1"], "supported", "proposed"))
    for i, t in enumerate(("create_item", "draft_rfi", "draft_email", "note"), start=2):
        db.execute(ins, (pid, i, t, None, ["R1", "D2"], "weak", "proposed"))
    db.execute(ins, (pid, 10, "update_item", sid, ["R1"], "unsupported", "stale"))
    for i, st in enumerate(("applied", "dismissed", "failed"), start=11):
        db.execute(ins, (pid, i, "note", None, ["R1"], "supported", st))
    st_default = db.execute("INSERT INTO action_pack_actions (pack_id, seq, type, refs, verifier) "
                            "VALUES (%s, 99, 'note', %s, 'weak') RETURNING state",
                            (pid, ["R1"])).fetchone()[0]
    assert st_default == "proposed"
    _raises(db, psycopg.errors.UniqueViolation, ins,
            (pid, 1, "note", None, ["R1"], "supported", "proposed"))
    _raises(db, psycopg.errors.CheckViolation, ins,
            (pid, 20, "delete_item", None, ["R1"], "supported", "proposed"))
    _raises(db, psycopg.errors.CheckViolation, ins,
            (pid, 21, "note", None, ["R1"], "maybe", "proposed"))
    _raises(db, psycopg.errors.CheckViolation, ins,
            (pid, 22, "note", None, ["R1"], "supported", "pending"))
    # at least one ref, always
    _raises(db, psycopg.errors.CheckViolation, ins,
            (pid, 23, "note", None, [], "supported", "proposed"))
    _raises(db, psycopg.errors.NotNullViolation, ins,
            (pid, 24, "note", None, None, "supported", "proposed"))
    # close/update must name their target
    _raises(db, psycopg.errors.CheckViolation, ins,
            (pid, 25, "close_item", None, ["R1"], "supported", "proposed"))
    _raises(db, psycopg.errors.CheckViolation, ins,
            (pid, 26, "update_item", None, ["R1"], "supported", "proposed"))


def test_children_cascade_with_the_pack(db):
    co, site, u = _world(db)
    pid = _pack(db, co, site, u)
    db.execute("INSERT INTO action_pack_sources (pack_id, ref, kind, provenance) "
               "VALUES (%s,'R1','topic','records')", (pid,))
    db.execute("INSERT INTO action_pack_actions (pack_id, seq, type, refs, verifier) "
               "VALUES (%s,1,'note',%s,'weak')", (pid, ["R1"]))
    db.execute("DELETE FROM action_packs WHERE id=%s", (pid,))
    assert db.execute("SELECT count(*) FROM action_pack_sources WHERE pack_id=%s",
                      (pid,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM action_pack_actions WHERE pack_id=%s",
                      (pid,)).fetchone()[0] == 0


_INDEXES = ("action_packs_one_active_per_creator", "action_packs_company_created",
            "action_packs_creator_state", "action_packs_site_created", "action_packs_sweep")


def test_indexes_exist_with_their_predicates(db):
    defs = dict(db.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='action_packs'").fetchall())
    for name in _INDEXES:
        assert name in defs, name
    assert "UNIQUE" in defs["action_packs_one_active_per_creator"]
    assert "(created_by)" in defs["action_packs_one_active_per_creator"]
    assert "queued" in defs["action_packs_one_active_per_creator"]
    assert "running" in defs["action_packs_one_active_per_creator"]
    assert "(company_id, created_at)" in defs["action_packs_company_created"]
    assert "(created_by, state)" in defs["action_packs_creator_state"]
    assert "(state, heartbeat_at)" in defs["action_packs_sweep"]
    assert "running" in defs["action_packs_sweep"] and "queued" in defs["action_packs_sweep"]


def test_migration_is_idempotent(db):
    with open(MIGRATION_0095, encoding="utf-8") as fh:
        sql = fh.read()
    db.execute(sql)
    db.execute(sql)
    co, site, u = _world(db)
    _pack(db, co, site, u)
    n = db.execute("SELECT count(*) FROM pg_indexes WHERE tablename='action_packs' "
                   "AND indexname = ANY(%s)", (list(_INDEXES),)).fetchone()[0]
    assert n == len(_INDEXES)
    n = db.execute("SELECT count(*) FROM pg_constraint WHERE conrelid IN "
                   "('action_packs'::regclass, 'action_pack_sources'::regclass, "
                   "'action_pack_actions'::regclass) AND contype='c'").fetchone()[0]
    assert n == 11
