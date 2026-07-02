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
