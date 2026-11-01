"""Unit: tenancy_status.enforce_enabled and caller_blocked (flag x state x archived x admin).

company_status is stubbed here; its SQL runs in tests/integration/test_tenancy_status.py.
"""
import logging

import pytest

import tenancy_status

TOKEN = "COMPANY_GATE_WOULD_BLOCK"


def _caller(role="worker", archived=None, company_id="co-1"):
    return {"id": "u-1", "cognito_sub": "sub-1", "company_id": company_id,
            "global_role": role, "archived_at": archived}


@pytest.mark.parametrize("value,expected", [
    ("on", True), ("ON", True), ("true", True), ("True", True), ("1", True),
    (" on ", False), ("off", False), ("false", False), ("0", False), ("", False),
    ("yes", False), ("enabled", False), (None, False),
])
def test_enforce_enabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    else:
        monkeypatch.setenv("COMPANY_STATUS_ENFORCE", value)
    assert tenancy_status.enforce_enabled() is expected


@pytest.fixture
def status_stub(monkeypatch):
    calls = []
    state = {"value": "active"}

    def fake(conn, company_id):
        calls.append(company_id)
        return state["value"]

    monkeypatch.setattr(tenancy_status, "company_status", fake)
    return state, calls


def _shadow_lines(caplog):
    return [r for r in caplog.records if TOKEN in r.getMessage()]


@pytest.mark.parametrize("flag", ["on", "off"])
@pytest.mark.parametrize("status", ["suspended", "exited"])
def test_inactive_company(monkeypatch, caplog, status_stub, flag, status):
    state, _calls = status_stub
    state["value"] = status
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", flag)
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        got = tenancy_status.caller_blocked(object(), _caller(), where="org-api")
    if flag == "on":
        assert got == "company_inactive"
        assert _shadow_lines(caplog) == []
    else:
        assert got is None
        lines = _shadow_lines(caplog)
        assert len(lines) == 1
        assert lines[0].levelno == logging.WARNING
        msg = lines[0].getMessage()
        for part in ("company_inactive", "u-1", "sub-1", "co-1", "org-api"):
            assert part in msg


@pytest.mark.parametrize("flag", ["on", "off"])
@pytest.mark.parametrize("status", ["active", "suspended"])
def test_archived_caller(monkeypatch, caplog, status_stub, flag, status):
    state, calls = status_stub
    state["value"] = status
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", flag)
    with caplog.at_level(logging.WARNING, logger="tenancy_status"):
        got = tenancy_status.caller_blocked(object(), _caller(archived="2026-10-01"),
                                            where="rag-search")
    # Archived wins over the company read: no DB round trip is needed to refuse.
    assert calls == []
    if flag == "on":
        assert got == "account_archived"
        assert _shadow_lines(caplog) == []
    else:
        assert got is None
        lines = _shadow_lines(caplog)
        assert len(lines) == 1
        assert "account_archived" in lines[0].getMessage()
        assert "rag-search" in lines[0].getMessage()


@pytest.mark.parametrize("flag", ["on", "off"])
def test_active_caller_not_blocked_and_silent(monkeypatch, caplog, status_stub, flag):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", flag)
    with caplog.at_level(logging.DEBUG, logger="tenancy_status"):
        assert tenancy_status.caller_blocked(object(), _caller()) is None
    assert _shadow_lines(caplog) == []


@pytest.mark.parametrize("flag", ["on", "off"])
@pytest.mark.parametrize("archived", [None, "2026-10-01"])
@pytest.mark.parametrize("status", ["active", "suspended", "exited"])
def test_platform_admin_exempt(monkeypatch, caplog, status_stub, flag, archived, status):
    state, calls = status_stub
    state["value"] = status
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", flag)
    with caplog.at_level(logging.DEBUG, logger="tenancy_status"):
        got = tenancy_status.caller_blocked(object(), _caller(role="platform_admin",
                                                              archived=archived))
    assert got is None
    assert calls == []
    assert _shadow_lines(caplog) == []


def test_none_caller_is_not_blocked(status_stub):
    _state, calls = status_stub
    assert tenancy_status.caller_blocked(object(), None) is None
    assert calls == []


def test_flag_is_read_per_call(monkeypatch, status_stub):
    state, _calls = status_stub
    state["value"] = "exited"
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    assert tenancy_status.caller_blocked(object(), _caller()) is None
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    assert tenancy_status.caller_blocked(object(), _caller()) == "company_inactive"


# ---- Task 9: suppress_decision -------------------------------------------------------

@pytest.mark.parametrize("status", ["suspended", "exited"])
def test_suppress_decision_inactive(monkeypatch, caplog, status):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    caplog.set_level(logging.WARNING)
    assert tenancy_status.suppress_decision("co-9", status, where="w", site_id="s-1") == \
        "company_inactive"
    assert not [r for r in caplog.records if "WOULD_SUPPRESS" in r.getMessage()]
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    assert tenancy_status.suppress_decision("co-9", status, where="w", site_id="s-1") is None
    lines = [r for r in caplog.records if "COMPANY_GATE_WOULD_SUPPRESS" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    msg = lines[0].getMessage()
    assert "where=w" in msg and "company=co-9" in msg and "site=s-1" in msg


@pytest.mark.parametrize("status", ["active", None])
def test_suppress_decision_active_or_unknown_is_silent(monkeypatch, caplog, status):
    caplog.set_level(logging.WARNING)
    for flag in ("on", "off"):
        monkeypatch.setenv("COMPANY_STATUS_ENFORCE", flag)
        assert tenancy_status.suppress_decision("co-9", status, where="w") is None
    assert not caplog.records
