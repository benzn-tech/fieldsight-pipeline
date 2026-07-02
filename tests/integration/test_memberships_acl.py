import pytest
from repositories import companies, users, sites, memberships

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
