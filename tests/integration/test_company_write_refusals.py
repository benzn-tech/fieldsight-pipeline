"""Integration: writes into an inactive company (spec 2026-11-01-company-offboarding §5.6,
Task 5) against real Postgres -- a really suspended company, the real dispatch, the real
companies.status read inside the request's transaction.

What the dispatch path can show: a company's own admin is already refused 403 by the
caller gate (Task 3) when the flag is on, so the 409 is defence in depth there and is
asserted by calling the handler directly; through dispatch the observable outcomes are
the flag-off shadow (allowed + COMPANY_GATE_WOULD_REFUSE) and the platform_admin exemption.
The one write that a caller of an ACTIVE company can aim at an inactive company's data --
their own session recorded on its site (unname, recorder regenerate) -- answers 409
through dispatch.

Nothing is committed (the `db` fixture rolls back), so no company_status_events cleanup.
Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import logging
import uuid

import psycopg
import pytest

from repositories import companies, memberships, recordings, sites, topics, users

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")

FLAG = "COMPANY_STATUS_ENFORCE"
DATE = "2026-10-30"
SID_A = "sid" + "a" * 32
SID_B = "sid" + "b" * 32


class _Cognito:
    class exceptions:
        class UsernameExistsException(Exception):
            pass

    def __init__(self):
        self.created = []

    def admin_create_user(self, **kw):
        self.created.append(kw["Username"])
        return {"User": {"Attributes": [{"Name": "sub", "Value": f"sub-new-{uuid.uuid4().hex}"}]}}


class _S3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw["Key"])

    def get_object(self, **kw):
        raise org.ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    def generate_presigned_url(self, *a, **k):
        return "https://example.invalid/x"


def _user(db, co, role, tag, folder=None):
    u = users.upsert_user(db, f"sub-wr-{tag}", f"wr-{tag}@example.com",
                          company_id=co["id"], global_role=role)
    if folder:
        users.set_folder_name(db, u["cognito_sub"], folder)
    return users.get_user_by_sub(db, u["cognito_sub"])


@pytest.fixture
def w(db, monkeypatch):
    """A (to be suspended) with site sa and its admin aad + member am; B (active) with site
    sb, its admin bad and worker bw (external worker on sa, recorded there); platform_admin
    pa in an operator company P."""
    t = uuid.uuid4().hex[:8]
    a = companies.create_company(db, f"Co-WA-{t}")
    b = companies.create_company(db, f"Co-WB-{t}")
    p = companies.create_company(db, f"Co-WP-{t}")
    sa = sites.create_site(db, a["id"], f"Site-WA-{t}")
    sb = sites.create_site(db, b["id"], f"Site-WB-{t}")
    aad = _user(db, a, "admin", f"aad-{t}")
    am = _user(db, a, "worker", f"am-{t}")
    bad = _user(db, b, "admin", f"bad-{t}")
    folder = f"WBW_{t}"
    bw = _user(db, b, "worker", f"bw-{t}", folder=folder)
    pa = _user(db, p, "platform_admin", f"pa-{t}")
    memberships.ensure_membership(db, am["id"], sa["id"], "worker")
    memberships.ensure_membership(db, bw["id"], sb["id"], "worker")
    memberships.add_external_membership(db, bw["id"], sa["id"], "worker")
    cog, s3 = _Cognito(), _S3()
    monkeypatch.setattr(org, "cognito", lambda: cog)
    monkeypatch.setattr(org, "s3", lambda: s3)
    monkeypatch.setattr(org, "_publish_directory", lambda conn: None)
    monkeypatch.setattr(org, "_publish_site_coords", lambda row: None)
    return dict(a=a, b=b, p=p, sa=sa, sb=sb, aad=aad, am=am, bad=bad, bw=bw, pa=pa,
                folder=folder, cog=cog, s3=s3, t=t)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(FLAG, "on")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


def _suspend(db, co):
    db.execute("UPDATE companies SET status='suspended' WHERE id=%s", (co["id"],))


def _usable(db):
    assert db.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    assert db.execute("SELECT 1").fetchone()[0] == 1


def _call(db, u, method, route, body=None, qs=None):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": qs,
          "body": json.dumps(body) if body is not None else None, "headers": {},
          "requestContext": {"authorizer": {"claims": {"sub": u["cognito_sub"]}}}}
    res = org.dispatch(db, ev, method, route)
    return res["statusCode"], json.loads(res["body"])


def _refusals(caplog):
    return [r.getMessage() for r in caplog.records
            if "COMPANY_GATE_WOULD_REFUSE" in r.getMessage()]


def _n_users(db, co):
    return db.execute("SELECT count(*) FROM users WHERE company_id=%s", (co["id"],)).fetchone()[0]


def _n_sites(db, co):
    return db.execute("SELECT count(*) FROM sites WHERE company_id=%s", (co["id"],)).fetchone()[0]


def _invite(w, email):
    return {"email": email, "first_name": "New", "last_name": w["t"],
            "memberships": [{"site_id": str(w["sa"]["id"]), "role": "worker"}]}


# ---- create_member ---------------------------------------------------------------------

def test_create_member_into_a_suspended_company(db, w, flag_on):
    _suspend(db, w["a"])
    before = _n_users(db, w["a"])
    # Through dispatch the company's own admin never reaches the handler: the caller gate.
    assert _call(db, w["aad"], "POST", "/members", _invite(w, "x1@example.com")) == (
        403, {"error": "company_inactive"})
    # The handler's own refusal: 409, before Cognito and before any row.
    res = org.create_member(db, dict(w["aad"]), _invite(w, "x2@example.com"))
    assert res["statusCode"] == 409 and json.loads(res["body"]) == {"error": "company_inactive"}
    assert w["cog"].created == [] and _n_users(db, w["a"]) == before
    _usable(db)


def test_create_member_platform_admin_is_exempt(db, w, flag_on):
    _suspend(db, w["a"])
    status, body = _call(db, w["pa"], "POST", "/members", _invite(w, "pa-new@example.com"))
    assert status == 201, body
    assert str(body["user"]["company_id"]) == str(w["a"]["id"])
    assert [str(m["site_id"]) for m in body["memberships"]] == [str(w["sa"]["id"])]


def test_create_member_flag_off_writes_and_logs(db, w, flag_off, caplog):
    _suspend(db, w["a"])
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        status, body = _call(db, w["aad"], "POST", "/members", _invite(w, "off@example.com"))
    assert status == 201, body
    assert w["cog"].created == ["off@example.com"]
    lines = _refusals(caplog)
    assert len(lines) == 1 and "create_member" in lines[0] and str(w["a"]["id"]) in lines[0]


def test_create_member_active_company_unchanged(db, w, flag_on, caplog):
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        status, _ = _call(db, w["aad"], "POST", "/members", _invite(w, "ok@example.com"))
    assert status == 201 and _refusals(caplog) == []


# ---- add_external_member ---------------------------------------------------------------

def _ext(w):
    return {"email": w["bad"]["email"], "role": "pm"}


def _ext_route(w):
    return f"/sites/{w['sa']['id']}/external-members"


def _ext_rows(db, w):
    return db.execute("SELECT count(*) FROM memberships WHERE user_id=%s AND site_id=%s",
                      (w["bad"]["id"], w["sa"]["id"])).fetchone()[0]


def test_add_external_member_onto_a_suspended_companys_site(db, w, flag_on):
    _suspend(db, w["a"])
    assert _call(db, w["aad"], "POST", _ext_route(w), _ext(w)) == (
        403, {"error": "company_inactive"})
    res = org.add_external_member(db, dict(w["aad"]), str(w["sa"]["id"]), _ext(w))
    assert res["statusCode"] == 409 and json.loads(res["body"]) == {"error": "company_inactive"}
    assert _ext_rows(db, w) == 0 and w["s3"].puts == []
    _usable(db)


def test_add_external_member_platform_admin_is_exempt(db, w, flag_on):
    _suspend(db, w["a"])
    status, body = _call(db, w["pa"], "POST", _ext_route(w), _ext(w))
    assert status == 201, body
    assert body["membership"]["external"] is True and _ext_rows(db, w) == 1


def test_add_external_member_flag_off_writes_and_logs(db, w, flag_off, caplog):
    _suspend(db, w["a"])
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        status, body = _call(db, w["aad"], "POST", _ext_route(w), _ext(w))
    assert status == 201, body
    lines = _refusals(caplog)
    assert len(lines) == 1 and "add_external_member" in lines[0]
    assert str(w["a"]["id"]) in lines[0]


# ---- site create -----------------------------------------------------------------------

def test_site_create_in_a_suspended_company(db, w, flag_on):
    _suspend(db, w["a"])
    before = _n_sites(db, w["a"])
    assert _call(db, w["aad"], "POST", "/sites", {"name": "Nope"}) == (
        403, {"error": "company_inactive"})
    res = org.create_org_site(db, dict(w["aad"]), {"name": "Nope"})
    assert res["statusCode"] == 409 and json.loads(res["body"]) == {"error": "company_inactive"}
    assert _n_sites(db, w["a"]) == before
    _usable(db)


def test_site_create_platform_admin_is_exempt(db, w, flag_on):
    _suspend(db, w["a"])
    before = _n_sites(db, w["a"])
    status, body = _call(db, w["pa"], "POST", "/sites",
                         {"name": "Fix-up", "target_company_id": str(w["a"]["id"])})
    assert status == 201, body
    assert str(body["company_id"]) == str(w["a"]["id"]) and _n_sites(db, w["a"]) == before + 1


def test_site_create_flag_off_writes_and_logs(db, w, flag_off, caplog):
    _suspend(db, w["a"])
    before = _n_sites(db, w["a"])
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        status, body = _call(db, w["aad"], "POST", "/sites", {"name": "Shadow"})
    assert status == 201, body
    assert _n_sites(db, w["a"]) == before + 1
    lines = _refusals(caplog)
    assert len(lines) == 1 and "site_create" in lines[0]


# ---- the other member/site writes (handler level, real rows) ---------------------------

def test_revive_unarchive_and_site_writes_are_refused(db, w, flag_on):
    aad, am, sa = dict(w["aad"]), w["am"], w["sa"]
    memberships.archive_membership(db, am["id"], sa["id"])
    sites.archive_site(db, w["sb"]["id"], w["b"]["id"])           # unrelated, B
    users.archive_user(db, am["cognito_sub"], w["a"]["id"])
    archived_site = sites.create_site(db, w["a"]["id"], f"Closed-{w['t']}")
    sites.archive_site(db, archived_site["id"], w["a"]["id"])
    _suspend(db, w["a"])

    def refused(res):
        return res["statusCode"] == 409 and json.loads(res["body"]) == {
            "error": "company_inactive"}

    # unarchive the member: still archived
    assert refused(org.archive_member_endpoint(db, aad, am["cognito_sub"], "unarchive"))
    assert users.get_user_by_sub(db, am["cognito_sub"])["archived_at"] is not None
    # reopen the site: still archived
    assert refused(org.archive_site_endpoint(db, aad, str(archived_site["id"]), "unarchive"))
    assert sites.get_site(db, archived_site["id"])["archived_at"] is not None
    # rename the site: unchanged
    assert refused(org.patch_org_site(db, aad, str(sa["id"]), {"name": "Renamed"}))
    assert sites.get_site(db, sa["id"])["name"] == sa["name"]
    # archiving is never refused
    assert org.archive_site_endpoint(db, aad, str(sa["id"]), "archive")["statusCode"] == 200
    _usable(db)


def test_put_membership_revive_is_refused_and_platform_admin_revives(db, w, flag_on):
    """PUT /members/{sub}/memberships/{site} revives an archived membership
    (ensure_membership); into a suspended company only platform_admin may."""
    am, sa = w["am"], w["sa"]
    memberships.archive_membership(db, am["id"], sa["id"])
    _suspend(db, w["a"])
    res = org.put_member_membership(db, dict(w["aad"]), am["cognito_sub"], str(sa["id"]),
                                    {"role": "pm"})
    assert res["statusCode"] == 409
    assert memberships.get_membership(db, am["id"], sa["id"])["archived_at"] is not None
    status, body = _call(db, w["pa"], "PUT",
                         f"/members/{am['cognito_sub']}/memberships/{sa['id']}", {"role": "pm"})
    assert status == 200, body
    assert memberships.get_membership(db, am["id"], sa["id"])["archived_at"] is None


# ---- a B person's own session on A's site ----------------------------------------------

@pytest.fixture
def session_on_a(db, w, monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    f = w["folder"]
    for site, co, sid in ((w["sa"], w["a"], SID_A), (w["sb"], w["b"], SID_B)):
        recordings.insert_pending(db, co["id"], w["bw"]["id"], site["id"], "audio",
                                  f"users/{f}/audio/{DATE}/dev_09-00-00_{sid}_c0.wav",
                                  uuid.uuid4().hex, f"{DATE}T01:00:00Z")
        topics.upsert_topic(db, site["id"], DATE, f"topic {sid[:4]}", user_id=w["bw"]["id"],
                            source_s3_key=f"extractions/{f}/{DATE}/{sid}.json",
                            time_range="09:00 - 09:30")
    return w


def _unname(db, u, sid, folder):
    return _call(db, u, "DELETE", f"/sessions/{folder}_{DATE}_09-00-00_{sid}/speaker-names",
                 qs={"name": "Somebody"})


def test_unname_on_the_suspended_companys_site_is_refused(db, session_on_a, flag_on):
    w = session_on_a
    assert _unname(db, w["bw"], SID_A, w["folder"])[0] == 200          # A active
    _suspend(db, w["a"])
    assert _unname(db, w["bw"], SID_A, w["folder"]) == (409, {"error": "company_inactive"})
    assert _unname(db, w["bw"], SID_B, w["folder"])[0] == 200          # own company's site
    assert _unname(db, w["pa"], SID_A, w["folder"])[0] == 200          # R5
    _usable(db)


def test_unname_flag_off_allowed_and_logged(db, session_on_a, flag_off, caplog):
    w = session_on_a
    _suspend(db, w["a"])
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        assert _unname(db, w["bw"], SID_A, w["folder"])[0] == 200
    lines = _refusals(caplog)
    assert len(lines) == 1 and "unname_speaker" in lines[0] and str(w["a"]["id"]) in lines[0]


def test_recorder_regenerate_guard_on_the_suspended_companys_site(db, session_on_a, flag_on):
    w = session_on_a
    bw = dict(w["bw"])
    guard = lambda sid: org._session_site_company_guard(  # noqa: E731
        db, bw, w["folder"], DATE, sid, "re-extraction", allow_recorder=True)
    assert guard(SID_A) is None
    _suspend(db, w["a"])
    res = guard(SID_A)
    assert res["statusCode"] == 409 and json.loads(res["body"]) == {"error": "company_inactive"}
    assert guard(SID_B) is None
    _usable(db)


# ---- own-folder writes that are NOT writes into A (Task 4 already clipped them) ---------

def test_day_report_content_of_a_mixed_day_leaves_the_suspended_site_out(
        db, session_on_a, flag_on, monkeypatch):
    """day_report_generate writes the B person's own day report; its content comes from the
    clipped _day_report_rows, so with A suspended nothing of A's site enters it -- a write
    of B's data, not into A's. Refusing it would block B's own report."""
    w = session_on_a
    monkeypatch.setattr(org, "GRADED_ROLES", True)
    monkeypatch.setattr(org, "_merged_keys_for", lambda conn, uid, date: [])
    monkeypatch.setattr(org, "_get_lake_json", lambda key: None)
    ev = {"queryStringParameters": {}}
    content, err = org._assemble_day_report(db, dict(w["bw"]), DATE, ev)
    assert err is None and set(content["siteIds"]) == {str(w["sa"]["id"]), str(w["sb"]["id"])}
    _suspend(db, w["a"])
    content, err = org._assemble_day_report(db, dict(w["bw"]), DATE, ev)
    assert err is None and content["siteIds"] == [str(w["sb"]["id"])]
    assert all(SID_A not in s for s in content["sessionIds"])


def test_photo_selection_cannot_name_the_suspended_sites_photo(db, session_on_a, flag_on):
    """put_photo_selection writes the B person's own day-level choice; with A suspended it
    can only name photos it can still see (Task 4 clipped _day_photo_block), so it cannot
    reach A's photographs."""
    w = session_on_a
    f = w["folder"]
    for name, site, co in (("IMG_a.jpg", w["sa"], w["a"]), ("IMG_b.jpg", w["sb"], w["b"])):
        recordings.insert_pending(db, co["id"], w["bw"]["id"], site["id"], "photo",
                                  f"users/{f}/pictures/{DATE}/{name}", uuid.uuid4().hex,
                                  f"{DATE}T01:00:00Z")
    _suspend(db, w["a"])
    route = f"/days/{DATE}/photos/selection"
    status, body = _call(db, w["bw"], "PUT", route, {"excluded": ["IMG_a.jpg"]})
    assert status == 400 and "IMG_a.jpg" in body["error"]
    status, body = _call(db, w["bw"], "PUT", route, {"excluded": ["IMG_b.jpg"]})
    assert status == 200, body
    assert [p["filename"] for p in body["photos"]] == ["IMG_b.jpg"]
