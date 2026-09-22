"""Every route that hands back a template hands back its sections too.

WHAT HAPPENED. `_template_payload` took `versions` as an optional argument and
only `create` ever passed it. So a template created through the Library looked
right for as long as you stayed on the page, and the moment you reloaded and
clicked it the panel read "No schema available yet." The template was intact in
Aurora. The request had dropped half of it, and the sentence on screen was
about the template rather than about the request.

Thirty-six route tests were green through all of it. They checked status codes,
authorisation, ordering and error wording -- everything except whether the
thing that came back was usable by the page that asked for it. That is the gap
this file exists to close, and it is worth more than the fix: "replace the
implementation, keep the return shape" loses content while the shape still
matches, and there is at least one more of those coming (the real template
extraction).

THE test is `every route that returns a template returns its sections`. It is
written as a loop over the routes rather than one test per route on purpose:
the bug was not that one route was wrong, it was that three were never asked.
"""
import json

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

COMPANY = "c0000000-0000-0000-0000-000000000001"
GM = {"id": "u-gm", "company_id": COMPANY, "global_role": "gm", "cognito_sub": "s-gm"}

BODY = {"sections": [{"key": "what", "title": "What this was", "purpose": "x"},
                     {"key": "did", "title": "Decided", "purpose": "y"}],
        "catch_all": {"key": "other", "title": "Anything else", "purpose": "z"},
        "excluded_subjects": [], "style": ["Short."]}


class _Repo:
    """Mirrors the SQL closely enough for the payload question: list carries a
    section_count, get_version answers for the current version."""

    def __init__(self):
        self.rows = {}
        self.versions = {}
        self._n = 0

    def _row(self, tid, with_count=False):
        r = dict(self.rows[tid])
        if with_count:
            v = self._current(tid)
            r["section_count"] = len(v["body"]["sections"]) if v else None
        return r

    def _current(self, tid):
        cur = self.rows[tid]["current_version"]
        return next((v for v in self.versions.get(tid, []) if v["version"] == cur), None)

    def list_visible(self, conn, company_id, user_id, scope=None):
        return [self._row(t, with_count=True) for t in self.rows]

    def get_visible(self, conn, company_id, user_id, template_id):
        return self._row(str(template_id)) if str(template_id) in self.rows else None

    def get_any(self, conn, template_id):
        return self._row(str(template_id)) if str(template_id) in self.rows else None

    def get_version(self, conn, template_id, version):
        tid = str(template_id)
        if tid not in self.rows:
            return None
        if version is None:
            return self._current(tid)
        return next((v for v in self.versions.get(tid, []) if v["version"] == version), None)

    def list_versions(self, conn, template_id):
        return sorted(self.versions.get(str(template_id), []),
                      key=lambda v: v["version"], reverse=True)

    def create(self, conn, company_id, scope, owner_user_id, slug, name, description,
               report_type, created_by):
        self._n += 1
        tid = "t-%d" % self._n
        self.rows[tid] = {"id": tid, "company_id": company_id, "scope": scope,
                          "owner_user_id": owner_user_id, "slug": slug, "name": name,
                          "description": description, "report_type": report_type,
                          "current_version": 0, "archived_at": None,
                          "created_by": created_by, "created_at": "now", "updated_at": "now"}
        return self._row(tid)

    def add_version(self, conn, template_id, body, change_note, created_by):
        tid = str(template_id)
        rows = self.versions.setdefault(tid, [])
        v = {"id": "v-%d" % (len(rows) + 1), "template_id": tid, "version": len(rows) + 1,
             "body": body, "change_note": change_note, "created_by": created_by,
             "created_at": "now"}
        rows.append(v)
        self.rows[tid]["current_version"] = v["version"]
        return v

    def update_meta(self, conn, template_id, name=None, description=None):
        r = self.rows[str(template_id)]
        if name is not None:
            r["name"] = name
        return self._row(str(template_id))

    def copy_to_personal(self, conn, template_id, owner_user_id, slug, name, created_by):
        src = self.rows[str(template_id)]
        cur = self._current(str(template_id))
        if cur is None:
            return None
        new = self.create(conn, src["company_id"], "personal", owner_user_id, slug, name,
                          src["description"], src["report_type"], created_by)
        self.add_version(conn, new["id"], cur["body"], "copied", created_by)
        return self._row(new["id"])

    def list_bindings(self, conn, company_id):
        return []

    def is_bound(self, conn, template_id):
        return False


@pytest.fixture()
def repo(monkeypatch):
    r = _Repo()
    monkeypatch.setattr(oa, "report_templates", r)
    monkeypatch.setattr(oa, "companies",
                        type("C", (), {"get_company_by_id": staticmethod(
                            lambda conn, cid: {"id": cid})})())
    return r


def _event(body=None, qs=None):
    return {"httpMethod": "GET", "path": "/api/org/templates",
            "queryStringParameters": qs,
            "body": json.dumps(body) if body is not None else None}


def _body(resp):
    return json.loads(resp["body"])


def _make(repo):
    created = oa.create_report_template(None, GM, _event(
        {"scope": "org", "name": "Site Daily", "report_type": "daily", "body": BODY}))
    return _body(created)["id"]


def _sections(payload):
    """What the page reads: the last version's sections."""
    versions = payload.get("versions") or []
    if not versions:
        return None
    return versions[-1]["body"]["sections"]


# ---- THE test ---------------------------------------------------------------

def test_THE_test_every_route_that_returns_a_template_returns_its_sections(repo):
    """A loop, not four tests: the bug was not one wrong route, it was three
    that nobody asked."""
    tid = _make(repo)

    routes = {
        "create": lambda: oa.create_report_template(None, GM, _event(
            {"scope": "org", "name": "Another", "report_type": "daily", "body": BODY})),
        "get": lambda: oa.get_report_template(None, GM, tid, _event()),
        "patch": lambda: oa.update_report_template(None, GM, tid, _event({"name": "Renamed"})),
        "copy": lambda: oa.copy_report_template(None, GM, tid, _event({"name": "My copy"})),
    }
    for name, call in routes.items():
        payload = _body(call())
        sections = _sections(payload)
        assert sections, "%s returned a template with no sections" % name
        assert [s["title"] for s in sections] == ["What this was", "Decided"], name


def test_the_list_says_how_big_each_template_is(repo):
    """list does not carry every body -- that would hand back every version of
    every template to draw one number -- but the row has to be able to say
    "2 sections" without a second request."""
    _make(repo)
    rows = _body(oa.list_report_templates(None, GM, _event()))["templates"]
    assert rows[0]["section_count"] == 2


def test_a_count_that_was_not_computed_is_absent_not_zero(repo):
    """0 sections and "this query did not ask" are different facts, and a
    client cannot tell them apart from a number alone."""
    tid = _make(repo)
    payload = _body(oa.get_report_template(None, GM, tid, _event()))
    assert "section_count" not in payload


# ---- the states that used to share one sentence -----------------------------

def test_a_template_with_no_body_yet_comes_back_with_an_empty_version_list(repo):
    """Distinguishable from "the request dropped them": current_version is 0,
    and versions is [] rather than missing."""
    row = repo.create(None, COMPANY, "org", None, "empty", "Empty", "", "daily", GM["id"])
    payload = _body(oa.get_report_template(None, GM, row["id"], _event()))
    assert payload["current_version"] == 0
    assert payload["versions"] == []


def test_a_template_with_a_body_never_comes_back_without_one(repo):
    tid = _make(repo)
    payload = _body(oa.get_report_template(None, GM, tid, _event()))
    assert payload["current_version"] == 1
    assert len(payload["versions"]) == 1
