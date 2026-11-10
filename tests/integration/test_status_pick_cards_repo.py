"""Integration: the pick-card store (status_pick_cards, cross-session status reconcile r0).

A pick card is one spoken status statement that could refer to 2-4 items; a person chooses.
These tests exercise the table and repositories.status_pick_cards.insert_pick_card against the
real migrated schema: idempotency on the anchor, the 2..4 candidate bound, and the cascade.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import uuid

import psycopg
import pytest

from db.connection import get_connection
from repositories import companies, sites
from repositories.status_pick_cards import insert_pick_card

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
