"""
Handler-level tests for GET /programme/tasks and the delay-flag routes —
Task 3 of the programme time-window plan. Spec §7, §10.

Two things here are worth more than the rest.

`assignee=me` resolves to the caller's folder_name. A caller without one has
no attributable programme work, and the correct answer is an empty list. If
that resolved to None instead, it would fall through to "no restriction" and
hand them the entire programme — the same empty-means-unrestricted inversion
this codebase has shipped before.

The 400-day cap is what stops the window endpoint from becoming the
whole-programme fetch it exists to replace.
"""
import pytest

import lambda_org_api as org

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
PROG_ID = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"

CALLER_SM = {"id": "u-sm", "global_role": "site_manager",
             "folder_name": "Sam_SM", "company_id": "c1"}
CALLER_PM = {"id": "u-pm", "global_role": "pm",
             "folder_name": "Pat_PM", "company_id": "c1"}
CALLER_NO_FOLDER = {"id": "u-x", "global_role": "site_manager",
                    "folder_name": None, "company_id": "c1"}

PROGRAMME = {"id": PROG_ID, "site_id": SITE_ID, "baseline_version": 1,
             "current_version": 3}


@pytest.fixture
def wired_window(monkeypatch):
    """Site ACL and the programme lookup stubbed; the window query is
    recorded rather than run so the handler's own decisions are what get
    asserted."""
    seen = {}
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org.programme_tasks, "get_primary_programme",
                        lambda conn, site_id: PROGRAMME)
    monkeypatch.setattr(org.programme_tasks, "list_assignees",
                        lambda conn, ids: {})

    def fake_window(conn, programme_id, *, date_from, date_to, assignee=None):
        seen.update(programme_id=programme_id, date_from=date_from,
                    date_to=date_to, assignee=assignee, called=True)
        return []

    monkeypatch.setattr(org.programme_window, "tasks_in_window", fake_window)
    return seen


def _event(**params):
    return {"queryStringParameters": dict(params)}


def _call(caller, **params):
    return org.list_programme_tasks(None, caller, _event(site=SITE_ID, **params))


def test_a_window_read_passes_its_bounds_through(wired_window):
    res = _call(CALLER_PM, **{"from": "2026-04-01", "to": "2026-05-31"})
    assert res["statusCode"] == 200
    assert wired_window["date_from"] == "2026-04-01"
    assert wired_window["date_to"] == "2026-05-31"


def test_assignee_me_resolves_to_the_callers_folder_name(wired_window):
    _call(CALLER_SM, **{"from": "2026-04-01", "to": "2026-05-31", "assignee": "me"})
    assert wired_window["assignee"] == "Sam_SM"


def test_a_caller_with_no_folder_identity_gets_nothing_not_everything(wired_window):
    """The inversion that would matter: resolving 'me' to None falls through
    to 'no restriction' and returns the whole programme to someone who owns
    none of it."""
    res = _call(CALLER_NO_FOLDER,
                **{"from": "2026-04-01", "to": "2026-05-31", "assignee": "me"})
    assert res["statusCode"] == 200
    assert "called" not in wired_window, \
        "the query must not run unfiltered for a caller with no identity"


def test_no_assignee_param_means_no_restriction(wired_window):
    _call(CALLER_PM, **{"from": "2026-04-01", "to": "2026-05-31"})
    assert wired_window["assignee"] is None


def test_an_explicit_assignee_is_passed_through(wired_window):
    _call(CALLER_PM, **{"from": "2026-04-01", "to": "2026-05-31",
                        "assignee": "Someone_Else"})
    assert wired_window["assignee"] == "Someone_Else"


def test_missing_bounds_are_rejected(wired_window):
    assert _call(CALLER_PM, **{"from": "2026-04-01"})["statusCode"] == 400
    assert _call(CALLER_PM, **{"to": "2026-05-31"})["statusCode"] == 400
    assert _call(CALLER_PM)["statusCode"] == 400


