"""Integration coverage for the Phase 3 repository additions (real SQL,
rolled back per test — see tests/conftest.py `db` fixture)."""
import pytest

from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration


def _company(db, name="OrgCo"):
    return companies.create_company(db, name)


def test_get_company_by_name_roundtrip(db):
    co = _company(db, "Lookup Co")
    assert companies.get_company_by_name(db, "Lookup Co")["id"] == co["id"]
    assert companies.get_company_by_name(db, "Nope") is None


def test_set_global_role_explicit(db):
    co = _company(db)
    users.upsert_user(db, "sub-r", "r@x.com", company_id=co["id"], global_role="worker")
    updated = users.set_global_role(db, "sub-r", "pm")
    assert updated["global_role"] == "pm"
    assert users.set_global_role(db, "no-such-sub", "pm") is None


def test_update_profile_coalesce_and_lockdown(db):
    co = _company(db)
    users.upsert_user(db, "sub-p", "p@x.com", company_id=co["id"],
                      first_name="Ann", global_role="pm")
    # partial: only last_name — first_name/role/company untouched
    row = users.update_profile(db, "sub-p", last_name="Lee")
    assert row["first_name"] == "Ann" and row["last_name"] == "Lee"
    assert row["global_role"] == "pm" and row["company_id"] == co["id"]
    # avatar key set independently
    row = users.update_profile(db, "sub-p", avatar_s3_key="org-assets/avatars/sub-p")
    assert row["avatar_s3_key"] == "org-assets/avatars/sub-p"
    assert row["last_name"] == "Lee"
    assert users.update_profile(db, "ghost") is None


def test_list_company_users_scoped(db):
    co1, co2 = _company(db, "A"), _company(db, "B")
    users.upsert_user(db, "sub-a", "a@x.com", company_id=co1["id"])
    users.upsert_user(db, "sub-b", "b@x.com", company_id=co2["id"])
    subs = {u["cognito_sub"] for u in users.list_company_users(db, co1["id"])}
    assert subs == {"sub-a"}


def test_count_provisioned_users_ignores_companyless(db):
    base = users.count_provisioned_users(db)
    users.upsert_user(db, "sub-float", "f@x.com")  # no company → not provisioned
    assert users.count_provisioned_users(db) == base
    co = _company(db)
    users.upsert_user(db, "sub-prov", "p2@x.com", company_id=co["id"])
    assert users.count_provisioned_users(db) == base + 1


def test_ensure_membership_idempotent_role_update(db):
    co = _company(db)
    u = users.upsert_user(db, "sub-m", "m@x.com", company_id=co["id"])
    s = sites.create_site(db, co["id"], "Site M")
    first = memberships.ensure_membership(db, u["id"], s["id"], "worker")
    again = memberships.ensure_membership(db, u["id"], s["id"], "site_manager")
    assert first["id"] == again["id"], "same (user, site) row must be reused"
    assert again["role"] == "site_manager"
    rows = memberships.list_company_memberships(db, co["id"])
    assert len(rows) == 1 and rows[0]["cognito_sub"] == "sub-m"


def test_list_company_memberships_excludes_other_company(db):
    co1, co2 = _company(db, "A"), _company(db, "B")
    u1 = users.upsert_user(db, "sub-1c", "1@x.com", company_id=co1["id"])
    u2 = users.upsert_user(db, "sub-2c", "2@x.com", company_id=co2["id"])
    s1 = sites.create_site(db, co1["id"], "S1")
    s2 = sites.create_site(db, co2["id"], "S2")
    memberships.ensure_membership(db, u1["id"], s1["id"], "worker")
    memberships.ensure_membership(db, u2["id"], s2["id"], "worker")
    rows = memberships.list_company_memberships(db, co1["id"])
    assert [r["cognito_sub"] for r in rows] == ["sub-1c"]


def test_sites_lookup_helpers(db):
    co = _company(db)
    s = sites.create_site(db, co["id"], "North Wharf", location="Akl")
    assert sites.get_site_by_name(db, co["id"], "North Wharf")["id"] == s["id"]
    assert sites.get_site_by_name(db, co["id"], "Ghost") is None
    assert sites.list_sites_by_ids(db, []) == []
    got = sites.list_sites_by_ids(db, [s["id"]])
    assert [x["id"] for x in got] == [s["id"]]


def test_set_icon_key(db):
    co = _company(db)
    s = sites.create_site(db, co["id"], "Iconic")
    updated = sites.set_icon_key(db, s["id"], f"org-assets/site-icons/{s['id']}")
    assert updated["icon_s3_key"] == f"org-assets/site-icons/{s['id']}"
