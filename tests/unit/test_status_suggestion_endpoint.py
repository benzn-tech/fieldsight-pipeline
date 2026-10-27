"""POST /api/org/action-items/{id}/status-suggestion (spec 2026-10-27 §4.5), and the read field.

Accept = the normal PATCH (audited, actor = the person) + mark accepted, one transaction.
Dismiss = mark dismissed, no status change. ACL is PATCH's, checked before anything about the
suggestion is revealed.
"""
from datetime import datetime, timezone

import lambda_org_api as org
from tests.unit.test_lambda_org_api import (  # noqa: F401  (fixture)
    AITEM, CALLER, _wire_item, body_of, make_event, wired)

STABLE = "5a5a5a5a-5a5a-5a5a-5a5a-5a5a5a5a5a5a"
SID = "9b9b9b9b-9b9b-9b9b-9b9b-9b9b9b9b9b9b"


def _wire(wired, suggestion=None, resolved=True, item=None):
    seen = _wire_item(wired, item=dict(item or AITEM, stable_id=STABLE))
    s = {"id": SID, "stable_id": STABLE, "suggested_status": "done", "state": "pending"}
    if suggestion is not None:
        s.update(suggestion)
    wired.setattr(org.status_suggestions_repo, "get_suggestion",
                  lambda conn, i: dict(s) if str(i) == SID else None)
    seen["resolved"] = []

    def _resolve(conn, i, state, uid):
        seen["resolved"].append((str(i), state, uid))
        return {"id": i, "state": state} if resolved else None

    wired.setattr(org.status_suggestions_repo, "resolve_suggestion", _resolve)
    return seen


def _post(body):
    return org.lambda_handler(make_event(
        "POST", "/api/org/action-items/a-1/status-suggestion", body=body), None)


def test_accept_patches_the_status_with_an_audit_row_and_marks_it(wired):
    seen = _wire(wired)
    res = _post({"suggestion_id": SID, "decision": "accept"})
    assert res["statusCode"] == 200, body_of(res)
    assert seen["fields"] == {"status": "done"}
    assert [a[3:6] for a in seen["audits"]] == [("status", "open", "done")]
    assert seen["resolved"] == [(SID, "accepted", CALLER["id"])]
    assert body_of(res)["state"] == "accepted"


def test_dismiss_changes_no_status(wired):
    seen = _wire(wired)
    res = _post({"suggestion_id": SID, "decision": "dismiss"})
    assert res["statusCode"] == 200
    assert "fields" not in seen and seen["audits"] == []
    assert seen["resolved"] == [(SID, "dismissed", CALLER["id"])]


def test_acl_runs_before_the_suggestion_is_looked_at(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    seen = _wire(wired, item=dict(AITEM, responsible="Someone Else"))
    looked = []
    wired.setattr(org.status_suggestions_repo, "get_suggestion",
                  lambda conn, i: looked.append(i))
    res = _post({"suggestion_id": SID, "decision": "accept"})
    assert res["statusCode"] == 403
    assert looked == [] and "fields" not in seen


def test_a_suggestion_for_another_item_is_404(wired):
    seen = _wire(wired, suggestion={"stable_id": "00000000-0000-0000-0000-000000000000"})
    assert _post({"suggestion_id": SID, "decision": "accept"})["statusCode"] == 404
    assert "fields" not in seen


def test_a_decided_suggestion_is_404(wired):
    _wire(wired, suggestion={"state": "dismissed"})
    assert _post({"suggestion_id": SID, "decision": "accept"})["statusCode"] == 404


def test_unknown_id_and_bad_decision(wired):
    _wire(wired)
    assert _post({"suggestion_id": "nope", "decision": "accept"})["statusCode"] == 404
    assert _post({"suggestion_id": SID, "decision": "maybe"})["statusCode"] == 400


def test_a_lost_race_is_409_not_a_silent_success(wired):
    _wire(wired, resolved=False)
    assert _post({"suggestion_id": SID, "decision": "dismiss"})["statusCode"] == 409


# ------------------------------------------------------------------ read field

T0 = datetime(2026, 10, 27, 10, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 27, 11, tzinfo=timezone.utc)


def _s(i, status, at):
    return {"id": i, "suggested_status": status, "quote": "q", "segment_key": "k",
            "offset_sec": 1.0, "created_at": at}


def test_the_newest_pending_suggestion_that_would_change_something_is_served():
    item = {"id": "a1", "status": "open", "collapsed_ids": ["a2"]}
    sugg = {"a1": [_s("s1", "in_progress", T0)], "a2": [_s("s2", "done", T1)]}
    out = org._status_suggestion_field(item, sugg)
    assert out["status_suggestion"]["id"] == "s2"      # from the collapsed row, and newer
    assert out["status_suggestion"]["status"] == "done"


def test_a_suggestion_equal_to_the_current_status_is_not_served():
    item = {"id": "a1", "status": "done"}
    assert org._status_suggestion_field(item, {"a1": [_s("s1", "done", T0)]}) == {}
    assert org._status_suggestion_field({"id": "a1", "status": "open"}, {}) == {}


def test_no_connection_means_no_lookup():
    assert org._pending_status_suggestions(None, [{"action_items": [{"id": "a1"}]}]) == {}
