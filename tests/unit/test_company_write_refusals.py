"""Writes that add people or access to an inactive company are refused
(spec 2026-11-01-company-offboarding §5.6, Task 5).

One helper decides: tenancy_status.write_refusal(conn, caller, company_id, where).
  * flag on, target company inactive, caller not platform_admin -> 'company_inactive'
    and the endpoint answers 409 {"error": "company_inactive"} before any write;
  * target active -> allowed;
  * platform_admin -> allowed (R5: comeback / admin fix-ups);
  * flag off -> allowed exactly as today, one WARNING `COMPANY_GATE_WOULD_REFUSE` line.

The company checked is the TARGET of the write (the member's / site's company, the
external site's company, the company owning the session's site), never the caller's.

Handlers are called directly: through dispatch a non-platform-admin caller of an inactive
company is already refused 403 by the caller gate (Task 3), so the 409 is defence in
depth there. The SQL (companies.status read) is proven against real Postgres in
tests/integration/test_company_write_refusals.py.
"""
import json
import logging

import pytest

import tenancy_status

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

FLAG = "COMPANY_STATUS_ENFORCE"
TARGET = "c-target"          # the company the write lands in
OTHER = "c-other"            # a caller's company that is not the target (B, operator)
SID = "sid" + "a" * 32
SESSION = f"F_2026-10-30_09-00-00_{SID}"


class FakeConn:
    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        class _Cur:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None
        return _Cur()


def _caller(role, company):
    return {"id": "u-c", "cognito_sub": "sub-c", "company_id": company, "email": "c@x.nz",
            "first_name": "C", "last_name": "C", "global_role": role, "archived_at": None,
            "folder_name": "F"}


@pytest.fixture
def inactive(monkeypatch):
    """The set of company ids whose status is 'suspended'. The unit conftest stubs
    company_status to 'active'; this overrides it per test."""
    ids = set()
    monkeypatch.setattr(tenancy_status, "company_status",
                        lambda conn, cid: tenancy_status.SUSPENDED if str(cid) in ids
                        else tenancy_status.ACTIVE)
    monkeypatch.setattr(tenancy_status, "inactive_company_ids", lambda conn: set(ids))
    return ids


# ---- the helper ------------------------------------------------------------------------

