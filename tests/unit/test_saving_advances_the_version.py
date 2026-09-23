"""Saving a template advances its version, and the API says so.

The Library showed "Version 1 · updated 23 Sep" on a template with four
versions in its history, and hung the "Current" badge on the oldest of them.
Both were display faults in the browser -- but they are indistinguishable from
the far worse thing they could have been, which is the server appending
versions without advancing `current_version`. If that were happening, every
save would land in the database and none of them would become the template
that gets used, and the only visible symptom would be... a version number that
does not move.

So this pins the rule that tells the two apart, from the outside:

    after N saves, the API's current_version is N, and the version it hands
    back is the body of the LAST save.

THE test is `the current version is the one just saved`. Not the count -- the
CONTENT. A number that advances while the body lags is the same bug wearing a
different symptom.

The browser-side faults are pinned separately in fieldsight-ui
(library-version-number.test.js); this file is deliberately about what the
server states, because that is what a client has no way to second-guess.
"""
import json

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

COMPANY = "c0000000-0000-0000-0000-000000000001"
GM = {"id": "u-gm", "company_id": COMPANY, "global_role": "gm", "cognito_sub": "s-gm"}


def _body(n):
    """A body whose text says which save it came from."""
    return {"sections": [{"key": "safety", "title": "Safety",
                          "purpose": "Description number %d." % n}],
            "catch_all": {"key": "other", "title": "Anything else", "purpose": "Rest."},
            "excluded_subjects": [], "style": []}


class _Repo:
    """Append-only versions and a current_version that follows them, which is
    what the SQL does. Kept faithful on that one point because it is the point
    the test is about."""

    def __init__(self):
        self.rows = {}
        self.versions = {}
        self._n = 0

    def _current(self, tid):
        cur = self.rows[tid]["current_version"]
        return next((v for v in self.versions.get(tid, []) if v["version"] == cur), None)

    def _row(self, tid, with_count=False):
        r = dict(self.rows[tid])
        if with_count:
            v = self._current(tid)
            r["section_count"] = len(v["body"]["sections"]) if v else None
        return r

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
        if name is not None:
            self.rows[str(template_id)]["name"] = name
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

    def bound_report_types(self, conn, template_id):
        return []


@pytest.fixture()
def repo(monkeypatch):
    r = _Repo()
    monkeypatch.setattr(oa, "report_templates", r)
    monkeypatch.setattr(oa, "companies",
                        type("C", (), {"get_company_by_id": staticmethod(
                            lambda conn, cid: {"id": cid})})())
    return r


def _event(body=None):
    return {"httpMethod": "POST", "path": "/api/org/templates",
            "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None}


def _json(resp):
    return json.loads(resp["body"])


def _create(repo):
    return _json(oa.create_report_template(None, GM, _event(
        {"scope": "org", "name": "Daily", "report_type": "daily", "body": _body(1)})))["id"]


def _save(tid, n):
    return oa.add_report_template_version(
        None, GM, tid, _event({"body": _body(n), "change_note": "Edited sections"}))


def _get(tid):
    return _json(oa.get_report_template(None, GM, tid, _event()))


# ---- THE test ---------------------------------------------------------------

def test_THE_test_the_current_version_is_the_one_just_saved(repo):
    """Not the count -- the CONTENT. A number that advances while the body lags
    is the same bug wearing a different symptom."""
    tid = _create(repo)
    for n in (2, 3, 4):
        _save(tid, n)

    payload = _get(tid)
    assert payload["current_version"] == 4
    assert payload["versions"][0]["body"]["sections"][0]["purpose"] == "Description number 4."


def test_current_version_equals_the_number_of_saves(repo):
    """The rule the browser could not check for itself: if the server appended
    versions without advancing this, every save would land and none would
    become the template that gets used."""
    tid = _create(repo)
    for n in range(2, 7):
        _save(tid, n)
        assert _get(tid)["current_version"] == n


def test_it_matches_the_length_of_the_history(repo):
    tid = _create(repo)
    for n in (2, 3):
        _save(tid, n)
    history = _json(oa.list_report_template_versions(None, GM, tid))["versions"]
    assert _get(tid)["current_version"] == len(history)


# ---- what the history says --------------------------------------------------

def test_the_history_is_newest_first_and_says_so_by_number(repo):
    """The browser sorts on these numbers now rather than on position, because
    reversing a list whose order had changed is what put "Current" on the
    oldest version."""
    tid = _create(repo)
    for n in (2, 3, 4):
        _save(tid, n)
    history = _json(oa.list_report_template_versions(None, GM, tid))["versions"]
    assert [v["version"] for v in history] == [4, 3, 2, 1]


def test_every_version_carries_its_own_number(repo):
    """Without it a client has only position to go on, and position is a claim
    about an order it does not control."""
    tid = _create(repo)
    _save(tid, 2)
    for v in _json(oa.list_report_template_versions(None, GM, tid))["versions"]:
        assert isinstance(v["version"], int) and v["version"] >= 1


def test_an_old_version_keeps_the_body_it_had(repo):
    """Append-only. A report that recorded (template, v2) must keep naming a
    body that still exists exactly as it was."""
    tid = _create(repo)
    for n in (2, 3):
        _save(tid, n)
    history = {v["version"]: v for v in
               _json(oa.list_report_template_versions(None, GM, tid))["versions"]}
    assert history[1]["body"]["sections"][0]["purpose"] == "Description number 1."
    assert history[2]["body"]["sections"][0]["purpose"] == "Description number 2."


# ---- the other routes that hand back a template -----------------------------

def test_every_route_reports_the_same_current_version(repo):
    """get, patch and copy all feed the client's selected template. One of them
    disagreeing would show a different version number depending on how you got
    there."""
    tid = _create(repo)
    for n in (2, 3):
        _save(tid, n)

    assert _get(tid)["current_version"] == 3
    patched = _json(oa.update_report_template(None, GM, tid, _event({"name": "Renamed"})))
    assert patched["current_version"] == 3
    assert patched["versions"][0]["version"] == 3

    copied = _json(oa.copy_report_template(None, GM, tid, _event({"name": "My copy"})))
    assert copied["current_version"] == 1, "a copy starts its own history"
    assert copied["versions"][0]["body"]["sections"][0]["purpose"] == "Description number 3.", \
        "and it starts from what it was copied from, not from the original's first version"
