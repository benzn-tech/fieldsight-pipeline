"""Integration: per-site task numbers (action packs Task 1, spec §14 D1) -- migration 0093,
the repository helpers and the backfill script, against a real Postgres (skipped without
TEST_DATABASE_URL -- a skip is NOT a pass).

The writer/ingest wiring (carry across re-extraction, deploy-window survival) is in
test_task_numbers_writer.py.
"""
import os
import threading
import time
import uuid

import psycopg
import pytest

import scripts.backfill_task_numbers as backfill
from db.connection import get_connection
from repositories import action_items, companies, sites, topics

pytestmark = pytest.mark.integration

MIGRATION_0093 = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "migrations",
    "0093_action_task_numbers.sql")


def _site(db, tag="x"):
    co = companies.create_company(db, f"TN-Co-{tag}-{uuid.uuid4().hex[:8]}")
    return sites.create_site(db, co["id"], f"TN-Site-{uuid.uuid4().hex[:8]}")


def _topic(db, site, texts, key=None):
    key = key or f"extractions/TN/2026-11-01/sid{uuid.uuid4().hex[:8]}.json"
    t = topics.upsert_topic(db, site["id"], "2026-11-01", "T", source_s3_key=key,
                            action_items=[{"text": x} for x in texts])
    rows = db.execute("SELECT id, stable_id, text FROM action_items WHERE topic_id=%s",
                      (t["id"],)).fetchall()
    by_text = {r[2]: (r[0], r[1]) for r in rows}
    return t, [by_text[x] for x in texts]


def _ledger(db, site):
    return db.execute("SELECT stable_id, task_no FROM action_task_numbers WHERE site_id=%s "
                      "ORDER BY task_no", (site["id"],)).fetchall()


# --------------------------------------------------------------------------- migration
def test_migration_is_rerunnable_and_enforces_per_site_uniqueness(db):
    with open(MIGRATION_0093, encoding="utf-8") as fh:
        sql = fh.read()
    db.execute(sql)          # already applied by the fixture; a second run is a no-op
    site = _site(db)
    db.execute("INSERT INTO action_task_numbers (stable_id, site_id, task_no) "
               "VALUES (%s,%s,1)", (uuid.uuid4(), site["id"]))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute("INSERT INTO action_task_numbers (stable_id, site_id, task_no) "
                       "VALUES (%s,%s,1)", (uuid.uuid4(), site["id"]))
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute("INSERT INTO action_task_numbers (stable_id, site_id, task_no) "
                       "VALUES (%s,%s,0)", (uuid.uuid4(), site["id"]))


# --------------------------------------------------------------------------- assign_task_no
def test_assigned_once_and_sequential_per_site(db):
    s1, s2 = _site(db), _site(db)
    _t, [(_, a), (_, b)] = _topic(db, s1, ["Order timber", "Book crane"])
    _t, [(_, c)] = _topic(db, s2, ["Call council"])
    assert action_items.assign_task_no(db, a, s1["id"]) == 1
    assert action_items.assign_task_no(db, a, s1["id"]) == 1        # idempotent
    assert action_items.assign_task_no(db, b, s1["id"]) == 2
    assert action_items.assign_task_no(db, c, s2["id"]) == 1        # own counter per site
    assert _ledger(db, s1) == [(a, 1), (b, 2)]


def test_never_reused_after_the_item_is_deleted(db):
    site = _site(db)
    t, [(_, a), (_, b)] = _topic(db, site, ["Order timber", "Book crane"])
    action_items.assign_task_no(db, a, site["id"])
    action_items.assign_task_no(db, b, site["id"])
    db.execute("DELETE FROM action_items WHERE stable_id=%s", (b,))
    db.execute("DELETE FROM topics WHERE id=%s", (t["id"],))
    _t, [(_, c)] = _topic(db, site, ["Call council"])
    assert action_items.assign_task_no(db, c, site["id"]) == 3
    assert _ledger(db, site) == [(a, 1), (b, 2), (c, 3)]


def test_assign_for_topics_numbers_each_new_stable_id_in_order(db):
    site = _site(db)
    t1, items1 = _topic(db, site, ["Order timber", "Book crane"])
    t2, items2 = _topic(db, site, ["Call council"])
    # One stable_id shared by two rows (a carried identity) is numbered once.
    db.execute("UPDATE action_items SET stable_id=%s WHERE id=%s", (items1[0][1], items2[0][0]))
    n = action_items.assign_task_nos_for_topics(db, [t1["id"], t2["id"]], site["id"])
    assert n == 2
    assert _ledger(db, site) == [(items1[0][1], 1), (items1[1][1], 2)]
    # Idempotent: nothing left to number.
    assert action_items.assign_task_nos_for_topics(db, [t1["id"], t2["id"]], site["id"]) == 0
    assert action_items.assign_task_nos_for_topics(db, [], site["id"]) == 0


