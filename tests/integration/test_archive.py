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


def test_archive_site_cascades_memberships(db):
    co = companies.create_company(db, "CascadeCo")
    s = sites.create_site(db, co["id"], "Casc Site")
    u = users.upsert_user(db, "sub-cs", "cs@x.nz", company_id=co["id"])
    memberships.ensure_membership(db, u["id"], s["id"], "worker")
    row = sites.archive_site(db, s["id"], co["id"])
    assert row is not None and row["archived_at"] is not None
    assert sites.list_company_sites(db, co["id"]) == []
    assert memberships.accessible_site_ids(db, u["id"], "worker") == []
    other = companies.create_company(db, "OtherCo")
    assert sites.archive_site(db, s["id"], other["id"]) is None   # cross-company
    assert sites.archive_site(db, s["id"], co["id"]) is None      # double-archive
    assert sites.unarchive_site(db, s["id"], co["id"])["archived_at"] is None
    assert [x["name"] for x in sites.list_company_sites(db, co["id"])] == ["Casc Site"]
    assert memberships.accessible_site_ids(db, u["id"], "worker") == []  # membership stays archived
    # re-adding revives the archived membership (ON CONFLICT resets archived_at)
    m = memberships.ensure_membership(db, u["id"], s["id"], "site_manager")
    assert m["role"] == "site_manager"
    assert memberships.accessible_site_ids(db, u["id"], "worker") == [s["id"]]


def test_archive_user_cascades_and_guards(db):
    co = companies.create_company(db, "UArchCo")
    s = sites.create_site(db, co["id"], "S")
    u = users.upsert_user(db, "sub-ua", "ua@x.nz", company_id=co["id"])
    memberships.ensure_membership(db, u["id"], s["id"], "worker")
    assert users.archive_user(db, "sub-ua", co["id"])["archived_at"] is not None
    assert users.list_company_users(db, co["id"]) == []
    assert memberships.accessible_site_ids(db, u["id"], "worker") == []
    other = companies.create_company(db, "Other2")
    assert users.archive_user(db, "sub-ua", other["id"]) is None
    assert users.unarchive_user(db, "sub-ua", co["id"])["archived_at"] is None


def test_set_site_icon_and_update_site(db):
    co = companies.create_company(db, "IconCo")
    s = sites.create_site(db, co["id"], "Icon Site", location="Chch")
    row = sites.set_site_icon(db, s["id"], "org-assets/site-icons/" + str(s["id"]) + "/x.png")
    assert row["icon_s3_key"].endswith("x.png")
    row = sites.update_site(db, s["id"], co["id"], name="Renamed")
    assert row["name"] == "Renamed" and row["location"] == "Chch"  # None-preserving
    other = companies.create_company(db, "IconOther")
    assert sites.update_site(db, s["id"], other["id"], name="X") is None  # company guard
    db.execute("UPDATE sites SET archived_at=now() WHERE id=%s", (s["id"],))
    assert sites.update_site(db, s["id"], co["id"], name="Y") is None     # archived -> None


def test_clear_avatar(db):
    co = companies.create_company(db, "AvCo")
    users.upsert_user(db, "sub-av", "av@x.nz", company_id=co["id"])
    users.update_profile(db, "sub-av", avatar_s3_key="org-assets/avatars/sub-av/a.png")
    row = users.clear_avatar(db, "sub-av")
    assert row["avatar_s3_key"] is None
    assert users.clear_avatar(db, "sub-ghost") is None
