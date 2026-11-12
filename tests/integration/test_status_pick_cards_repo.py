"""Integration: the pick-card store (status_pick_cards, cross-session status reconcile r0).

A pick card is one spoken status statement that could refer to 2-4 items; a person chooses.
These tests exercise the table and repositories.status_pick_cards.insert_pick_card against the
real migrated schema: idempotency on the anchor, the 2..4 candidate bound, and the cascade;
plus the pending read across sites and the compare-and-set resolve.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import uuid

import psycopg
import pytest

from db.connection import get_connection
from repositories import companies, sites
from repositories.status_pick_cards import (get_pick_card, insert_pick_card,
                                            pending_for_sites, resolve_pick_card)

pytestmark = pytest.mark.integration


def _ids(n):
    return [str(uuid.uuid4()) for _ in range(n)]


@pytest.fixture
def site_ctx(migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    conn = get_connection(migrated_db_url, autocommit=True)
    co = companies.create_company(conn, f"Pick-Co-{tag}")
    site = sites.create_site(conn, co["id"], f"Pick-Site-{tag}")
    try:
        yield conn, co, site, tag
    finally:
        conn.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
        conn.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
        conn.close()


def test_insert_and_idempotent(site_ctx):
    conn, co, site, tag = site_ctx
    cands = _ids(3)
    row = insert_pick_card(conn, co["id"], site["id"], "done", "that one is done",
                           f"seg-{tag}", 240.0, cands, "final:2026-11-10T10:00:00Z")
    assert row is not None
    assert row["state"] == "pending"
    assert row["candidates"] == cands
    assert row["suggested_status"] == "done"
    assert row["segment_key"] == f"seg-{tag}"
    assert row["offset_sec"] == 240.0
    assert row["chosen_stable_id"] is None and row["resolved_by"] is None

    again = insert_pick_card(conn, co["id"], site["id"], "done", "that one is done",
                             f"seg-{tag}", 240.0, _ids(2), "final:2026-11-10T10:30:00Z")
    assert again is None
    # A different status on the same anchor is a different card.
    other = insert_pick_card(conn, co["id"], site["id"], "blocked", "that one is stuck",
                             f"seg-{tag}", 240.0, cands, "final:2026-11-10T10:00:00Z")
    assert other is not None
    n = conn.execute("SELECT count(*) FROM status_pick_cards WHERE site_id=%s",
                     (site["id"],)).fetchone()[0]
    assert n == 2


@pytest.mark.parametrize("n", [1, 5])
def test_candidates_bounds(site_ctx, n):
    conn, co, site, tag = site_ctx
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_pick_card(conn, co["id"], site["id"], "done", "q", f"seg-{tag}-{n}", 1.0,
                         _ids(n), "final:x")


def test_bad_status_rejected(site_ctx):
    conn, co, site, tag = site_ctx
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_pick_card(conn, co["id"], site["id"], "finished", "q", f"seg-{tag}", 1.0,
                         _ids(2), "final:x")


def test_cascade_on_site_delete(migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    conn = get_connection(migrated_db_url, autocommit=True)
    co = companies.create_company(conn, f"PickDel-Co-{tag}")
    try:
        site = sites.create_site(conn, co["id"], f"PickDel-Site-{tag}")
        row = insert_pick_card(conn, co["id"], site["id"], "closed", "q", f"seg-{tag}", 5.0,
                               _ids(4), "final:x")
        assert row is not None
        conn.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
        n = conn.execute("SELECT count(*) FROM status_pick_cards WHERE id=%s",
                         (row["id"],)).fetchone()[0]
        assert n == 0
    finally:
        conn.execute("DELETE FROM sites WHERE company_id=%s", (co["id"],))
        conn.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
        conn.close()


def _user(conn, company_id, tag):
    return conn.execute(
        "INSERT INTO users (company_id, email, global_role, folder_name) "
        "VALUES (%s, %s, 'worker', %s) RETURNING id",
        (company_id, f"{tag}@x.com", tag)).fetchone()[0]


def test_pending_for_sites_filters_site_and_state(site_ctx):
    conn, co, site, tag = site_ctx
    site2 = sites.create_site(conn, co["id"], f"Pick-Site2-{tag}")
    other = sites.create_site(conn, co["id"], f"Pick-Other-{tag}")
    uid = _user(conn, co["id"], f"pick-{tag}")
    try:
        a = insert_pick_card(conn, co["id"], site["id"], "done", "a", f"seg-{tag}", 1.0,
                             _ids(2), "final:x")
        b = insert_pick_card(conn, co["id"], site2["id"], "blocked", "b", f"seg-{tag}", 2.0,
                             _ids(3), "final:x")
        resolved = insert_pick_card(conn, co["id"], site["id"], "closed", "c", f"seg-{tag}",
                                    3.0, _ids(2), "final:x")
        insert_pick_card(conn, co["id"], other["id"], "done", "d", f"seg-{tag}", 4.0,
                         _ids(2), "final:x")
        # Make the order observable: b is newer than a.
        conn.execute("UPDATE status_pick_cards SET created_at = now() - interval '1 hour' "
                     "WHERE id=%s", (a["id"],))
        assert resolve_pick_card(conn, resolved["id"], "none", None, uid) is not None

        got = pending_for_sites(conn, [str(site["id"]), str(site2["id"])])
        assert [r["id"] for r in got] == [b["id"], a["id"]]
        assert all(r["state"] == "pending" for r in got)
        assert pending_for_sites(conn, []) == []
    finally:
        conn.execute("DELETE FROM sites WHERE id = ANY(%s)", ([site2["id"], other["id"]],))
        conn.execute("DELETE FROM status_pick_cards WHERE site_id=%s", (site["id"],))
        conn.execute("DELETE FROM users WHERE id=%s", (uid,))


def test_resolve_is_cas(site_ctx):
    conn, co, site, tag = site_ctx
    first = _user(conn, co["id"], f"pick1-{tag}")
    second = _user(conn, co["id"], f"pick2-{tag}")
    cands = _ids(2)
    try:
        card = insert_pick_card(conn, co["id"], site["id"], "done", "q", f"seg-{tag}", 9.0,
                                cands, "final:x")
        won = resolve_pick_card(conn, card["id"], "chosen", cands[0], first)
        assert won is not None
        assert won["state"] == "chosen"
        assert str(won["chosen_stable_id"]) == cands[0]
        assert won["resolved_by"] == first and won["resolved_at"] is not None

        assert get_pick_card(conn, card["id"])["state"] == "chosen"
        assert get_pick_card(conn, str(uuid.uuid4())) is None

        lost = resolve_pick_card(conn, card["id"], "none", None, second)
        assert lost is None
        row = conn.execute("SELECT state, chosen_stable_id, resolved_by FROM status_pick_cards "
                           "WHERE id=%s", (card["id"],)).fetchone()
        assert row[0] == "chosen" and str(row[1]) == cands[0] and row[2] == first
    finally:
        conn.execute("DELETE FROM status_pick_cards WHERE site_id=%s", (site["id"],))
        conn.execute("DELETE FROM users WHERE id = ANY(%s)", ([first, second],))
