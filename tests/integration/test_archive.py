import pytest
from repositories import companies, users, sites, memberships

pytestmark = pytest.mark.integration


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_archived_at_columns_exist(db):
    for t in ("sites", "users", "memberships"):
        assert "archived_at" in _columns(db, t)


def test_lists_hide_archived_rows_and_include_flag(db):
    co = companies.create_company(db, "ArchCo")
    s_live = sites.create_site(db, co["id"], "Live Site")
    s_arch = sites.create_site(db, co["id"], "Arch Site")
    u_live = users.upsert_user(db, "sub-al", "al@x.nz", company_id=co["id"])
    u_arch = users.upsert_user(db, "sub-aa", "aa@x.nz", company_id=co["id"])
    memberships.ensure_membership(db, u_live["id"], s_live["id"], "worker")
    db.execute("UPDATE sites SET archived_at=now() WHERE id=%s", (s_arch["id"],))
    db.execute("UPDATE users SET archived_at=now() WHERE id=%s", (u_arch["id"],))

    assert [r["name"] for r in sites.list_company_sites(db, co["id"])] == ["Live Site"]
    assert {r["name"] for r in sites.list_company_sites(db, co["id"], include_archived=True)} == {"Live Site", "Arch Site"}
    assert [s["name"] for s in sites.list_sites_by_ids(db, [s_live["id"], s_arch["id"]])] == ["Live Site"]
    assert [u["cognito_sub"] for u in users.list_company_users(db, co["id"])] == ["sub-al"]
    assert {u["cognito_sub"] for u in users.list_company_users(db, co["id"], include_archived=True)} == {"sub-al", "sub-aa"}
    # get_* point lookups still see archived (seed idempotency / self-read)
    assert sites.get_company_site_by_name(db, co["id"], "Arch Site") is not None
    assert users.get_user_by_sub(db, "sub-aa") is not None


def test_accessible_site_ids_excludes_archived(db):
    co = companies.create_company(db, "AccArch")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")
    w = users.upsert_user(db, "sub-w", "w@x.nz", company_id=co["id"], global_role="worker")
    memberships.ensure_membership(db, w["id"], s1["id"], "worker")
    memberships.ensure_membership(db, w["id"], s2["id"], "worker")
    db.execute("UPDATE memberships SET archived_at=now() WHERE site_id=%s", (s2["id"],))
    assert memberships.accessible_site_ids(db, w["id"], "worker") == [s1["id"]]
    adm = users.upsert_user(db, "sub-adm", "adm@x.nz", company_id=co["id"], global_role="admin")
    db.execute("UPDATE sites SET archived_at=now() WHERE id=%s", (s2["id"],))
    assert set(memberships.accessible_site_ids(db, adm["id"], "admin")) == {s1["id"]}