def test_task_nos_for_item_ids_and_get_live_by_task_no(db):
    site = _site(db)
    t, [(aid, a), (bid, b)] = _topic(db, site, ["Order timber", "Book crane"])
    action_items.assign_task_no(db, a, site["id"])
    assert action_items.task_nos_for_item_ids(db, [aid, str(bid)]) == {str(aid): 1}
    assert action_items.task_nos_for_item_ids(db, []) == {}

    row = action_items.get_live_by_task_no(db, site["id"], 1)
    assert row["id"] == aid and row["stable_id"] == a
    assert action_items.get_live_by_task_no(db, site["id"], 2) is None       # unnumbered
    assert action_items.get_live_by_task_no(db, _site(db)["id"], 1) is None  # other site
    assert action_items.get_live_by_task_no(db, "not-a-uuid", 1) is None
    assert action_items.get_live_by_task_no(db, site["id"], "x") is None
    assert action_items.get_live_by_task_no(db, site["id"], "1")["id"] == aid   # digit string
    for bad in (True, 1.0, 1.5, "1.0", "abc", "", " ", "-1", 0, -1, None, 2**31):
        assert action_items.get_live_by_task_no(db, site["id"], bad) is None, bad
    # Superseded: the number still resolves to nothing live.
    db.execute("UPDATE topics SET superseded_at=now() WHERE id=%s", (t["id"],))
    assert action_items.get_live_by_task_no(db, site["id"], 1) is None


def test_get_action_item_carries_task_no(db):
    site = _site(db)
    _t, [(aid, a), (bid, _b)] = _topic(db, site, ["Order timber", "Book crane"])
    action_items.assign_task_no(db, a, site["id"])
    assert action_items.get_action_item(db, aid)["task_no"] == 1
    assert action_items.get_action_item(db, bid)["task_no"] is None
    db.execute("DROP TABLE action_task_numbers")       # rolled back with the fixture
    row = action_items.get_action_item(db, aid)
    assert row["task_no"] is None and row["text"] == "Order timber"
    assert db.execute("SELECT 1").fetchone() == (1,)    # transaction still usable


# --------------------------------------------------------------------------- deploy window
def test_missing_table_is_tolerated_and_the_transaction_stays_usable(db):
    site = _site(db)
    t, [(aid, _a)] = _topic(db, site, ["Order timber"])
    db.execute("DROP TABLE action_task_numbers")       # rolled back with the fixture
    assert action_items.assign_task_nos_for_topics(db, [t["id"]], site["id"]) is None
    assert action_items.task_nos_for_item_ids(db, [aid]) == {}
    # Only a real SAVEPOINT leaves the caller's transaction usable after the 42P01.
    assert db.execute("SELECT count(*) FROM action_items WHERE id=%s", (aid,)).fetchone() == (1,)


# --------------------------------------------------------------------------- concurrency
def test_two_connections_on_one_site_never_collide(migrated_db_url):
    seed = get_connection(migrated_db_url, autocommit=True)
    site = None
    try:
        site = _site(seed, "conc")
        _t, [(_, a), (_, b)] = _topic(seed, site, ["Order timber", "Book crane"])
        c1 = get_connection(migrated_db_url)
        c2 = get_connection(migrated_db_url)
        try:
            assert action_items.assign_task_no(c1, a, site["id"]) == 1   # c1 holds the lock
            out = {}

            def second():
                out["n"] = action_items.assign_task_no(c2, b, site["id"])
                c2.commit()

            th = threading.Thread(target=second)
            th.start()
            time.sleep(0.5)
            assert th.is_alive(), "the second writer must wait on the site lock"
            c1.commit()
            th.join(10)
            assert not th.is_alive()
            assert out["n"] == 2
        finally:
            c1.close()
            c2.close()
        assert _ledger(seed, site) == [(a, 1), (b, 2)]
    finally:
        if site is not None:
            seed.execute("DELETE FROM action_task_numbers WHERE site_id=%s", (site["id"],))
            seed.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
            seed.execute("DELETE FROM companies WHERE id=%s", (site["company_id"],))
        seed.close()


