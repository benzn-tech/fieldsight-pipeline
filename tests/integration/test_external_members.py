"""Project-owned tenancy P1 against a real Postgres: the external flag and the
roster queries. A connection double never parses SQL, so the column, the
dropped u.company_id = s.company_id equality and the upsert are only proven here.
"""
import pytest
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration


def _world(db):
    site_co = companies.create_company(db, "SiteCo-ext")
    home_co = companies.create_company(db, "HomeCo-ext")
    site = sites.create_site(db, site_co["id"], "Tower A")
    emp = users.upsert_user(db, "sub-emp", "emp@site.nz", company_id=site_co["id"],
                            first_name="Em", last_name="Ployee")
    ext = users.upsert_user(db, "sub-ext", "Ext@Home.nz", company_id=home_co["id"],
                            first_name="Eve", last_name="Xu")
    memberships.ensure_membership(db, emp["id"], site["id"], "worker")
    return site_co, home_co, site, emp, ext


def test_external_column_defaults_false(db):
    _, _, site, emp, _ = _world(db)
    row = memberships.get_membership(db, emp["id"], site["id"])
    assert row["external"] is False


def test_email_lookup_is_exact_case_insensitive_and_global(db):
    _, home_co, _, _, ext = _world(db)
    got = users.get_user_by_email_global(db, "ext@home.NZ")
    assert got["id"] == ext["id"] and got["company_name"] == "HomeCo-ext"
    assert users.get_user_by_email_global(db, "ext@home") is None
    db.execute("UPDATE users SET archived_at=now() WHERE id=%s", (ext["id"],))
    assert users.get_user_by_email_global(db, "ext@home.nz") is None


def test_external_member_appears_in_every_roster_query(db):
    site_co, _, site, emp, ext = _world(db)
    row = memberships.add_external_membership(db, ext["id"], site["id"], "pm")
    assert row["external"] is True and row["role"] == "pm"

    members = memberships.members_for_site(db, site_co["id"], site["id"])
    by_sub = {m["cognito_sub"]: m for m in members}
    assert set(by_sub) == {"sub-emp", "sub-ext"}
    assert by_sub["sub-ext"]["external"] is True
    assert by_sub["sub-ext"]["home_company_name"] == "HomeCo-ext"
    assert by_sub["sub-ext"]["site_role"] == "pm"
    assert by_sub["sub-emp"]["external"] is False
    assert by_sub["sub-emp"]["home_company_name"] == "SiteCo-ext"

    assert memberships.count_by_site(db, [site["id"]]) == {str(site["id"]): 2}

    co_rows = memberships.list_company_memberships(db, site_co["id"])
    assert {(r["cognito_sub"], r["external"]) for r in co_rows} == {("sub-emp", False), ("sub-ext", True)}

    all_rows = [r for r in memberships.list_all_memberships(db) if r["site_id"] == site["id"]]
    assert {r["cognito_sub"] for r in all_rows} == {"sub-emp", "sub-ext"}
    assert {r["home_company_name"] for r in all_rows} == {"SiteCo-ext", "HomeCo-ext"}


def test_the_home_company_listing_does_not_gain_the_other_companys_site(db):
    """list_company_memberships stays pinned to the SITE's company: the home
    company does not see its employee's membership on someone else's site."""
    _, home_co, site, _, ext = _world(db)
    memberships.add_external_membership(db, ext["id"], site["id"], "worker")
    assert memberships.list_company_memberships(db, home_co["id"]) == []


def test_archive_then_revive_external(db):
    site_co, _, site, _, ext = _world(db)
    memberships.add_external_membership(db, ext["id"], site["id"], "worker")
    assert memberships.archive_membership(db, ext["id"], site["id"]) is not None
    assert memberships.get_membership(db, ext["id"], site["id"])["archived_at"] is not None
    assert "sub-ext" not in {m["cognito_sub"] for m in memberships.members_for_site(db, site_co["id"], site["id"])}
    row = memberships.add_external_membership(db, ext["id"], site["id"], "site_manager")
    got = memberships.get_membership(db, ext["id"], site["id"])
    assert got["archived_at"] is None and got["role"] == "site_manager" and row["external"] is True


def test_external_membership_grants_reach_to_the_site(db):
    _, _, site, _, ext = _world(db)
    assert memberships.accessible_site_ids(db, ext["id"], "worker") == []
    memberships.add_external_membership(db, ext["id"], site["id"], "worker")
    assert memberships.accessible_site_ids(db, ext["id"], "worker") == [site["id"]]


def test_an_unflagged_cross_company_row_is_still_dropped(db):
    """Only a row FLAGGED external may cross companies; a mis-tenanted employee
    row (bad data, external=false) stays hidden from every roster query."""
    site_co, _, site, _, ext = _world(db)
    memberships.ensure_membership(db, ext["id"], site["id"], "worker")  # not flagged
    assert "sub-ext" not in {m["cognito_sub"] for m in memberships.members_for_site(db, site_co["id"], site["id"])}
    assert memberships.count_by_site(db, [site["id"]]) == {str(site["id"]): 1}
    assert "sub-ext" not in {r["cognito_sub"] for r in memberships.list_company_memberships(db, site_co["id"])}
    assert "sub-ext" not in {r["cognito_sub"] for r in memberships.list_all_memberships(db)}
