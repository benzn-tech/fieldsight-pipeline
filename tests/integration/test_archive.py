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


def _mem_archived_at(db, user_id, site_id):
    return db.execute("SELECT archived_at FROM memberships WHERE user_id=%s AND site_id=%s",
                      (user_id, site_id)).fetchone()[0]


def test_unarchive_user_restores_only_the_cascaded_memberships(db):
    """Spec 2026-11-01 company offboarding §8 (Task 6). archive_user stamps the user and
    every live membership with the same now(); unarchive_user brings back exactly those --
    a membership closed earlier on its own (DELETE /members/{sub}/memberships/{site})
    stays closed. An external membership on another company's site comes back too, even
    while that company is suspended: the live-company choke point hides it, the row is
    the person's."""
    co = companies.create_company(db, "UnarchCo")
    other = companies.create_company(db, "UnarchOther")
    s_kept = sites.create_site(db, co["id"], "Kept")
    s_also = sites.create_site(db, co["id"], "Also")
    s_off = sites.create_site(db, co["id"], "Taken off earlier")
    s_ext = sites.create_site(db, other["id"], "External")
    u = users.upsert_user(db, "sub-unarch", "unarch@x.nz", company_id=co["id"])
    for s in (s_kept, s_also, s_off):
        memberships.ensure_membership(db, u["id"], s["id"], "worker")
    memberships.add_external_membership(db, u["id"], s_ext["id"], "worker")
    # Taken off one project an hour before the person was archived. (The `db` fixture is
    # one transaction, so now() is the same everywhere in this test; stamp it explicitly.)
    memberships.archive_membership(db, u["id"], s_off["id"])
    db.execute("UPDATE memberships SET archived_at = now() - interval '1 hour' "
               "WHERE user_id=%s AND site_id=%s", (u["id"], s_off["id"]))
    earlier = _mem_archived_at(db, u["id"], s_off["id"])

    archived = users.archive_user(db, "sub-unarch", co["id"])
    for s in (s_kept, s_also, s_ext):
        assert _mem_archived_at(db, u["id"], s["id"]) == archived["archived_at"]
    db.execute("UPDATE companies SET status='suspended' WHERE id=%s", (other["id"],))

    # Wrong company / not archived: None, nothing touched.
    assert users.unarchive_user(db, "sub-unarch", other["id"]) is None
    assert _mem_archived_at(db, u["id"], s_kept["id"]) is not None

    row = users.unarchive_user(db, "sub-unarch", co["id"])
    assert row is not None and row["archived_at"] is None
    for s in (s_kept, s_also, s_ext):
        assert _mem_archived_at(db, u["id"], s["id"]) is None, s["name"]
    assert _mem_archived_at(db, u["id"], s_off["id"]) == earlier
    assert set(memberships.accessible_site_ids(db, u["id"], "worker")) >= {
        s_kept["id"], s_also["id"]}
    assert s_off["id"] not in memberships.accessible_site_ids(db, u["id"], "worker")
    assert users.unarchive_user(db, "sub-unarch", co["id"]) is None   # already live


def test_unarchive_user_leaves_other_peoples_memberships_alone(db):
    """Equal timestamps are matched per USER: a colleague archived in the same instant
    (same transaction here) is not brought back with them."""
    co = companies.create_company(db, "UnarchPair")
    s = sites.create_site(db, co["id"], "S")
    a = users.upsert_user(db, "sub-unarch-a", "ua-a@x.nz", company_id=co["id"])
    b = users.upsert_user(db, "sub-unarch-b", "ua-b@x.nz", company_id=co["id"])
    for u in (a, b):
        memberships.ensure_membership(db, u["id"], s["id"], "worker")
        users.archive_user(db, u["cognito_sub"], co["id"])
    users.unarchive_user(db, "sub-unarch-a", co["id"])
    assert _mem_archived_at(db, a["id"], s["id"]) is None
    assert _mem_archived_at(db, b["id"], s["id"]) is not None
    assert users.get_user_by_sub(db, "sub-unarch-b")["archived_at"] is not None


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
