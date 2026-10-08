"""org-api publishes the people-and-sites directory (config/directory.json).

The pipeline lambdas that cannot reach Aurora read it instead of the August
hand-edited user_mapping.json. These tests run the real SQL: a fake connection
records SQL and never executes it, which is how a wrong join would pass.
"""
import json

import pytest

import directory
import lambda_org_api as api
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration


@pytest.fixture
def lake(monkeypatch):
    state = {"doc": None, "puts": []}

    class S3:
        def put_object(self, **kw):
            state["puts"].append(kw)
            state["doc"] = json.loads(kw["Body"].decode("utf-8"))

    monkeypatch.setattr(api, "s3", lambda: S3())
    monkeypatch.setattr(api, "_get_lake_json", lambda key: state["doc"])
    return state


def _world(db):
    co = companies.create_company(db, "DirCo")
    a = sites.create_site(db, co["id"], "Alpha School", location="Chch", client="MoE",
                          slug="alpha-school")
    b = sites.create_site(db, co["id"], "Beta Hall", slug="beta-hall")
    adm = users.upsert_user(db, "dir-adm", "adm@x.nz", company_id=co["id"],
                            first_name="Ada", last_name="Min", global_role="admin")
    users.set_folder_name(db, "dir-adm", "Ada_Min")
    one = users.upsert_user(db, "dir-one", "one@x.nz", company_id=co["id"],
                            first_name="Deandre", last_name="Alberts", global_role="site_manager")
    users.set_folder_name(db, "dir-one", "Deandre__Alberts")
    two = users.upsert_user(db, "dir-two", "two@x.nz", company_id=co["id"],
                            first_name="Two", last_name="Sites", global_role="pm")
    users.set_folder_name(db, "dir-two", "Two_Sites")
    nofolder = users.upsert_user(db, "dir-nf", "nf@x.nz", company_id=co["id"],
                                 first_name="No", last_name="Folder")
    memberships.ensure_membership(db, one["id"], a["id"], "site_manager")
    memberships.ensure_membership(db, two["id"], a["id"], "pm")
    memberships.ensure_membership(db, two["id"], b["id"], "pm")
    memberships.ensure_membership(db, nofolder["id"], a["id"], "worker")
    return co, a, b, adm, one, two


def test_THE_publisher_emits_people_and_sites_from_the_database(db, lake):
    co, a, b, adm, one, two = _world(db)
    out = api.republish_directory(db)
    assert out["changed"] is True
    doc = lake["doc"]
    assert lake["puts"][0]["Key"] == directory.KEY
    assert doc["version"] == 1 and doc["published_at"]

    p = doc["people"]["Deandre__Alberts"]
    assert p["name"] == "Deandre Alberts" and p["role"] == "site_manager"
    assert p["company_id"] == str(co["id"])
    assert p["primary_site"] == "alpha-school" and p["sites"] == ["alpha-school"]

    # Two open sites: naming one as "primary" would be a guess.
    assert doc["people"]["Two_Sites"]["primary_site"] is None
    assert doc["people"]["Two_Sites"]["sites"] == ["alpha-school", "beta-hall"]
    # No site at all.
    assert doc["people"]["Ada_Min"]["primary_site"] is None
    assert doc["people"]["Ada_Min"]["sites"] == []
    # No folder, nothing the pipeline could key them by.
    assert not any(v["name"] == "No Folder" for v in doc["people"].values())

    s = doc["sites"]["alpha-school"]
    assert (s["name"], s["location"], s["client"]) == ("Alpha School", "Chch", "MoE")
    assert s["company_id"] == str(co["id"]) and s["site_id"] == str(a["id"])


def test_archived_users_sites_and_memberships_are_left_out(db, lake):
    co, a, b, adm, one, two = _world(db)
    db.execute("UPDATE users SET archived_at=now() WHERE id=%s", (one["id"],))
    sites.archive_site(db, b["id"], co["id"])      # also archives its memberships
    api.republish_directory(db)
    doc = lake["doc"]
    assert "Deandre__Alberts" not in doc["people"]
    assert "beta-hall" not in doc["sites"]
    # Two_Sites is now on one open site, so the primary becomes unambiguous.
    assert doc["people"]["Two_Sites"]["sites"] == ["alpha-school"]
    assert doc["people"]["Two_Sites"]["primary_site"] == "alpha-school"


def test_an_unchanged_directory_is_not_rewritten(db, lake):
    _world(db)
    api.republish_directory(db)
    out = api.republish_directory(db)
    assert out["changed"] is False and len(lake["puts"]) == 1


def test_a_failed_publish_never_raises_into_the_request(db, monkeypatch):
    _world(db)

    class Broken:
        def put_object(self, **kw):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(api, "s3", lambda: Broken())
    monkeypatch.setattr(api, "_get_lake_json", lambda key: None)
    api._publish_directory(db)          # must return, not raise
    resp = {"statusCode": 200, "body": "{}"}
    assert api._directory_after(db, resp) is resp


def _event(sub):
    return {"requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def test_THE_handlers_republish_on_success_through_the_real_router(db, lake):
    """Archive a site, take someone off a project, rename a folder, change a
    role -- each through dispatch, and the object follows. Reverting any one
    wrap in dispatch makes the matching assertion go red."""
    co, a, b, adm, one, two = _world(db)
    api.republish_directory(db)
    lake["puts"].clear()

    def call(method, route, body=None):
        ev = _event("dir-adm")
        ev["body"] = json.dumps(body) if body is not None else None
        return api.dispatch(db, ev, method, route)

    r = call("DELETE", f"/members/dir-two/memberships/{b['id']}")
    assert r["statusCode"] == 200, r
    assert lake["doc"]["people"]["Two_Sites"]["sites"] == ["alpha-school"]

    r = call("PATCH", "/members/dir-one/role", {"global_role": "pm"})
    assert r["statusCode"] == 200, r
    assert lake["doc"]["people"]["Deandre__Alberts"]["role"] == "pm"

    r = call("PATCH", "/members/dir-one/folder", {"folder_name": "Deandre_A"})
    assert r["statusCode"] == 200, r
    assert "Deandre_A" in lake["doc"]["people"]
    assert "Deandre__Alberts" not in lake["doc"]["people"]

    r = call("PUT", f"/members/dir-two/memberships/{b['id']}", {"role": "pm"})
    assert r["statusCode"] == 200, r
    assert lake["doc"]["people"]["Two_Sites"]["sites"] == ["alpha-school", "beta-hall"]

    r = call("POST", f"/sites/{b['id']}/archive")
    assert r["statusCode"] == 200, r
    assert "beta-hall" not in lake["doc"]["sites"]

    r = call("POST", "/members/dir-two/archive")
    assert r["statusCode"] == 200, r
    assert "Two_Sites" not in lake["doc"]["people"]


def test_a_rejected_request_does_not_publish(db, lake):
    _world(db)
    api.republish_directory(db)
    lake["puts"].clear()
    ev = _event("dir-adm")
    ev["body"] = json.dumps({"global_role": "not-a-role"})
    r = api.dispatch(db, ev, "PATCH", "/members/dir-one/role")
    assert r["statusCode"] == 400
    assert lake["puts"] == []
