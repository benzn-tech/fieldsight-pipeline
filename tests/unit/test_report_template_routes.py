"""The template routes: who may do what, and which route wins.

SCOPE OF THIS FILE. The repository is replaced with an in-memory double, so
nothing here proves anything about SQL -- that is split between
tests/unit/test_report_templates_sql_parses.py (PostgreSQL's own parser) and
tests/integration/test_report_templates_repository.py (a real database). What
IS proved here is the part that lives in the routes and nowhere else: the
authorisation decisions, the validation, and the dispatch order.

THE test is `bindings is not read as a template id`. A route table that matches
"/templates/{id}" before "/templates/bindings" answers 404 for every binding
call, and a 404 on an endpoint gm/admin are the only ones allowed to use reads
exactly like a permissions bug -- so the search starts in the wrong place.
"""
import json

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

COMPANY = "c0000000-0000-0000-0000-000000000001"
OTHER_COMPANY = "c0000000-0000-0000-0000-000000000002"
GM = {"id": "u-gm", "company_id": COMPANY, "global_role": "gm", "cognito_sub": "s-gm"}
WORKER = {"id": "u-wk", "company_id": COMPANY, "global_role": "worker", "cognito_sub": "s-wk"}
OTHER_WORKER = {"id": "u-ow", "company_id": COMPANY, "global_role": "worker", "cognito_sub": "s-ow"}
PLATFORM = {"id": "u-pa", "company_id": OTHER_COMPANY, "global_role": "platform_admin",
            "cognito_sub": "s-pa"}

BODY = {"sections": [{"key": "what", "title": "What this was", "purpose": "x"}],
        "catch_all": {"key": "other", "title": "Anything else", "purpose": "y"},
        "excluded_subjects": [], "style": ["Keep it short."]}


class _Repo:
    """In-memory stand-in with the same visibility rule the SQL has."""

    def __init__(self):
        self.templates = {}
        self.versions = {}
        self.bindings = {}
        self._n = 0

    # -- reads
    def list_visible(self, conn, company_id, user_id, scope=None):
        out = [t for t in self.templates.values()
               if str(t["company_id"]) == str(company_id) and t["archived_at"] is None
               and (t["scope"] == "org" or str(t["owner_user_id"]) == str(user_id))]
        if scope:
            out = [t for t in out if t["scope"] == scope]
        return out

    def get_visible(self, conn, company_id, user_id, template_id):
        t = self.templates.get(str(template_id))
        if t is None or t["archived_at"] is not None:
            return None
        if str(t["company_id"]) != str(company_id):
            return None
        if t["scope"] == "personal" and str(t["owner_user_id"]) != str(user_id):
            return None
        return t

    def get_any(self, conn, template_id):
        return self.templates.get(str(template_id))

    def list_versions(self, conn, template_id):
        return sorted(self.versions.get(str(template_id), []),
                      key=lambda v: v["version"], reverse=True)

    def get_version(self, conn, template_id, version):
        rows = self.versions.get(str(template_id), [])
        if version is None:
            t = self.templates.get(str(template_id))
            version = t and t["current_version"]
            if not version:
                return None
        return next((v for v in rows if v["version"] == int(version)), None)

    # -- writes
    def create(self, conn, company_id, scope, owner_user_id, slug, name, description,
               report_type, created_by):
        for t in self.templates.values():
            if (str(t["company_id"]), t["scope"], str(t["owner_user_id"]), t["slug"]) == \
                    (str(company_id), scope, str(owner_user_id), slug) and t["archived_at"] is None:
                raise oa.UniqueViolation("duplicate slug")
        self._n += 1
        row = {"id": "t-%d" % self._n, "company_id": company_id, "scope": scope,
               "owner_user_id": owner_user_id, "slug": slug, "name": name,
               "description": description, "report_type": report_type,
               "current_version": 0, "archived_at": None, "created_by": created_by,
               "created_at": "now", "updated_at": "now"}
        self.templates[row["id"]] = row
        return row

    def add_version(self, conn, template_id, body, change_note, created_by):
        rows = self.versions.setdefault(str(template_id), [])
        row = {"id": "v-%d" % (len(rows) + 1), "template_id": template_id,
               "version": len(rows) + 1, "body": body, "change_note": change_note,
               "created_by": created_by, "created_at": "now"}
        rows.append(row)
        self.templates[str(template_id)]["current_version"] = row["version"]
        return row

    def update_meta(self, conn, template_id, name=None, description=None):
        t = self.templates[str(template_id)]
        if name is not None:
            t["name"] = name
        if description is not None:
            t["description"] = description
        return t

    def archive(self, conn, template_id):
        self.templates[str(template_id)]["archived_at"] = "now"
        return self.templates[str(template_id)]

    def copy_to_personal(self, conn, template_id, owner_user_id, slug, name, created_by):
        src = self.get_any(conn, template_id)
        version = self.get_version(conn, template_id, None)
        if src is None or version is None:
            return None
        new = self.create(conn, src["company_id"], "personal", owner_user_id, slug, name,
                          src["description"], src["report_type"], created_by)
        self.add_version(conn, new["id"], version["body"], "copied", created_by)
        return self.get_any(conn, new["id"])

    def list_bindings(self, conn, company_id):
        out = []
        for (cid, rtype), b in self.bindings.items():
            if str(cid) != str(company_id):
                continue
            t = self.templates[str(b["template_id"])]
            out.append(dict(b, template_name=t["name"], template_slug=t["slug"],
                            current_version=t["current_version"], archived_at=t["archived_at"]))
        return out

    def set_binding(self, conn, company_id, report_type, template_id, pinned_version, set_by):
        row = {"company_id": company_id, "report_type": report_type,
               "template_id": template_id, "pinned_version": pinned_version,
               "set_by": set_by, "set_at": "now"}
        self.bindings[(str(company_id), report_type)] = row
        return row

    def is_bound(self, conn, template_id):
        return any(str(b["template_id"]) == str(template_id) for b in self.bindings.values())