# --------------------------------------------------------------------------- backfill
def test_backfill_numbers_live_items_in_created_order_and_is_idempotent(db):
    s1, s2 = _site(db), _site(db)
    t_old, [(old_id, old_stable)] = _topic(db, s1, ["Order timber"])
    t_new, [(new_id, _)] = _topic(db, s1, ["Order the timber"])
    _t, [(_, b)] = _topic(db, s1, ["Book crane"])
    _t, [(_, c)] = _topic(db, s2, ["Call council"])
    _t, [(_, d)] = _topic(db, s2, ["Order scaffold"])
    db.execute("UPDATE action_items SET created_at = now() - interval '3 days' "
               "WHERE stable_id=%s", (d,))
    t_gone, [(_, gone)] = _topic(db, s1, ["Superseded only"])
    # old -> new carry: the stable_id's FIRST appearance (the old row) sets its order.
    db.execute("UPDATE topics SET superseded_at=now() WHERE id IN (%s,%s)",
               (t_old["id"], t_gone["id"]))
    db.execute("UPDATE action_items SET stable_id=%s, carried_from=%s WHERE id=%s",
               (old_stable, old_id, new_id))
    db.execute("UPDATE action_items SET created_at = now() - interval '2 days' WHERE id=%s",
               (old_id,))
    db.execute("UPDATE action_items SET created_at = now() - interval '1 day' "
               "WHERE stable_id=%s", (b,))
    # b already has a number (e.g. written live after migrate): it is kept, not renumbered.
    db.execute("INSERT INTO action_task_numbers (stable_id, site_id, task_no) VALUES (%s,%s,1)",
               (b, s1["id"]))
    scope = [s1["id"], s2["id"]]

    dry = backfill.run_backfill(db, apply=False, site_ids=scope)
    assert dry == {"sites": 2, "to_number": 3, "numbered": 0}
    assert _ledger(db, s1) == [(b, 1)]                      # dry run wrote nothing

    done = backfill.run_backfill(db, apply=True, site_ids=scope)
    assert done == {"sites": 2, "to_number": 3, "numbered": 3}
    assert _ledger(db, s1) == [(b, 1), (old_stable, 2)]
    assert _ledger(db, s2) == [(d, 1), (c, 2)]           # created_at order, not insert order
    assert gone not in [r[0] for r in _ledger(db, s1)]      # superseded-only: not live

    again = backfill.run_backfill(db, apply=True, site_ids=scope)
    assert again == {"sites": 0, "to_number": 0, "numbered": 0}
    assert _ledger(db, s1) == [(b, 1), (old_stable, 2)]


def test_backfill_default_all_sites_run_numbers_and_is_idempotent(db):
    """The owner's exact command: no --site, so the `sites IS NULL` arm. Inside the fixture's
    rolled-back transaction, so numbering every live item in the shared test DB is undone."""
    s1, s2 = _site(db), _site(db)
    _t, [(_, a), (_, b)] = _topic(db, s1, ["Order timber", "Book crane"])
    _t, [(_, c)] = _topic(db, s2, ["Call council"])
    db.execute("UPDATE action_items SET created_at = now() - interval '1 day' "
               "WHERE stable_id=%s", (b,))

    dry = backfill.run_backfill(db, apply=False)
    assert dry["to_number"] >= 3 and dry["sites"] >= 2 and dry["numbered"] == 0
    assert _ledger(db, s1) == [] and _ledger(db, s2) == []

    done = backfill.run_backfill(db, apply=True)
    assert done["numbered"] == done["to_number"] == dry["to_number"]
    assert _ledger(db, s1) == [(b, 1), (a, 2)]
    assert _ledger(db, s2) == [(c, 1)]

    assert backfill.run_backfill(db, apply=False) == {"sites": 0, "to_number": 0, "numbered": 0}
    assert backfill.run_backfill(db, apply=True) == {"sites": 0, "to_number": 0, "numbered": 0}
    assert _ledger(db, s1) == [(b, 1), (a, 2)]


def test_org_api_task_runs_the_real_backfill(migrated_db_url, monkeypatch):
    """The prod path (Aurora is VPC-only): {"task": "backfill_task_numbers"} through org-api's
    lambda_handler on a real autocommit connection. A dry run writes nothing; apply numbers;
    a second apply numbers nothing. Scoped to this test's site, so the shared DB's other
    items are untouched."""
    import lambda_org_api as org
    seed = get_connection(migrated_db_url, autocommit=True)
    site = None
    try:
        site = _site(seed, "task")
        _t, [(_, a), (_, b)] = _topic(seed, site, ["Order timber", "Book crane"])
        seed.execute("UPDATE action_items SET created_at = now() - interval '1 day' "
                     "WHERE stable_id=%s", (b,))
        monkeypatch.setattr(org, "get_connection",
                            lambda dsn=None, autocommit=False:
                            get_connection(migrated_db_url, autocommit=autocommit))
        event = {"task": "backfill_task_numbers", "site_ids": [str(site["id"])]}

        dry = org.lambda_handler(dict(event), None)
        assert dry == {"applied": False, "sites": 1, "to_number": 2, "numbered": 0}
        assert _ledger(seed, site) == []

        done = org.lambda_handler(dict(event, apply=True), None)
        assert done == {"applied": True, "sites": 1, "to_number": 2, "numbered": 2}
        assert _ledger(seed, site) == [(b, 1), (a, 2)]       # committed (autocommit)

        again = org.lambda_handler(dict(event, apply=True), None)
        assert again == {"applied": True, "sites": 0, "to_number": 0, "numbered": 0}
    finally:
        if site is not None:
            seed.execute("DELETE FROM action_task_numbers WHERE site_id=%s", (site["id"],))
            seed.execute("DELETE FROM action_items WHERE site_id=%s", (site["id"],))
            seed.execute("DELETE FROM topics WHERE site_id=%s", (site["id"],))
            seed.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
            seed.execute("DELETE FROM companies WHERE id=%s", (site["company_id"],))
        seed.close()
