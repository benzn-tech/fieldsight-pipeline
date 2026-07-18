from datetime import datetime, timedelta, timezone

import pytest

from repositories import companies, users, sites, memberships, ws_connections

pytestmark = pytest.mark.integration


def test_site_voice_tables_exist(db):
    # Both tables are created by 0016; a bare INSERT/SELECT proves the schema.
    db.execute(
        "INSERT INTO ws_connections (connection_id, user_id, company_id) "
        "VALUES ('c-smoke', gen_random_uuid(), gen_random_uuid())")
    assert db.execute(
        "SELECT count(*) FROM ws_connections WHERE connection_id='c-smoke'"
    ).fetchone()[0] == 1
    row = db.execute(
        "INSERT INTO voice_messages (company_id, site_id, sender_user_id, s3_key) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'voice/x.wav') "
        "RETURNING id, duration_s, created_at").fetchone()
    assert row[0] is not None and row[1] is None and row[2] is not None


def _seed(db):
    co = companies.create_company(db, "Acme", industry="construction")
    a = users.upsert_user(db, "sub-a", "a@acme.com", company_id=co["id"])
    b = users.upsert_user(db, "sub-b", "b@acme.com", company_id=co["id"])
    s = sites.create_site(db, co["id"], "North Wharf")
    memberships.add_membership(db, a["id"], s["id"], "worker")
    memberships.add_membership(db, b["id"], s["id"], "worker")
    return co, a, b, s


def test_upsert_is_idempotent_on_connection_id(db):
    co, a, b, s = _seed(db)
    ws_connections.upsert_connection(db, "conn-1", a["id"], co["id"])
    ws_connections.upsert_connection(db, "conn-1", a["id"], co["id"])
    n = db.execute("SELECT count(*) FROM ws_connections WHERE connection_id='conn-1'").fetchone()[0]
    assert n == 1


def test_recipients_for_site_excludes_sender_and_offline(db):
    co, a, b, s = _seed(db)
    ws_connections.upsert_connection(db, "conn-a", a["id"], co["id"])
    ws_connections.upsert_connection(db, "conn-b", b["id"], co["id"])
    got = ws_connections.recipients_for_site(db, co["id"], s["id"], a["id"])
    assert got == ["conn-b"]                      # sender excluded; b online
    # A non-member (no membership row) is never a recipient even if connected.
    c = users.upsert_user(db, "sub-c", "c@acme.com", company_id=co["id"])
    ws_connections.upsert_connection(db, "conn-c", c["id"], co["id"])
    assert set(ws_connections.recipients_for_site(db, co["id"], s["id"], a["id"])) == {"conn-b"}


def test_recipients_cross_company_isolated(db):
    co, a, b, s = _seed(db)
    other = companies.create_company(db, "Other")
    ws_connections.upsert_connection(db, "conn-b", b["id"], co["id"])
    # Same site id, but querying under a different company returns nothing.
    assert ws_connections.recipients_for_site(db, other["id"], s["id"], a["id"]) == []


def test_delete_connection_and_bulk_and_stale(db):
    co, a, b, s = _seed(db)
    ws_connections.upsert_connection(db, "conn-a", a["id"], co["id"])
    ws_connections.upsert_connection(db, "conn-b", b["id"], co["id"])
    ws_connections.delete_connection(db, "conn-a")
    assert ws_connections.recipients_for_site(db, co["id"], s["id"], a["id"]) == ["conn-b"]
    assert ws_connections.delete_connections(db, ["conn-b", "missing"]) == 1
    assert ws_connections.delete_connections(db, []) == 0
    ws_connections.upsert_connection(db, "conn-old", a["id"], co["id"])
    db.execute("UPDATE ws_connections SET connected_at = now() - interval '48 hours' WHERE connection_id='conn-old'")
    assert ws_connections.delete_stale(db, datetime.now(timezone.utc) - timedelta(hours=24)) == 1
