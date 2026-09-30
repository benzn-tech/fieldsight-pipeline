import pytest
from psycopg.errors import UniqueViolation

from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration


def test_move_changes_company_and_leaves_memberships_alone(db):
    old = companies.create_company(db, "MoveOld")
    new = companies.create_company(db, "MoveNew")
    s = sites.create_site(db, old["id"], "Moving Site", slug="moving")
    mgr = users.upsert_user(db, "sub-mv-mgr", "mgr@x.nz", company_id=old["id"])
    adm_old = users.upsert_user(db, "sub-mv-ao", "ao@x.nz", company_id=old["id"])
    adm_new = users.upsert_user(db, "sub-mv-an", "an@x.nz", company_id=new["id"])
    memberships.ensure_membership(db, mgr["id"], s["id"], "site_manager")
    q = ("SELECT id, user_id, site_id, role, archived_at FROM memberships "
         "WHERE site_id=%s ORDER BY id")
    before = db.execute(q, (s["id"],)).fetchall()

    assert memberships.accessible_site_ids(db, adm_old["id"], "admin") == [s["id"]]
    assert memberships.accessible_site_ids(db, adm_new["id"], "admin") == []

    row = sites.move_site_company(db, s["id"], old["id"], new["id"])
    assert row["company_id"] == new["id"] and row["slug"] == "moving"

    assert db.execute(q, (s["id"],)).fetchall() == before
    # membership-based access survives; ALL-scope access follows the company
    assert memberships.accessible_site_ids(db, mgr["id"], "site_manager") == [s["id"]]
    assert memberships.accessible_site_ids(db, adm_old["id"], "admin") == []
    assert memberships.accessible_site_ids(db, adm_new["id"], "admin") == [s["id"]]


def test_move_is_guarded_by_source_company_and_archive(db):
    a = companies.create_company(db, "GuardA")
    b = companies.create_company(db, "GuardB")
    c = companies.create_company(db, "GuardC")
    s = sites.create_site(db, a["id"], "Guarded", slug="g")
    assert sites.move_site_company(db, s["id"], b["id"], c["id"]) is None   # wrong source
    db.execute("UPDATE sites SET archived_at=now() WHERE id=%s", (s["id"],))
    assert sites.move_site_company(db, s["id"], a["id"], c["id"]) is None   # archived


def test_slug_is_unique_per_company_not_globally(db):
    a = companies.create_company(db, "SlugA")
    b = companies.create_company(db, "SlugB")
    sites.create_site(db, a["id"], "One", slug="same")
    s_b = sites.create_site(db, b["id"], "Two", slug="same")   # other company: fine
    s_a2 = sites.create_site(db, a["id"], "Three", slug="other")
    # Moving INTO a company that already holds the slug violates the index, which
    # is why patch_org_site checks first and answers 409 instead of a 500.
    with pytest.raises(UniqueViolation):
        with db.transaction():
            sites.move_site_company(db, s_b["id"], b["id"], a["id"])
    assert sites.get_site(db, s_b["id"])["company_id"] == b["id"]
    assert sites.move_site_company(db, s_a2["id"], a["id"], b["id"]) is not None
