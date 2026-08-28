"""Unit: the one endpoint that decides on what grounds a company may hold voices.

The column shipped with nothing able to write it — a read path, a fallback for the absent
value, and no way to make it present. This is the write half, and these tests are mostly
about authorisation, because this repository has a note titled "the endpoint I wrote had zero
authorisation" and the way that happened was writing the happy path first and meaning to come
back.

What the endpoint decides is not a display preference. `voiceprint_consent_basis` is what
`speaker_corrections` consults before creating biometric data at all: NULL means enrolment
falls back to needing the subject's own id, and a value means a correction is enough. So the
tests below are the guard rails on that, in the order they would be got wrong.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api")


class _Cur:
    def __init__(self, row):
        self.row, self.sql, self.params = row, [], []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.params.append(params)
        return self

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row=None):
        self.cur = _Cur(row)

    def cursor(self, row_factory=None):
        return self.cur


CO = "6a23c57c-5fa3-4ef4-a93c-88e9543272fc"
OTHER_CO = "99999999-9999-9999-9999-999999999999"


def _caller(role="platform_admin", company=CO):
    return {"id": "u-1", "company_id": company, "global_role": role,
            "cognito_sub": "sub-1"}


def _event(body):
    return {"body": json.dumps(body)}


def _row(basis):
    return {"id": CO, "name": "Co", "voiceprint_consent_basis": basis}


def test_the_feature_switch_hides_it_entirely(monkeypatch):
    """`off` is the rollback, and a rollback that leaves an endpoint answering is not one.

    The connection returns a row on purpose. With `_Conn()` — no row — the "company not
    found" branch returns 404 too, so the assertion passed whether or not the mode gate was
    there: removing the gate left this green. A guard hanging on the same value as the thing
    it guards against is the shape this session has removed several times, and it took a
    mutation run to see it here.

    So the connection would succeed, and the ONLY reason for a 404 is the gate. The write
    reaching the database at all is asserted as well, because "returned 404" and "returned
    404 after updating the row" are not the same outcome.
    """
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    conn = _Conn(_row("notice"))
    res = org.company_voiceprint_basis(conn, _caller(), _event({"basis": "notice"}))
    assert res["statusCode"] == 404
    assert not conn.cur.sql, "the endpoint touched the database before refusing"


def test_only_a_platform_admin_may_decide_it(monkeypatch):
    """Deliberately narrower than the correction roles. Naming a speaker is an everyday act
    by whoever is on site; deciding the legal grounds for holding voices is not, and sharing
    one permission between them would let the first imply the second."""
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    for role in ("admin", "gm", "pm", "site_manager", "worker", None):
        res = org.company_voiceprint_basis(_Conn(), _caller(role=role),
                                           _event({"basis": "notice"}))
        assert res["statusCode"] == 403, role


def test_the_company_comes_from_the_caller_never_from_the_body(monkeypatch):
    """A body-supplied company id would be a button that decides on what grounds ANOTHER
    tenant may hold biometric data. The same rule as `withdraw_voiceprint` beside it, for the
    same reason."""
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    conn = _Conn(_row("notice"))
    org.company_voiceprint_basis(
        conn, _caller(), _event({"basis": "notice", "company_id": OTHER_CO}))
    written = [p for p in conn.cur.params if p]
    assert any(CO in [str(x) for x in p] for p in written), "the caller's company was not used"
    assert not any(OTHER_CO in [str(x) for x in p] for p in written), (
        "a company id from the request body reached the UPDATE")


def test_an_unknown_basis_is_refused_rather_than_stored(monkeypatch):
    """A typo outside the closed list would read as "settled" to the correction endpoint
    while meaning nothing — a guard passing on a value nobody intended."""
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    res = org.company_voiceprint_basis(_Conn(_row(None)), _caller(),
                                       _event({"basis": "sure why not"}))
    assert res["statusCode"] == 400


def test_a_basis_can_be_cleared_which_returns_the_strict_rule(monkeypatch):
    """Withdrawing the basis has to be as available as setting it. `null` puts enrolment back
    to needing the subject's own id, which is the state every company is in today."""
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    conn = _Conn(_row(None))
    res = org.company_voiceprint_basis(conn, _caller(), _event({"basis": None}))
    assert res["statusCode"] == 200
    assert json.loads(res["body"])["basis"] is None


def test_it_reports_what_was_stored_not_what_was_asked_for(monkeypatch):
    """Read back from the RETURNING row. Echoing the request would say "notice" even if the
    UPDATE matched nothing, and "we did it" and "there was no such company" would look the
    same to whoever asked."""
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    res = org.company_voiceprint_basis(_Conn(None), _caller(), _event({"basis": "notice"}))
    assert res["statusCode"] == 404


def test_the_route_is_wired(monkeypatch):
    """A handler nothing dispatches to is the shape this session has removed six times."""
    src = open("src/lambda_org_api.py", encoding="utf-8").read()
    assert '"/company/voiceprint-basis" and method == "PUT"' in src
    assert "return company_voiceprint_basis(conn, caller, event)" in src
