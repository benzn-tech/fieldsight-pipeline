"""org-api: the decision-loop shadow review routes (plan 2026-11-08 site decision loop P0,
Task 6).

  GET  /api/org/decision-shadow?label=&limit=&offset=
  POST /api/org/decision-shadow/{card_id}/label

Pinned here with fakes (no SQL runs -- tests/integration/test_org_api_decision_shadow_sql.py
runs the real queries):
1. SITE_DECISION_LOOP != 'shadow' -> both routes 404 like an unknown route (read per call).
2. platform_admin only: admin, gm and pm get 403 "platform_admin role required".
3. The label body: only `label` and `note` are read; an unknown label is a 400, an unknown
   or malformed card id a 404. The `label` filter is one of the three values or absent.
4. The summary formulas: precision = needed / (needed + not_needed); the per-recipient-day
   p95 (nearest rank) over NZ days with Sunday folded into Monday, fault cards excluded.
"""
import json
from datetime import datetime, timezone

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO = "c0000000-0000-0000-0000-000000000001"
CARD = "33333333-3333-3333-3333-333333333333"
U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"


def _caller(role="platform_admin"):
    return {"id": "u-1", "cognito_sub": "sub-u-1", "company_id": CO, "email": "a@x.nz",
            "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
            "global_role": role, "archived_at": None}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass

    def transaction(self):
        import contextlib
        return contextlib.nullcontext()


def _card_row(**over):
    row = {"card_id": CARD, "company_id": CO, "site_id": "s-1", "rule_id": "checklist.gap",
           "kind": "choose", "purpose": "gap", "urgency": "normal",
           "question": "'Rails' on the Scaffold checklist failed",
           "consequence": "It stays open.",
           "options": [{"id": "remind", "label": "Remind", "recommended": True},
                       {"id": "accept", "label": "Accept with reason", "recommended": False,
                        "reason_required": True}],
           "evidence": [], "recipients": [{"user_id": U1, "role": "pm"}],
           "status": "open", "shadow_label": None, "label_note": None,
           "created_at": datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc),
           "company_name": "Acme", "site_name": "UC PK"}
    row.update(over)
    return row


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    st = type("S", (), {})()
    st.caller = _caller()
    st.labels = []
    st.card = _card_row()
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: st.caller)
    monkeypatch.setattr(org.tenancy_status, "caller_blocked", lambda *a, **k: None)
    monkeypatch.setattr(org.users, "display_names_for_ids",
                        lambda conn, ids: {U1: "Pat Manager"})
    dc = org.decision_cards
    monkeypatch.setattr(dc, "list_shadow", lambda conn, limit, offset, label=None: [st.card])
    monkeypatch.setattr(dc, "shadow_summary_rows", lambda conn: [])
    monkeypatch.setattr(dc, "evidence_sources", lambda conn, ids: [])
    monkeypatch.setattr(dc, "get_shadow",
                        lambda conn, cid: st.card if cid == CARD else None)

    def set_label(conn, card_id, label, note, user_id):
        st.labels.append((card_id, label, note, user_id))
        return st.card if card_id == CARD else None

    monkeypatch.setattr(dc, "set_label", set_label)
    return st


def _call(method, route, body=None, query=None, raw_body=None):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": query,
          "body": raw_body if raw_body is not None else
          (None if body is None else json.dumps(body)),
          "headers": {}, "requestContext": {"authorizer": {"claims": {"sub": "sub-u-1"}}}}
    res = org.lambda_handler(ev, None)
    return res["statusCode"], json.loads(res["body"])


# ---------------------------------------------------------------- auth and flag

@pytest.mark.parametrize("role", ["admin", "gm", "pm"])
def test_non_platform_admin_gets_403(env, role):
    env.caller = _caller(role)
    assert _call("GET", "/decision-shadow") == (403, {"error": "platform_admin role required"})
    status, body = _call("POST", f"/decision-shadow/{CARD}/label", {"label": "needed"})
    assert (status, body) == (403, {"error": "platform_admin role required"})
    assert env.labels == []