@pytest.fixture()
def repo(monkeypatch):
    r = _Repo()
    monkeypatch.setattr(oa, "report_templates", r)
    monkeypatch.setattr(oa, "companies",
                        type("C", (), {"get_company_by_id": staticmethod(
                            lambda conn, cid: {"id": cid})})())
    return r


def _event(method, path, body=None, qs=None):
    return {"httpMethod": method, "path": "/api/org" + path,
            "queryStringParameters": qs,
            "body": json.dumps(body) if body is not None else None}


def _routed(monkeypatch, caller, method, path, body=None, qs=None):
    """Go through dispatch, not straight to the handler.

    Calling the handler directly would prove the handler and say nothing about
    whether the route table ever reaches it -- which is the entire question
    when two patterns can match the same path.
    """
    monkeypatch.setattr(oa.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(oa.device_heartbeat, "parse_headers", lambda h: None)
    monkeypatch.setattr(oa.device_heartbeat, "record", lambda *a, **kw: None)
    ev = _event(method, path, body, qs)
    ev["requestContext"] = {"authorizer": {"claims": {"sub": caller["cognito_sub"]}}}
    return oa.dispatch(None, ev, method, path)


def _body(resp):
    return json.loads(resp["body"])


def _make(repo, caller, scope="org", name="Daily", owner=None):
    row = repo.create(None, caller["company_id"], scope,
                      owner if scope == "personal" else None,
                      name.lower().replace(" ", "-"), name, "", "daily", caller["id"])
    repo.add_version(None, row["id"], BODY, "first", caller["id"])
    return row


# ---- THE test: dispatch order ----------------------------------------------

def test_THE_test_bindings_is_not_read_as_a_template_id(repo, monkeypatch):
    """Through dispatch, because that is where the question lives. If
    "/templates/{id}" matched first, this would be a 404 that reads like a
    permissions bug on the one endpoint only gm/admin may use."""
    resp = _routed(monkeypatch, GM, "GET", "/templates/bindings")
    assert resp["statusCode"] == 200
    assert "bindings" in _body(resp), "matched the wrong route"


def test_setting_a_binding_reaches_the_binding_route_not_the_template_one(repo, monkeypatch):
    t = _make(repo, GM)
    resp = _routed(monkeypatch, GM, "PUT", "/templates/bindings/daily",
                   {"template_id": t["id"]})
    assert resp["statusCode"] == 200
    assert _body(resp)["report_type"] == "daily"


def test_every_template_route_is_actually_reachable(repo, monkeypatch):
    """A handler nothing dispatches to is a handler that does not exist. Each
    of these asserts only that the route table got somewhere sensible -- the
    behaviour is tested below by calling the handlers directly."""
    t = _make(repo, GM)
    cases = [
        ("GET", "/templates", None),
        ("POST", "/templates", {"scope": "personal", "name": "N", "body": BODY}),
        ("GET", "/templates/bindings", None),
        ("PUT", "/templates/bindings/daily", {"template_id": t["id"]}),
        ("GET", "/templates/%s" % t["id"], None),
        ("PATCH", "/templates/%s" % t["id"], {"name": "Renamed"}),
        ("GET", "/templates/%s/versions" % t["id"], None),
        ("POST", "/templates/%s/versions" % t["id"], {"body": BODY}),
        ("POST", "/templates/%s/versions/1/restore" % t["id"], None),
        ("POST", "/templates/%s/copy" % t["id"], None),
    ]
    for method, path, body in cases:
        resp = _routed(monkeypatch, GM, method, path, body)
        assert resp["statusCode"] in (200, 201, 409), "%s %s -> %s %s" % (
            method, path, resp["statusCode"], resp["body"])


def test_deleting_reaches_the_archive_route(repo, monkeypatch):
    t = _make(repo, GM)
    resp = _routed(monkeypatch, GM, "DELETE", "/templates/%s" % t["id"])
    assert resp["statusCode"] == 200
    assert _body(resp)["archived"] == t["id"]


# ---- personal scope --------------------------------------------------------

def test_a_gm_cannot_read_a_workers_personal_template(repo):
    t = _make(repo, WORKER, scope="personal", name="Mine", owner=WORKER["id"])
    resp = oa.get_report_template(None, GM, t["id"], _event("GET", "/templates/" + t["id"]))
    assert resp["statusCode"] == 404


def test_a_gm_cannot_write_a_workers_personal_template(repo):
    t = _make(repo, WORKER, scope="personal", name="Mine", owner=WORKER["id"])
    resp = oa.update_report_template(None, GM, t["id"],
                                     _event("PATCH", "/templates/x", {"name": "Theirs"}))
    assert resp["statusCode"] == 404, "404, not 403 -- 403 would confirm it exists"


def test_a_worker_owns_their_own_personal_template(repo):
    t = _make(repo, WORKER, scope="personal", name="Mine", owner=WORKER["id"])
    resp = oa.update_report_template(None, WORKER, t["id"],
                                     _event("PATCH", "/templates/x", {"name": "Renamed"}))
    assert resp["statusCode"] == 200
    assert _body(resp)["name"] == "Renamed"


def test_one_workers_personal_template_is_invisible_to_another(repo):
    t = _make(repo, WORKER, scope="personal", name="Mine", owner=WORKER["id"])
    resp = oa.get_report_template(None, OTHER_WORKER, t["id"], _event("GET", "/t"))
    assert resp["statusCode"] == 404


def test_the_list_shows_org_templates_plus_only_your_own(repo):
    _make(repo, GM, name="Company Daily")
    _make(repo, WORKER, scope="personal", name="Mine", owner=WORKER["id"])
    _make(repo, OTHER_WORKER, scope="personal", name="Theirs", owner=OTHER_WORKER["id"])
    names = {t["name"] for t in _body(
        oa.list_report_templates(None, WORKER, _event("GET", "/templates")))["templates"]}
    assert names == {"Company Daily", "Mine"}


# ---- org templates are gm/admin's -----------------------------------------

def test_a_worker_cannot_create_an_org_template(repo):
    resp = oa.create_report_template(None, WORKER, _event(
        "POST", "/templates", {"scope": "org", "name": "X", "body": BODY}))
    assert resp["statusCode"] == 403


def test_a_worker_can_create_their_own(repo):
    resp = oa.create_report_template(None, WORKER, _event(
        "POST", "/templates", {"scope": "personal", "name": "X", "body": BODY}))
    assert resp["statusCode"] == 201
    assert _body(resp)["scope"] == "personal"
    assert _body(resp)["current_version"] == 1, "created with its first body"


def test_a_worker_cannot_change_an_org_template(repo):
    t = _make(repo, GM)
    resp = oa.add_report_template_version(None, WORKER, t["id"], _event(
        "POST", "/v", {"body": BODY}))
    assert resp["statusCode"] == 403


def test_anyone_may_copy_an_org_template_into_their_own_library(repo):
    """Copying is how somebody who may not change the company's template gets
    one they can change."""
    t = _make(repo, GM)
    resp = oa.copy_report_template(None, WORKER, t["id"], _event("POST", "/copy"))
    assert resp["statusCode"] == 201
    copied = _body(resp)
    assert copied["scope"] == "personal" and copied["owner_user_id"] == WORKER["id"]


# ---- bindings are gm/admin only -------------------------------------------

def test_a_worker_cannot_choose_the_scheduled_template(repo):
    t = _make(repo, GM)
    resp = oa.set_report_template_binding(None, WORKER, "daily", _event(
        "PUT", "/b", {"template_id": t["id"]}))
    assert resp["statusCode"] == 403


def test_a_gm_can(repo):
    t = _make(repo, GM)
    resp = oa.set_report_template_binding(None, GM, "daily", _event(
        "PUT", "/b", {"template_id": t["id"]}))
    assert resp["statusCode"] == 200
    assert _body(resp)["effective_version"] == 1


def test_a_personal_template_cannot_write_the_companys_nightly_report(repo):
    """Nobody else can see it, so nobody else could say what the report is."""
    t = _make(repo, GM, scope="personal", name="Mine", owner=GM["id"])
    resp = oa.set_report_template_binding(None, GM, "daily", _event(
        "PUT", "/b", {"template_id": t["id"]}))
    assert resp["statusCode"] == 400
    assert "organisation template" in _body(resp)["error"]


def test_only_the_scheduled_kinds_are_bindable(repo):
    t = _make(repo, GM)
    resp = oa.set_report_template_binding(None, GM, "session", _event(
        "PUT", "/b", {"template_id": t["id"]}))
    assert resp["statusCode"] == 400


def test_everyone_may_read_the_bindings(repo):
    t = _make(repo, GM)
    oa.set_report_template_binding(None, GM, "daily", _event("PUT", "/b", {"template_id": t["id"]}))
    resp = oa.list_report_template_bindings(None, WORKER, _event("GET", "/templates/bindings"))
    assert resp["statusCode"] == 200
    assert _body(resp)["bindings"][0]["template_id"] == t["id"]


# ---- archiving --------------------------------------------------------------

def test_a_bound_template_cannot_be_archived(repo):
    """Archiving is an UPDATE; no foreign key can catch it. If this passed, the
    nightly run would point at something the Library no longer shows and the
    report would quietly come out in the default format."""
    t = _make(repo, GM)
    oa.set_report_template_binding(None, GM, "daily", _event("PUT", "/b", {"template_id": t["id"]}))
    resp = oa.archive_report_template(None, GM, t["id"])
    assert resp["statusCode"] == 409
    assert "scheduled report" in _body(resp)["error"]


def test_an_unbound_template_archives(repo):
    t = _make(repo, GM)
    assert oa.archive_report_template(None, GM, t["id"])["statusCode"] == 200
    assert oa.get_report_template(None, GM, t["id"], _event("GET", "/t"))["statusCode"] == 404


# ---- content only changes through a version --------------------------------

def test_patch_refuses_to_change_the_body(repo):
    """A route that could rename AND re-body would let an edit land with no
    version behind it."""
    t = _make(repo, GM)
    resp = oa.update_report_template(None, GM, t["id"], _event(
        "PATCH", "/t", {"body": BODY}))
    assert resp["statusCode"] == 400
    assert "versions" in _body(resp)["error"]


def test_a_restore_writes_a_new_version_rather_than_moving_back(repo):
    t = _make(repo, GM)
    oa.add_report_template_version(None, GM, t["id"], _event("POST", "/v", {"body": dict(BODY, style=["b"])}))
    resp = oa.restore_report_template_version(None, GM, t["id"], 1)
    assert resp["statusCode"] == 201
    assert _body(resp)["version"] == 3
    assert repo.get_version(None, t["id"], 2)["body"]["style"] == ["b"], "v2 untouched"


def test_restoring_a_version_that_does_not_exist_is_a_404(repo):
    t = _make(repo, GM)
    assert oa.restore_report_template_version(None, GM, t["id"], 9)["statusCode"] == 404


def test_a_concurrent_save_is_a_409_not_a_500(repo, monkeypatch):
    t = _make(repo, GM)

    def boom(*a, **kw):
        raise oa.UniqueViolation("duplicate key")

    monkeypatch.setattr(repo, "add_version", boom)
    resp = oa.add_report_template_version(None, GM, t["id"], _event("POST", "/v", {"body": BODY}))
    assert resp["statusCode"] == 409
    assert "reload" in _body(resp)["error"]


# ---- the body is checked at the door ---------------------------------------

@pytest.mark.parametrize("bad,why", [
    ({}, "no sections"),
    ({"sections": []}, "empty sections"),
    ({"sections": [{"title": "x"}], "catch_all": {"title": "c", "purpose": "p"}}, "no purpose"),
    ({"sections": [{"title": "x", "purpose": "p"}]}, "no catch_all"),
    ({"sections": [{"title": "x", "purpose": "p"}], "catch_all": {"title": "c"}}, "catch_all half-made"),
    ({"sections": [{"title": "x", "purpose": "p"}], "catch_all": {"title": "c", "purpose": "p"},
      "style": ["ok", ""]}, "blank style rule"),
])
def test_a_body_render_prompt_would_crash_on_is_refused_here(repo, bad, why):
    """render_prompt subscripts template["catch_all"] and s["title"]/["purpose"].
    A body missing them does not make a worse report -- it raises inside the
    non-VPC worker, long after the person who saved it has gone home."""
    resp = oa.create_report_template(None, GM, _event(
        "POST", "/templates", {"scope": "org", "name": "X", "body": bad}))
    assert resp["statusCode"] == 400, why


def test_a_good_body_is_accepted(repo):
    resp = oa.create_report_template(None, GM, _event(
        "POST", "/templates", {"scope": "org", "name": "X", "body": BODY}))
    assert resp["statusCode"] == 201


def test_a_template_with_no_name_is_refused(repo):
    resp = oa.create_report_template(None, GM, _event(
        "POST", "/templates", {"scope": "org", "name": "   ", "body": BODY}))
    assert resp["statusCode"] == 400


def test_a_duplicate_name_is_a_409_that_says_name_not_slug(repo):
    oa.create_report_template(None, GM, _event(
        "POST", "/templates", {"scope": "org", "name": "Daily", "body": BODY}))
    resp = oa.create_report_template(None, GM, _event(
        "POST", "/templates", {"scope": "org", "name": "Daily", "body": BODY}))
    assert resp["statusCode"] == 409
    assert "slug" not in _body(resp)["error"], "'slug' is a word this product never shows"


def test_a_chinese_name_does_not_collide_with_every_other_chinese_name(repo):
    """slugify falls back to a unique token rather than an empty slug; two
    differently-named Chinese templates must both be creatable."""
    a = oa.create_report_template(None, GM, _event(
        "POST", "/templates", {"scope": "org", "name": "每日报告", "body": BODY}))
    b = oa.create_report_template(None, GM, _event(
        "POST", "/templates", {"scope": "org", "name": "每周报告", "body": BODY}))
    assert a["statusCode"] == 201 and b["statusCode"] == 201
    assert _body(a)["name"] == "每日报告", "the NAME keeps its characters"
    assert _body(a)["slug"] != _body(b)["slug"]


# ---- cross-company ----------------------------------------------------------

def test_a_normal_caller_cannot_name_another_company(repo):
    resp = oa.list_report_templates(None, WORKER, _event(
        "GET", "/templates", qs={"company_id": OTHER_COMPANY}))
    assert resp["statusCode"] == 403


def test_a_platform_admin_can(repo):
    """Without this branch the operator company's empty Library reads as 'this
    company owns nothing', which is the standing 200-empty-list trap."""
    _make(repo, GM)
    resp = oa.list_report_templates(None, PLATFORM, _event(
        "GET", "/templates", qs={"company_id": COMPANY}))
    assert resp["statusCode"] == 200
    assert len(_body(resp)["templates"]) == 1
