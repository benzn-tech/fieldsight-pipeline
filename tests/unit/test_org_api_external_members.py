"""POST /api/org/sites/{id}/external-members (project-owned tenancy P1).

An admin of the site's company (or a platform_admin) adds an EXISTING user of
another company to ONE of its sites. The auth matrix and the refusals are
pinned here; the SQL is pinned by tests/integration/test_external_members.py,
because a connection double never parses a query.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


def make_event(method, path, sub="sub-1", body=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def body_of(res):
    return json.loads(res["body"])


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return self

    def execute(self, *a, **k):
        class _Cur:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None
        return _Cur()

SITE_CO = "c-site"
SITE = {"id": "s-9", "company_id": SITE_CO, "name": "Tower A", "archived_at": None}
EXTERNAL = {"id": "u-ext", "cognito_sub": "sub-ext", "company_id": "c-home", "email": "ext@home.nz",
            "first_name": "Eve", "last_name": "Xu", "global_role": "worker",
            "company_name": "Home Ltd", "archived_at": None}


def caller(role, company=SITE_CO):
    return {"id": "u-c", "cognito_sub": "sub-c", "company_id": company, "email": "c@x.nz",
            "first_name": "C", "last_name": "C", "global_role": role,
            "avatar_s3_key": None, "created_at": "2026-07-04"}


@pytest.fixture
def env(monkeypatch):
    st = {"caller": caller("admin"), "site": dict(SITE), "target": dict(EXTERNAL),
          "existing": None, "added": [], "mails": []}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda c, sub: dict(st["caller"]))
    monkeypatch.setattr(org.sites, "get_site", lambda c, sid: st["site"])
    monkeypatch.setattr(org.users, "get_user_by_email_global",
                        lambda c, e: st["target"] if e.lower() == "ext@home.nz" else None)
    monkeypatch.setattr(org.companies, "get_company_by_id", lambda c, i: {"id": i, "name": "Site Co"})
    monkeypatch.setattr(org.memberships, "get_membership", lambda c, u, s: st["existing"])

    def _add(c, u, s, r):
        st["added"].append((u, s, r))
        return {"id": "m-1", "user_id": u, "site_id": s, "role": r, "external": True}
    monkeypatch.setattr(org.memberships, "add_external_membership", _add)
    monkeypatch.setattr(org, "_notify_external_member", lambda *a: st["mails"].append(a))
    return st


def post(body=None):
    return org.lambda_handler(
        make_event("POST", "/api/org/sites/s-9/external-members", sub="sub-c",
                   body={"email": "Ext@Home.nz", "role": "pm"} if body is None else body), None)


def test_admin_adds_external_member(env):
    res = post()
    assert res["statusCode"] == 201
    m = body_of(res)["membership"]
    assert m["external"] is True and m["role"] == "pm"
    assert m["user_name"] == "Eve Xu" and m["home_company_name"] == "Home Ltd"
    assert env["added"] == [("u-ext", "s-9", "pm")]
    assert env["mails"] == [("ext@home.nz", "Tower A", "Site Co")]


def test_platform_admin_adds_to_any_company(env):
    env["caller"] = caller("platform_admin", company="c-operator")
    assert post()["statusCode"] == 201


@pytest.mark.parametrize("role", ["gm", "pm", "site_manager", "worker"])
def test_other_roles_are_refused(env, role):
    env["caller"] = caller(role)
    assert post()["statusCode"] == 403
    assert env["added"] == []


def test_admin_of_another_company_gets_404(env):
    env["caller"] = caller("admin", company="c-other")
    assert post()["statusCode"] == 404
    assert env["added"] == []


def test_unknown_site_404(env):
    env["site"] = None
    assert post()["statusCode"] == 404


def test_unknown_email_404(env):
    assert post({"email": "nobody@x.nz", "role": "worker"})["statusCode"] == 404
    assert env["added"] == []


def test_same_company_user_is_400(env):
    env["target"] = {**EXTERNAL, "company_id": SITE_CO}
    res = post()
    assert res["statusCode"] == 400 and "normal add member" in body_of(res)["error"]
    assert env["added"] == []


def test_live_member_is_409(env):
    env["existing"] = {"id": "m-0", "archived_at": None, "external": True}
    assert post()["statusCode"] == 409
    assert env["added"] == []


def test_archived_membership_is_revived(env):
    env["existing"] = {"id": "m-0", "archived_at": "2026-09-01", "external": True}
    assert post()["statusCode"] == 201
    assert env["added"] == [("u-ext", "s-9", "pm")]


@pytest.mark.parametrize("body", [{"email": "ext@home.nz", "role": "admin"},
                                  {"email": "ext@home.nz"},
                                  {"email": "nope", "role": "pm"},
                                  {"role": "pm"}])
def test_bad_body_is_400(env, body):
    assert post(body)["statusCode"] == 400


def test_archived_site_is_409(env):
    env["site"] = {**SITE, "archived_at": "2026-09-01"}
    assert post()["statusCode"] == 409


def test_email_failure_does_not_fail_the_request(monkeypatch):
    """The notice itself: a sender that raises is logged, never propagated."""
    import email_sender

    class Boom(email_sender.EmailSender):
        def send(self, *a, **k):
            raise RuntimeError("ses down")
    monkeypatch.setattr(email_sender, "get_sender", lambda: Boom())
    org._notify_external_member("a@b.nz", "Tower A", "Site Co")  # must not raise


def test_notice_names_site_and_company(monkeypatch):
    import email_sender
    sent = []

    class Rec(email_sender.EmailSender):
        def send(self, to, subject, body_text, body_html=None):
            sent.append((to, subject, body_text))
    monkeypatch.setattr(email_sender, "get_sender", lambda: Rec())
    org._notify_external_member("a@b.nz", "Tower A", "Site Co")
    to, subject, text = sent[0]
    assert subject == "You've been added to Tower A on FieldSight"
    assert "Site Co" in text and "Tower A" in text


# ---- staffing routes reach external members, and only them
def _delete(monkeypatch, membership):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda c, sub: (
        caller("admin") if sub == "sub-c" else dict(EXTERNAL)))
    monkeypatch.setattr(org.sites, "get_site", lambda c, sid: dict(SITE))
    monkeypatch.setattr(org.memberships, "get_membership", lambda c, u, s: membership)
    monkeypatch.setattr(org.memberships, "archive_membership",
                        lambda c, u, s: {"user_id": u, "site_id": s})
    return org.lambda_handler(make_event("DELETE", "/api/org/members/sub-ext/memberships/s-9",
                                         sub="sub-c"), None)


def test_delete_membership_reaches_an_external_member(monkeypatch):
    assert _delete(monkeypatch, {"external": True, "archived_at": None})["statusCode"] == 200


def test_delete_membership_still_404s_for_a_stranger(monkeypatch):
    assert _delete(monkeypatch, None)["statusCode"] == 404