@pytest.mark.parametrize("value", ["off", "", "on", None])
def test_flag_off_is_404(env, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SITE_DECISION_LOOP", raising=False)
    else:
        monkeypatch.setenv("SITE_DECISION_LOOP", value)
    assert _call("GET", "/decision-shadow")[0] == 404
    assert _call("POST", f"/decision-shadow/{CARD}/label", {"label": "needed"})[0] == 404
    assert env.labels == []


def test_flag_shadow_serves_the_list(env):
    status, body = _call("GET", "/decision-shadow")
    assert status == 200
    [card] = body["cards"]
    assert card == {
        "card_id": CARD, "company_name": "Acme", "site_name": "UC PK",
        "rule_id": "checklist.gap", "kind": "choose", "purpose": "gap", "urgency": "normal",
        "question": "'Rails' on the Scaffold checklist failed",
        "consequence": "It stays open.", "created_at": "2026-11-09T22:00:00+00:00",
        "shadow_label": None, "label_note": None,
        "options": [{"id": "remind", "label": "Remind", "recommended": True},
                    {"id": "accept", "label": "Accept with reason", "recommended": False,
                     "reason_required": True}],
        "evidence": [],
        "recipients": [{"user_id": U1, "role": "pm", "display_name": "Pat Manager"}]}


def test_unknown_method_or_subroute_is_404(env):
    assert _call("DELETE", "/decision-shadow")[0] == 404
    assert _call("GET", f"/decision-shadow/{CARD}/label")[0] == 404
    assert _call("POST", f"/decision-shadow/{CARD}")[0] == 404


# ---------------------------------------------------------------- label body + filter

def test_label_validation(env):
    route = f"/decision-shadow/{CARD}/label"
    assert _call("POST", route, {"label": "maybe"})[0] == 400
    assert _call("POST", route, {"note": "no label"})[0] == 400
    assert _call("POST", route, raw_body="not json")[0] == 400
    assert _call("POST", route, {"label": "needed", "note": 5})[0] == 400
    assert env.labels == []
    status, body = _call("POST", route, {"label": "wrong_evidence", "note": "quote off",
                                         "status": "done", "labelled_by": "x"})
    assert status == 200 and body["card_id"] == CARD
    # Only label and note are read; the caller id comes from the authorizer, not the body.
    assert env.labels == [(CARD, "wrong_evidence", "quote off", "u-1")]


def test_label_note_is_optional(env):
    status, _ = _call("POST", f"/decision-shadow/{CARD}/label", {"label": "needed"})
    assert status == 200
    assert env.labels == [(CARD, "needed", None, "u-1")]


def test_label_unknown_card_404(env):
    other = "44444444-4444-4444-4444-444444444444"
    assert _call("POST", f"/decision-shadow/{other}/label", {"label": "needed"})[0] == 404
    # A malformed id never reaches SQL.
    n = len(env.labels)
    assert _call("POST", "/decision-shadow/not-a-uuid/label", {"label": "needed"})[0] == 404
    assert len(env.labels) == n


def test_label_filter_values(env, monkeypatch):
    seen = []
    monkeypatch.setattr(org.decision_cards, "list_shadow",
                        lambda conn, limit, offset, label=None:
                        seen.append((limit, offset, label)) or [])
    for value in ("needed", "not_needed", "wrong_evidence"):
        assert _call("GET", "/decision-shadow", query={"label": value})[0] == 200
    assert _call("GET", "/decision-shadow", query={"label": "unlabelled"})[0] == 400
    assert _call("GET", "/decision-shadow", query={"limit": "x"})[0] == 400
    assert _call("GET", "/decision-shadow", query={"offset": "-1"})[0] == 400
    assert _call("GET", "/decision-shadow", query={"limit": "0"})[0] == 400
    assert _call("GET", "/decision-shadow", query={"limit": "500", "offset": "10"})[0] == 200
    assert [s[2] for s in seen[:3]] == ["needed", "not_needed", "wrong_evidence"]
    assert seen[3] == (org.DECISION_SHADOW_MAX_LIMIT, 10, None)


# ---------------------------------------------------------------- summary formulas

def _srow(label=None, kind="choose", recips=(U1,), at=None):
    return {"shadow_label": label, "kind": kind,
            "recipients": [{"user_id": u, "role": "pm"} for u in recips],
            "created_at": at or datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)}


def test_summary_precision_formula():
    rows = [_srow("needed"), _srow("needed"), _srow("not_needed"), _srow("wrong_evidence"),
            _srow(None)]
    s = org.decision_shadow_summary(rows)
    assert (s["labelled"], s["needed"], s["not_needed"], s["wrong_evidence"]) == (4, 2, 1, 1)
    assert s["precision"] == pytest.approx(0.667, abs=1e-3)


def test_summary_empty_is_none():
    s = org.decision_shadow_summary([])
    assert s == {"labelled": 0, "needed": 0, "not_needed": 0, "wrong_evidence": 0,
                 "precision": None, "cards_per_recipient_day_p95": None}
    only_wrong = org.decision_shadow_summary([_srow("wrong_evidence")])
    assert only_wrong["precision"] is None


def test_p95_nearest_rank_sunday_folds_into_monday_fault_excluded():
    # NZDT is UTC+13 in November. 2026-11-08 is a Sunday, 2026-11-09 the Monday.
    sun = datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc)    # Sun 08 12:00 NZ
    mon = datetime(2026, 11, 8, 20, 0, tzinfo=timezone.utc)    # Mon 09 09:00 NZ
    tue = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)    # Tue 10 09:00 NZ
    rows = ([_srow(at=sun)] * 2 + [_srow(at=mon)] * 2 + [_srow(at=tue)]
            + [_srow(kind="fault", at=tue)] * 9
            + [_srow(recips=(U2,), at=tue)])
    s = org.decision_shadow_summary(rows)
    # pairs: (U1, Mon) = 4, (U1, Tue) = 1, (U2, Tue) = 1 -> sorted [1, 1, 4];
    # nearest rank ceil(0.95 * 3) = 3 -> 4.
    assert s["cards_per_recipient_day_p95"] == 4.0
    assert org.decision_shadow_summary([_srow(kind="fault")])[
        "cards_per_recipient_day_p95"] is None


def test_p95_uses_nz_day_not_utc_day():
    # Both are 2026-11-10 in NZ (UTC+13) but different UTC days.
    a = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)      # Tue 10 01:00 NZ
    b = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)      # Tue 10 22:00 NZ
    assert org.decision_shadow_summary([_srow(at=a), _srow(at=b)])[
        "cards_per_recipient_day_p95"] == 2.0
