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
    assert doc["version"] == 2 and doc["published_at"]

    p = doc["people"]["Deandre__Alberts"]
    assert p["name"] == "Deandre Alberts" and p["role"] == "site_manager"
    assert p["company_id"] == str(co["id"])
    assert p["primary_site"] == str(a["id"]) and p["sites"] == [str(a["id"])]

    # Two open sites and no recent topics: naming one would be a guess.
    assert doc["people"]["Two_Sites"]["primary_site"] is None
    assert doc["people"]["Two_Sites"]["sites"] == sorted([str(a["id"]), str(b["id"])])
    # No site at all.
    assert doc["people"]["Ada_Min"]["primary_site"] is None
    assert doc["people"]["Ada_Min"]["sites"] == []
    # No folder, nothing the pipeline could key them by.
    assert not any(v["name"] == "No Folder" for v in doc["people"].values())

    s = doc["sites"][str(a["id"])]
    assert (s["slug"], s["name"], s["location"], s["client"]) == (
        "alpha-school", "Alpha School", "Chch", "MoE")
    assert s["company_id"] == str(co["id"])


def test_archived_users_sites_and_memberships_are_left_out(db, lake):
    co, a, b, adm, one, two = _world(db)
    db.execute("UPDATE users SET archived_at=now() WHERE id=%s", (one["id"],))
    sites.archive_site(db, b["id"], co["id"])      # also archives its memberships
    api.republish_directory(db)
    doc = lake["doc"]
    assert "Deandre__Alberts" not in doc["people"]
    assert str(b["id"]) not in doc["sites"]
    # Two_Sites is now on one open site, so the primary becomes unambiguous.
    assert doc["people"]["Two_Sites"]["sites"] == [str(a["id"])]
    assert doc["people"]["Two_Sites"]["primary_site"] == str(a["id"])


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
    assert lake["doc"]["people"]["Two_Sites"]["sites"] == [str(a["id"])]

    r = call("PATCH", "/members/dir-one/role", {"global_role": "pm"})
    assert r["statusCode"] == 200, r
    assert lake["doc"]["people"]["Deandre__Alberts"]["role"] == "pm"

    r = call("PATCH", "/members/dir-one/folder", {"folder_name": "Deandre_A"})
    assert r["statusCode"] == 200, r
    assert "Deandre_A" in lake["doc"]["people"]
    assert "Deandre__Alberts" not in lake["doc"]["people"]

    r = call("PUT", f"/members/dir-two/memberships/{b['id']}", {"role": "pm"})
    assert r["statusCode"] == 200, r
    assert lake["doc"]["people"]["Two_Sites"]["sites"] == sorted([str(a["id"]), str(b["id"])])

    r = call("POST", f"/sites/{b['id']}/archive")
    assert r["statusCode"] == 200, r
    assert str(b["id"]) not in lake["doc"]["sites"]

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


def test_THE_two_companies_with_the_same_slug_do_not_collide(db, lake):
    c1 = companies.create_company(db, "SlugCoOne")
    c2 = companies.create_company(db, "SlugCoTwo")
    s1 = sites.create_site(db, c1["id"], "One Main", slug="main-site")
    s2 = sites.create_site(db, c2["id"], "Two Main", slug="main-site")
    u1 = users.upsert_user(db, "slug-u1", "u1@x.nz", company_id=c1["id"], first_name="U", last_name="One")
    users.set_folder_name(db, "slug-u1", "U_One")
    memberships.ensure_membership(db, u1["id"], s1["id"], "worker")
    api.republish_directory(db)
    doc = lake["doc"]
    assert doc["sites"][str(s1["id"])]["name"] == "One Main"
    assert doc["sites"][str(s2["id"])]["name"] == "Two Main"
    assert doc["sites"][str(s1["id"])]["slug"] == doc["sites"][str(s2["id"])]["slug"] == "main-site"
    # U_One points at ITS site by id, never at the other company's.
    assert doc["people"]["U_One"]["primary_site"] == str(s1["id"])


def _topic(db, site, user, date_expr, n=1):
    for i in range(n):
        db.execute(
            "INSERT INTO topics (site_id, user_id, report_date, title) "
            f"VALUES (%s, %s, {date_expr}, %s)", (site["id"], user["id"], f"t{i}"))


def test_THE_primary_site_is_the_busiest_in_30_days_then_latest_then_null(db, lake):
    co, a, b, adm, one, two = _world(db)
    ids = {"a": str(a["id"]), "b": str(b["id"])}
    # No topics: null.
    api.republish_directory(db)
    assert lake["doc"]["people"]["Two_Sites"]["primary_site"] is None
    # More recent topics on b: b. Old topics (40 days) do not count.
    _topic(db, a, two, "current_date - 40", 5)
    _topic(db, b, two, "current_date - 1", 2)
    _topic(db, a, two, "current_date - 2", 1)
    api.republish_directory(db)
    assert lake["doc"]["people"]["Two_Sites"]["primary_site"] == ids["b"]
    # Tie at 2 each: the one with the latest topic (a, added today-0).
    _topic(db, a, two, "current_date", 1)
    api.republish_directory(db)
    assert lake["doc"]["people"]["Two_Sites"]["primary_site"] == ids["a"]
    # One membership: that site regardless of topics.
    assert lake["doc"]["people"]["Deandre__Alberts"]["primary_site"] == ids["a"]


def test_THE_a_failing_publisher_query_does_not_roll_back_the_save(db, monkeypatch):
    co, a, b, adm, one, two = _world(db)
    monkeypatch.setattr(api, "s3", lambda: pytest.fail("must not reach S3"))
    monkeypatch.setattr(api, "_get_lake_json", lambda key: None)

    def boom(conn):
        conn.execute("SELECT * FROM no_such_table")
    monkeypatch.setattr(api.directory_repo, "live_users", boom)
    ev = _event("dir-adm")
    ev["body"] = json.dumps({"global_role": "gm"})
    r = api.dispatch(db, ev, "PATCH", "/members/dir-one/role")
    assert r["statusCode"] == 200
    # The request's own transaction is still usable and holds the write.
    row = db.execute("SELECT global_role FROM users WHERE cognito_sub='dir-one'").fetchone()
    assert row[0] == "gm"


def test_a_dry_run_returns_the_document_and_writes_nothing(db, lake):
    _world(db)
    out = api.republish_directory(db, dry_run=True)
    assert out["dry_run"] is True and "Two_Sites" in out["document"]["people"]
    assert lake["puts"] == []
