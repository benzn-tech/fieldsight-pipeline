"""Integration tests for the graded visible_scope primitive (Phase 3 Task 1,
additive/unwired) -- real DB semantics: caller_site_roles / worker_user_ids_
for_sites archive-exclusion, company-pin isolation, and the sole platform_admin
cross-company branch (D6). Mirrors tests/integration/test_memberships_acl.py's
style (repo helper functions, not raw SQL) rather than the plan's inline-SQL
sketch, which mixed %-formatting into a parameterized query for company_id."""
import pytest

from repositories import companies, memberships, scope, sites, users

pytestmark = pytest.mark.integration


def test_caller_site_roles_and_worker_ids_exclude_archived(db):
    co = companies.create_company(db, "Scope Co A")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")

    mgr = users.upsert_user(db, "sub-scope-mgr", "mgr@sc.nz", company_id=co["id"])
    w1 = users.upsert_user(db, "sub-scope-w1", "w1@sc.nz", company_id=co["id"])
    w2 = users.upsert_user(db, "sub-scope-w2", "w2@sc.nz", company_id=co["id"])

    memberships.add_membership(db, mgr["id"], s1["id"], "site_manager")
    memberships.add_membership(db, w1["id"], s1["id"], "worker")
    memberships.add_membership(db, w2["id"], s2["id"], "worker")          # other site
    archived = memberships.add_membership(db, w2["id"], s1["id"], "worker")
    db.execute("UPDATE memberships SET archived_at = now() WHERE id=%s", (archived["id"],))

    assert memberships.caller_site_roles(db, mgr["id"]) == {str(s1["id"]): "site_manager"}
    assert memberships.worker_user_ids_for_sites(db, [str(s1["id"])]) == {str(w1["id"])}  # w2's s1 membership archived
    assert memberships.worker_user_ids_for_sites(db, []) == set()                          # no round-trip on empty input


def test_visible_scope_worker_cannot_see_out_of_scope_site(db):
    co = companies.create_company(db, "Scope Co B")
    mine = sites.create_site(db, co["id"], "Mine")
    other = sites.create_site(db, co["id"], "Other")

    u = users.upsert_user(db, "sub-scope-w", "w@sb.nz", company_id=co["id"])
    users.set_folder_name(db, "sub-scope-w", "W_Folder")
    memberships.add_membership(db, u["id"], mine["id"], "worker")

    caller = {"id": u["id"], "company_id": co["id"], "global_role": "worker", "folder_name": "W_Folder"}
    sc = scope.visible_scope(db, caller)

    assert str(mine["id"]) in sc["site_ids"]
    assert str(other["id"]) not in sc["site_ids"]
    assert sc["author_ids"] == {str(u["id"])}
    assert sc["user_scope"] == "SELF"
    assert sc["cross_company"] is False


def test_visible_scope_site_manager_sees_self_plus_workers_via_real_db(db):
    co = companies.create_company(db, "Scope Co D")
    s1 = sites.create_site(db, co["id"], "S1")

    mgr = users.upsert_user(db, "sub-scope-mgr2", "mgr2@sd.nz", company_id=co["id"])
    other_mgr = users.upsert_user(db, "sub-scope-mgr3", "mgr3@sd.nz", company_id=co["id"])
    w1 = users.upsert_user(db, "sub-scope-w3", "w3@sd.nz", company_id=co["id"])

    memberships.add_membership(db, mgr["id"], s1["id"], "site_manager")
    memberships.add_membership(db, other_mgr["id"], s1["id"], "site_manager")  # BUG-25 class: another manager
    memberships.add_membership(db, w1["id"], s1["id"], "worker")

    caller = {"id": mgr["id"], "company_id": co["id"], "global_role": "site_manager", "folder_name": None}
    sc = scope.visible_scope(db, caller)

    assert sc["user_scope"] == "SELF+WORKERS"
    assert sc["author_ids"] == {str(mgr["id"]), str(w1["id"])}
    assert str(other_mgr["id"]) not in sc["author_ids"]   # never another site_manager


def test_visible_scope_platform_admin_spans_companies_but_admin_does_not(db):
    ca = companies.create_company(db, "Scope Co C-A")
    cb = companies.create_company(db, "Scope Co C-B")
    sa = sites.create_site(db, ca["id"], "SA")
    sb = sites.create_site(db, cb["id"], "SB")

    admin_a = users.upsert_user(db, "sub-scope-admin-a", "admin@ca.nz", company_id=ca["id"], global_role="admin")
    plat = users.upsert_user(db, "sub-scope-plat", "plat@ca.nz", company_id=ca["id"], global_role="platform_admin")

    admin_caller = {"id": admin_a["id"], "company_id": ca["id"], "global_role": "admin", "folder_name": None}
    plat_caller = {"id": plat["id"], "company_id": ca["id"], "global_role": "platform_admin", "folder_name": None}

    admin_ids = scope.visible_scope(db, admin_caller)["site_ids"]
    plat_scope = scope.visible_scope(db, plat_caller)
    plat_ids = plat_scope["site_ids"]

    assert str(sa["id"]) in admin_ids and str(sb["id"]) not in admin_ids     # company A admin: A only
    assert {str(sa["id"]), str(sb["id"])} <= plat_ids                        # platform_admin: both
    assert plat_scope["cross_company"] is True
