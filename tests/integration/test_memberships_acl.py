import pytest
from repositories import companies, users, sites, memberships
from repositories.memberships import ensure_membership, list_company_memberships
from repositories.sites import list_sites_by_ids as sites_list_by_ids, get_company_site_by_name
from repositories.companies import get_company_by_name

pytestmark = pytest.mark.integration


def test_accessible_site_ids_by_role(db):
    co = companies.create_company(db, "Acme")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")

    admin = users.upsert_user(db, "sub-admin", "admin@a.com", company_id=co["id"], global_role="admin")
    worker = users.upsert_user(db, "sub-w", "w@a.com", company_id=co["id"], global_role="worker")
    memberships.add_membership(db, worker["id"], s1["id"], "worker")

    admin_sites = set(memberships.accessible_site_ids(db, admin["id"], "admin"))
    worker_sites = set(memberships.accessible_site_ids(db, worker["id"], "worker"))

    assert admin_sites == {s1["id"], s2["id"]}       # admin sees all
    assert worker_sites == {s1["id"]}                # worker sees only membership


def test_admin_scope_is_company_bounded(db):
    co_a = companies.create_company(db, "A")
    co_b = companies.create_company(db, "B")
    sa = sites.create_site(db, co_a["id"], "A1")
    sites.create_site(db, co_b["id"], "B1")
    admin_a = users.upsert_user(db, "sub-admin-a", "a@a.com", company_id=co_a["id"], global_role="admin")
    got = set(memberships.accessible_site_ids(db, admin_a["id"], "admin"))
    assert got == {sa["id"]}, "admin must not see other companies' sites"


@pytest.mark.integration
def test_ensure_membership_idempotent_role_update(db):
    c = companies.create_company(db, "EnsureCo")
    u = users.upsert_user(db, "sub-en-1", "e@x.nz", company_id=c["id"])
    s = sites.create_site(db, c["id"], "Ensure Site")
    m1 = ensure_membership(db, u["id"], s["id"], "worker")
    m2 = ensure_membership(db, u["id"], s["id"], "site_manager")  # re-run: no raise
    assert m1["id"] == m2["id"]
    assert m2["role"] == "site_manager"


@pytest.mark.integration
def test_list_company_memberships_scoped(db):
    c1 = companies.create_company(db, "MemCo A")
    c2 = companies.create_company(db, "MemCo B")
    u1 = users.upsert_user(db, "sub-me-1", "m1@x.nz", company_id=c1["id"])
    u2 = users.upsert_user(db, "sub-me-2", "m2@x.nz", company_id=c2["id"])
    s1 = sites.create_site(db, c1["id"], "Mem Site A")
    s2 = sites.create_site(db, c2["id"], "Mem Site B")
    ensure_membership(db, u1["id"], s1["id"], "worker")
    ensure_membership(db, u2["id"], s2["id"], "worker")
    rows = list_company_memberships(db, c1["id"])
    assert [r["cognito_sub"] for r in rows] == ["sub-me-1"]
    assert rows[0]["site_id"] == s1["id"]


@pytest.mark.integration
def test_sites_by_ids_and_by_name(db):
    c = companies.create_company(db, "SiteLookupCo")
    s1 = sites.create_site(db, c["id"], "Lookup One")
    sites.create_site(db, c["id"], "Lookup Two")
    assert sites_list_by_ids(db, []) == []
    got = sites_list_by_ids(db, [s1["id"]])
    assert [g["name"] for g in got] == ["Lookup One"]
    assert get_company_site_by_name(db, c["id"], "Lookup Two")["name"] == "Lookup Two"
    assert get_company_site_by_name(db, c["id"], "Nope") is None


@pytest.mark.integration
def test_get_company_by_name(db):
    companies.create_company(db, "FindMe Ltd")
    assert get_company_by_name(db, "FindMe Ltd")["name"] == "FindMe Ltd"
    assert get_company_by_name(db, "Ghost Co") is None


@pytest.mark.integration
def test_list_company_memberships_excludes_cross_company_user(db):
    """Defense-in-depth: a user from company B wrongly enrolled in a
    company-A site must NOT appear in company A's membership listing."""
    ca = companies.create_company(db, "XTen A")
    cb = companies.create_company(db, "XTen B")
    ua = users.upsert_user(db, "sub-xt-a", "a@xt.nz", company_id=ca["id"])
    ub = users.upsert_user(db, "sub-xt-b", "b@xt.nz", company_id=cb["id"])
    sa = sites.create_site(db, ca["id"], "XTen Site A")
    ensure_membership(db, ua["id"], sa["id"], "worker")
    ensure_membership(db, ub["id"], sa["id"], "worker")  # bad data simulation
    rows = list_company_memberships(db, ca["id"])
    assert [r["cognito_sub"] for r in rows] == ["sub-xt-a"]


@pytest.mark.integration
def test_members_for_site_returns_company_members_excludes_cross_company_and_archived(db):
    c = companies.create_company(db, "MFS Co A")
    s = sites.create_site(db, c["id"], "MFS Site")
    u1 = users.upsert_user(db, "sub-mfs-a", "ada@mfs.nz", company_id=c["id"], first_name="Ada", last_name="X")
    u2 = users.upsert_user(db, "sub-mfs-b", "bea@mfs.nz", company_id=c["id"], first_name="Bea", last_name="X")
    ensure_membership(db, u1["id"], s["id"], "worker")
    ensure_membership(db, u2["id"], s["id"], "site_manager")

    # cross-company site+member must not appear
    cb = companies.create_company(db, "MFS Co B")
    sb = sites.create_site(db, cb["id"], "MFS Site B")
    ub = users.upsert_user(db, "sub-mfs-c", "cy@mfs.nz", company_id=cb["id"], first_name="Cy", last_name="X")
    ensure_membership(db, ub["id"], sb["id"], "worker")

    # archived membership must not appear
    db.execute("UPDATE memberships SET archived_at = now() WHERE user_id=%s AND site_id=%s", (u2["id"], s["id"]))

    rows = memberships.members_for_site(db, c["id"], str(s["id"]))
    names = [r["first_name"] for r in rows]
    assert names == ["Ada"]                              # Bea archived; Cy cross-company; both excluded
    assert rows[0]["site_role"] == "worker"

    # cross-company caller company must never see this site's members
    assert memberships.members_for_site(db, cb["id"], str(s["id"])) == []