def test_helper_flag_on_inactive_target_refuses(inactive, monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    inactive.add(TARGET)
    assert tenancy_status.write_refusal(FakeConn(), _caller("admin", OTHER), TARGET,
                                        where="x") == "company_inactive"


def test_helper_active_target_allows(inactive, monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    inactive.add(OTHER)                       # the CALLER's company is not the question
    assert tenancy_status.write_refusal(FakeConn(), _caller("admin", OTHER), TARGET,
                                        where="x") is None


def test_helper_platform_admin_is_exempt_without_a_read(monkeypatch):
    monkeypatch.setenv(FLAG, "on")

    def boom(conn, cid):
        raise AssertionError("platform_admin must not cost a status read")
    monkeypatch.setattr(tenancy_status, "company_status", boom)
    assert tenancy_status.write_refusal(FakeConn(), _caller("platform_admin", OTHER), TARGET,
                                        where="x") is None


def test_helper_flag_off_allows_and_logs(inactive, monkeypatch, caplog):
    monkeypatch.delenv(FLAG, raising=False)
    inactive.add(TARGET)
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        assert tenancy_status.write_refusal(FakeConn(), _caller("admin", OTHER), TARGET,
                                            where="create_member") is None
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "COMPANY_GATE_WOULD_REFUSE" in line and "create_member" in line and TARGET in line


def test_helper_flag_off_active_target_is_silent(inactive, monkeypatch, caplog):
    monkeypatch.delenv(FLAG, raising=False)
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        assert tenancy_status.write_refusal(FakeConn(), _caller("admin", OTHER), TARGET,
                                            where="x") is None
    assert "COMPANY_GATE_WOULD_REFUSE" not in caplog.text


def test_helper_no_company_allows(inactive, monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    assert tenancy_status.write_refusal(FakeConn(), _caller("admin", OTHER), None,
                                        where="x") is None


# ---- the endpoints ---------------------------------------------------------------------
# Each case wires one handler so that, if it is allowed, it reaches its write; `writes`
# records every write (DB or Cognito). A case returns (response, writes).

def _site(company=TARGET, sid="s-1"):
    return {"id": sid, "company_id": company, "name": "Tower", "archived_at": None,
            "slug": "tower"}


class _Cognito:
    class exceptions:
        class UsernameExistsException(Exception):
            pass

    def __init__(self, writes):
        self.writes = writes

    def admin_create_user(self, **kw):
        self.writes.append(("cognito", kw["Username"]))
        return {"User": {"Attributes": [{"Name": "sub", "Value": "sub-new"}]}}


def case_create_member(mp, caller, writes):
    mp.setattr(org.sites, "get_site", lambda c, sid: _site())
    mp.setattr(org.companies, "get_company_by_id", lambda c, i: {"id": i})
    mp.setattr(org, "cognito", lambda: _Cognito(writes))
    mp.setattr(org.users, "get_user_by_sub", lambda c, sub: None)
    mp.setattr(org.users, "upsert_user",
               lambda c, sub, email, **kw: writes.append(("upsert", kw["company_id"]))
               or {"id": "u-new", "folder_name": "New_P", "cognito_sub": sub})
    mp.setattr(org.memberships, "ensure_membership",
               lambda c, u, s, r: writes.append(("membership", s)) or {"id": "m-1"})
    mp.setattr(org.users, "first_officer_or_member", lambda c, co: None)
    body = {"email": "new@x.nz", "first_name": "New", "last_name": "P",
            "memberships": [{"site_id": "s-1", "role": "worker"}]}
    return org.create_member(FakeConn(), caller, body)


def case_add_external_member(mp, caller, writes):
    mp.setattr(org.sites, "get_site", lambda c, sid: _site(sid="s-9"))
    target = {"id": "u-ext", "cognito_sub": "sub-ext", "company_id": "c-home",
              "email": "ext@home.nz", "first_name": "Eve", "last_name": "Xu"}
    mp.setattr(org.users, "find_live_users_by_email_global",
               lambda c, e, limit=2: writes.append(("lookup", e)) or [target])
    mp.setattr(org.memberships, "get_membership", lambda c, u, s: None)
    mp.setattr(org.memberships, "add_external_membership",
               lambda c, u, s, r: writes.append(("external", s)) or {"id": "m-1"})
    mp.setattr(org.memberships, "record_audit", lambda *a, **k: None)
    mp.setattr(org.companies, "get_company_by_id", lambda c, i: {"id": i, "name": "Co"})
    mp.setattr(org, "_write_external_member_notice", lambda *a, **k: None)
    return org.add_external_member(FakeConn(), caller, "s-9",
                                   {"email": "ext@home.nz", "role": "pm"})


def case_put_member_membership(mp, caller, writes):
    """The ensure_membership revive route (PUT /members/{sub}/memberships/{site})."""
    mp.setattr(org.sites, "get_site", lambda c, sid: _site())
    mp.setattr(org.users, "get_user_by_sub",
               lambda c, sub: {"id": "u-t", "cognito_sub": sub, "company_id": TARGET,
                               "archived_at": None})
    mp.setattr(org.memberships, "get_membership", lambda c, u, s: None)
    mp.setattr(org.memberships, "ensure_membership",
               lambda c, u, s, r: writes.append(("membership", s)) or {"id": "m-1"})
    return org.put_member_membership(FakeConn(), caller, "sub-t", "s-1", {"role": "worker"})


def case_member_unarchive(mp, caller, writes):
    mp.setattr(org.users, "unarchive_user",
               lambda c, sub, co: writes.append(("unarchive_user", co)) or {"id": "u-t"})
    return org.archive_member_endpoint(FakeConn(), caller, "sub-t", "unarchive")


def case_site_unarchive(mp, caller, writes):
    mp.setattr(org.sites, "unarchive_site",
               lambda c, sid, co: writes.append(("unarchive_site", co)) or _site())
    return org.archive_site_endpoint(FakeConn(), caller, "s-1", "unarchive")


def case_site_create(mp, caller, writes):
    mp.setattr(org.companies, "get_company_by_id", lambda c, i: {"id": i})
    mp.setattr(org, "_unique_site_slug", lambda c, co, s: s)
    mp.setattr(org.sites, "create_site",
               lambda c, co, name, **kw: writes.append(("create_site", co)) or _site(co))
    mp.setattr(org, "_publish_site_coords", lambda row: None)
    body = {"name": "New site"}
    if caller["global_role"] == "platform_admin":
        body["target_company_id"] = TARGET
    return org.create_org_site(FakeConn(), caller, body)


def case_site_patch(mp, caller, writes):
    mp.setattr(org.sites, "get_site", lambda c, sid: _site())
    mp.setattr(org.sites, "update_site",
               lambda c, sid, co, **kw: writes.append(("update_site", co)) or _site())
    mp.setattr(org, "_publish_site_coords", lambda row: None)
    return org.patch_org_site(FakeConn(), caller, "s-1", {"name": "Renamed"})


CASES = {
    "create_member": case_create_member,
    "add_external_member": case_add_external_member,
    "put_member_membership": case_put_member_membership,
    "member_unarchive": case_member_unarchive,
    "site_unarchive": case_site_unarchive,
    "site_create": case_site_create,
    "site_patch": case_site_patch,
}


def _admin_for(name):
    # The company's own admin: through dispatch the caller gate would already refuse them
    # (their company is the target); called directly, the write refusal must hold alone.
    return _caller("admin", TARGET)


@pytest.mark.parametrize("name", sorted(CASES))
def test_flag_on_inactive_target_is_refused_before_any_write(name, inactive, monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    inactive.add(TARGET)
    writes = []
    res = CASES[name](monkeypatch, _admin_for(name), writes)
    assert res["statusCode"] == 409, (name, res)
    assert json.loads(res["body"]) == {"error": "company_inactive"}
    assert writes == [], (name, writes)


@pytest.mark.parametrize("name", sorted(CASES))
def test_flag_on_active_target_is_allowed(name, inactive, monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    inactive.add(OTHER)
    writes = []
    res = CASES[name](monkeypatch, _admin_for(name), writes)
    assert res["statusCode"] < 300, (name, res)
    assert writes, name


# The two archive routes refuse platform_admin today (resolve_scope is not ALL for it, and
# they pin to the caller's own company), so there is nothing for it to be exempt from there.
_PA_ROUTES = sorted(set(CASES) - {"member_unarchive", "site_unarchive"})


@pytest.mark.parametrize("name", _PA_ROUTES)
def test_platform_admin_may_write_into_an_inactive_company(name, inactive, monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    inactive.add(TARGET)
    writes = []
    res = CASES[name](monkeypatch, _caller("platform_admin", OTHER), writes)
    assert res["statusCode"] < 300, (name, res)
    assert writes, name


@pytest.mark.parametrize("name", sorted(CASES))
def test_flag_off_is_allowed_and_logged(name, inactive, monkeypatch, caplog):
    monkeypatch.delenv(FLAG, raising=False)
    inactive.add(TARGET)
    writes = []
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        res = CASES[name](monkeypatch, _admin_for(name), writes)
    assert res["statusCode"] < 300, (name, res)
    assert writes, name
    lines = [r.getMessage() for r in caplog.records
             if "COMPANY_GATE_WOULD_REFUSE" in r.getMessage()]
    assert len(lines) == 1, (name, lines)
    assert TARGET in lines[0]


def test_member_archive_is_never_refused(inactive, monkeypatch):
    """Removing access from an inactive company is not a write INTO it."""
    monkeypatch.setenv(FLAG, "on")
    inactive.add(TARGET)
    monkeypatch.setattr(org.users, "archive_user", lambda c, sub, co: {"id": "u-t"})
    res = org.archive_member_endpoint(FakeConn(), _caller("admin", TARGET), "sub-t", "archive")
    assert res["statusCode"] == 200


def test_membership_delete_is_never_refused(inactive, monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    inactive.add(TARGET)
    monkeypatch.setattr(org.sites, "get_site", lambda c, sid: _site())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda c, sub: {"id": "u-t", "cognito_sub": sub, "company_id": TARGET})
    monkeypatch.setattr(org.memberships, "get_membership", lambda c, u, s: None)
    monkeypatch.setattr(org.memberships, "archive_membership",
                        lambda c, u, s: {"id": "m-1", "role": "worker"})
    res = org.delete_member_membership(FakeConn(), _caller("admin", TARGET), "sub-t", "s-1")
    assert res["statusCode"] == 200


# ---- writes on a session of an inactive company's site --------------------------------

def _session_on(mp, company):
    mp.setattr(org.recordings, "session_site_companies",
               lambda conn, folder, date, sb: [("s-1", company)])


def test_recorder_regenerate_on_an_inactive_site_is_refused(inactive, monkeypatch):
    """regenerate_session lets the recorder act on their own session wherever it lies
    (allow_recorder); on a site of an inactive company that is a write into it."""
    monkeypatch.setenv(FLAG, "on")
    inactive.add(TARGET)
    _session_on(monkeypatch, TARGET)
    res = org._session_site_company_guard(FakeConn(), _caller("worker", OTHER), "F",
                                          "2026-10-30", SID, "re-extraction",
                                          allow_recorder=True)
    assert res["statusCode"] == 409
    assert json.loads(res["body"]) == {"error": "company_inactive"}


def test_recorder_regenerate_allowed_when_active_pa_or_flag_off(inactive, monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "on")
    _session_on(monkeypatch, TARGET)
    guard = lambda caller: org._session_site_company_guard(  # noqa: E731
        FakeConn(), caller, "F", "2026-10-30", SID, "re-extraction", allow_recorder=True)
    assert guard(_caller("worker", OTHER)) is None                 # A active
    inactive.add(TARGET)
    assert guard(_caller("platform_admin", OTHER)) is None         # R5
    monkeypatch.delenv(FLAG)
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        assert guard(_caller("worker", OTHER)) is None             # shadow
    assert "COMPANY_GATE_WOULD_REFUSE" in caplog.text and TARGET in caplog.text


def test_recorder_guard_without_allow_recorder_is_unchanged(inactive, monkeypatch):
    """The F5 403 for another company's session stays exactly as it was."""
    monkeypatch.setenv(FLAG, "on")
    inactive.add(TARGET)
    _session_on(monkeypatch, TARGET)
    res = org._session_site_company_guard(FakeConn(), _caller("worker", OTHER), "F",
                                          "2026-10-30", SID, "naming a speaker")
    assert res["statusCode"] == 403


@pytest.fixture
def unname(monkeypatch):
    calls = []
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org.topics, "folders_for_session_base", lambda c, co, sb: ["F"])
    monkeypatch.setattr(org.scope, "visible_scope", lambda c, caller: {"self_folder": "F"})
    monkeypatch.setattr(org.voiceprints, "unname",
                        lambda c, co, session_base, display_name, rejected_by=None:
                        calls.append(session_base) or 2)
    _session_on(monkeypatch, TARGET)

    def run(role="worker"):
        return org.unname_speaker(FakeConn(), _caller(role, OTHER), SESSION,
                                  {"queryStringParameters": {"name": "Ben"}})
    run.calls = calls
    return run


@pytest.mark.parametrize("role", ["worker", "admin"])
def test_unname_on_an_inactive_companys_session_is_refused(unname, inactive, monkeypatch, role):
    monkeypatch.setenv(FLAG, "on")
    inactive.add(TARGET)
    res = unname(role)
    assert res["statusCode"] == 409 and json.loads(res["body"]) == {"error": "company_inactive"}
    assert unname.calls == []


def test_unname_allowed_when_active_pa_or_flag_off(unname, inactive, monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "on")
    assert unname()["statusCode"] == 200
    inactive.add(TARGET)
    assert unname("platform_admin")["statusCode"] == 200
    monkeypatch.delenv(FLAG)
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        assert unname()["statusCode"] == 200
    assert "COMPANY_GATE_WOULD_REFUSE" in caplog.text
    assert len(unname.calls) == 3


def test_session_check_costs_no_session_read_while_nobody_is_inactive(inactive, monkeypatch):
    """The normal case: no company inactive -> neither the folder lookup nor the session's
    site read runs (flag on or off), so the role arm still never pays for ownership."""
    monkeypatch.setenv(FLAG, "on")

    def boom(*a, **k):
        raise AssertionError("session lookup ran while no company is inactive")
    monkeypatch.setattr(org.recordings, "session_site_companies", boom)
    assert org._session_write_refused(FakeConn(), _caller("admin", OTHER), boom,
                                      None, SID, "x") is None