def test_malformed_dates_are_rejected_as_400_not_500(wired_window):
    res = _call(CALLER_PM, **{"from": "yesterday", "to": "2026-05-31"})
    assert res["statusCode"] == 400


def test_a_reversed_window_is_rejected(wired_window):
    res = _call(CALLER_PM, **{"from": "2026-05-31", "to": "2026-04-01"})
    assert res["statusCode"] == 400


def test_an_oversized_window_is_rejected(wired_window):
    """Without the cap this endpoint becomes the whole-programme fetch it
    exists to replace."""
    res = _call(CALLER_PM, **{"from": "2020-01-01", "to": "2026-05-31"})
    assert res["statusCode"] == 400
    assert "called" not in wired_window


def test_the_maximum_window_is_still_allowed(wired_window):
    res = _call(CALLER_PM, **{"from": "2026-01-01", "to": "2027-02-05"})  # 400 days
    assert res["statusCode"] == 200


def test_a_site_with_no_programme_returns_an_empty_list(monkeypatch, wired_window):
    monkeypatch.setattr(org.programme_tasks, "get_primary_programme",
                        lambda conn, site_id: None)
    res = _call(CALLER_PM, **{"from": "2026-04-01", "to": "2026-05-31"})
    assert res["statusCode"] == 200


# ---- delay flags ---------------------------------------------------------

@pytest.fixture
def wired_flags(monkeypatch):
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org.programme_tasks, "get_task",
                        lambda conn, tid: {"id": tid, "programme_id": PROG_ID})
    monkeypatch.setattr(org.programme_tasks, "get_primary_programme_by_id",
                        lambda conn, pid: PROGRAMME)
    monkeypatch.setattr(org.programme_delay_flags, "raise_flag",
                        lambda conn, **kw: dict(kw, id="f1"))
    monkeypatch.setattr(org.programme_delay_flags, "get",
                        lambda conn, fid: {"id": fid, "task_id": "t1"})
    monkeypatch.setattr(org.programme_delay_flags, "set_state",
                        lambda conn, fid, state: {"id": fid, "state": state})


def test_a_site_manager_may_raise_a_delay_flag(wired_flags):
    """This is where the 403 on editing a contract date sends them. Gating it
    to managers would leave that refusal pointing nowhere."""
    res = org.raise_delay_flag(None, CALLER_SM, "t1",
                               {"reason": "pump unavailable"})
    assert res["statusCode"] == 200


def test_a_worker_may_not_raise_a_delay_flag(wired_flags):
    worker = {"id": "u-w", "global_role": "worker", "folder_name": "Wes_W"}
    res = org.raise_delay_flag(None, worker, "t1", {"reason": "x"})
    assert res["statusCode"] == 403


def test_a_delay_flag_without_a_reason_is_a_400(wired_flags, monkeypatch):
    def boom(conn, **kw):
        raise ValueError("a delay flag needs a reason")
    monkeypatch.setattr(org.programme_delay_flags, "raise_flag", boom)
    res = org.raise_delay_flag(None, CALLER_SM, "t1", {"reason": "  "})
    assert res["statusCode"] == 400


def test_a_site_manager_may_not_acknowledge_a_flag(wired_flags):
    """Letting the raiser close their own flag would let the signal die
    before reaching anyone who can act on it."""
    res = org.acknowledge_delay_flag(None, CALLER_SM, "f1")
    assert res["statusCode"] == 403


def test_a_manager_may_acknowledge_a_flag(wired_flags):
    res = org.acknowledge_delay_flag(None, CALLER_PM, "f1")
    assert res["statusCode"] == 200


def test_resolving_is_a_distinct_state_from_acknowledging(wired_flags):
    ack = org.acknowledge_delay_flag(None, CALLER_PM, "f1")
    res = org.acknowledge_delay_flag(None, CALLER_PM, "f1", state="resolved")
    import json
    assert json.loads(ack["body"])["delay_flag"]["state"] == "acknowledged"
    assert json.loads(res["body"])["delay_flag"]["state"] == "resolved"
