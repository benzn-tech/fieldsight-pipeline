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
    # Every card resolves to a live recording unless a test says otherwise.
    st.sources = None            # None -> each asked card resolves to F/2026-11-09/sidaaaa
    st.overdue = {}              # card_id -> (deadline text, visible)
    st.mirror = {}               # (folder, date) -> set of deleted session ids
    st.prefixes = {}             # (folder, date) -> [tombstone prefix]
    st.calls = []

    def evidence_sources(conn, ids):
        st.calls.append(("evidence_sources", len(ids)))
        if st.sources is not None:
            return [x for x in st.sources if x["card_id"] in {str(i) for i in ids}]
        return [{"card_id": str(i), "user_folder": "F", "report_date": "2026-11-09",
                 "session": "sidaaaa", "walk_key": None, "walk_parts": []} for i in ids]

    def overdue_item_states(conn, ids):
        st.calls.append(("overdue_item_states", len(ids)))
        out = []
        for i in ids:
            deadline, visible = st.overdue.get(str(i), ("2026-11-01", True))
            out.append({"card_id": str(i), "deadline": deadline, "visible": visible})
        return out

    def mirror(s3, bucket, folder, date):
        st.calls.append(("mirror", folder, date))
        return st.mirror.get((folder, date), set())

    def prefixes(conn, folder, date):
        st.calls.append(("prefixes", folder, date))
        return st.prefixes.get((folder, date), [])

    monkeypatch.setattr(dc, "evidence_sources", evidence_sources)
    monkeypatch.setattr(dc, "overdue_item_states", overdue_item_states)
    monkeypatch.setattr(org.deletion_mirror, "deleted_sessions_strict", mirror)
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes", prefixes)
    monkeypatch.setattr(org, "s3", lambda: None)
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
                 "precision": None, "cards_per_recipient_day_p95": None, "since": None}
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


def test_since_limits_labels_and_p95_to_nz_days_on_or_after_it():
    from datetime import date
    # NZDT = UTC+13. 2026-11-10 11:30Z is Wed 11 Nov 00:30 NZ; 10:30Z is Tue 10 Nov 23:30 NZ.
    before = datetime(2026, 11, 10, 10, 30, tzinfo=timezone.utc)
    after = datetime(2026, 11, 10, 11, 30, tzinfo=timezone.utc)
    rows = ([_srow("needed", at=before) for _ in range(5)]
            + [_srow("not_needed", at=after), _srow("needed", at=after), _srow("needed")])
    rows[-1]["created_at"] = None
    full = org.decision_shadow_summary(rows)
    assert (full["labelled"], full["cards_per_recipient_day_p95"], full["since"]) == (
        8, 5.0, None)
    s = org.decision_shadow_summary(rows, since=date(2026, 11, 11))
    assert (s["labelled"], s["needed"], s["not_needed"]) == (2, 1, 1)
    assert s["precision"] == 0.5
    assert s["cards_per_recipient_day_p95"] == 2.0
    assert s["since"] == "2026-11-11"
    assert org.decision_shadow_summary(rows, since=date(2026, 11, 12))[
        "cards_per_recipient_day_p95"] is None


def test_since_query_param_valid_invalid(env, monkeypatch):
    rows = [_srow("needed", at=datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc))]
    monkeypatch.setattr(org.decision_cards, "shadow_summary_rows", lambda conn: rows)
    status, body = _call("GET", "/decision-shadow", query={"since": "2026-11-11"})
    assert status == 200
    assert body["summary"]["since"] == "2026-11-11" and body["summary"]["labelled"] == 0
    assert len(body["cards"]) == 1                       # the card list is unchanged
    status, body = _call("GET", "/decision-shadow", query={"since": ""})
    assert status == 200 and body["summary"]["since"] is None
    assert body["summary"]["labelled"] == 1
    for bad in ("2026-13-01", "11/11/2026", "2026-11-1", "2026-11-11T00:00", "yesterday"):
        status, body = _call("GET", "/decision-shadow", query={"since": bad})
        assert (status, body.get("error")) == (400, "since must be YYYY-MM-DD"), bad


def test_p95_uses_nz_day_not_utc_day():
    # Both are 2026-11-10 in NZ (UTC+13) but different UTC days.
    a = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)      # Tue 10 01:00 NZ
    b = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)      # Tue 10 22:00 NZ
    assert org.decision_shadow_summary([_srow(at=a), _srow(at=b)])[
        "cards_per_recipient_day_p95"] == 2.0


# ---------------------------------------------------------------- removed sources (read time)
# A card whose source was deleted serves no transcript-derived text: no evidence, no person
# in the remind label, and for todo.overdue no item text in the question.

C2 = "55555555-5555-5555-5555-555555555555"
C3 = "66666666-6666-6666-6666-666666666666"


def _gap_named(card_id=CARD, evidence=()):
    return _card_row(card_id=card_id, options=[
        {"id": "remind", "label": "Remind Tom to recheck", "recommended": True},
        {"id": "accept", "label": "Accept with reason", "recommended": False,
         "reason_required": True}], evidence=list(evidence))


def _overdue_row(card_id=C2, text="Order the rebar"):
    return _card_row(card_id=card_id, rule_id="todo.overdue", purpose="overdue",
                     subject_ref="77777777-7777-7777-7777-777777777777",
                     question=f"'{text}' was due 1 Nov 2026, still open",
                     options=[{"id": "remind", "label": "Remind Mike", "recommended": True},
                              {"id": "done", "label": "Mark done", "recommended": False}])


def _list(env, monkeypatch, rows):
    monkeypatch.setattr(org.decision_cards, "list_shadow",
                        lambda conn, limit, offset, label=None: rows)
    status, body = _call("GET", "/decision-shadow")
    assert status == 200
    return {c["card_id"]: c for c in body["cards"]}


def test_gap_without_evidence_and_tombstoned_source_gets_generic_label(env, monkeypatch):
    env.prefixes[("F", "2026-11-09")] = ["extractions/F/2026-11-09/aaaa"]   # bare spelling
    [card] = _list(env, monkeypatch, [_gap_named()]).values()
    assert card["options"][0] == {"id": "remind", "label": "Remind the inspector to recheck",
                                  "recommended": True}
    assert card["options"][1]["label"] == "Accept with reason"
    assert card["evidence"] == []


def test_tombstoned_source_drops_evidence_and_live_source_keeps_it(env, monkeypatch):
    env.sources = [
        {"card_id": CARD, "user_folder": "F", "report_date": "2026-11-09",
         "session": "sidaaaa", "walk_key": None, "walk_parts": []},
        {"card_id": C3, "user_folder": "F", "report_date": "2026-11-09",
         "session": "sidbbbb", "walk_key": None, "walk_parts": []}]
    env.prefixes[("F", "2026-11-09")] = ["extractions/F/2026-11-09/sidaaaa"]
    cards = _list(env, monkeypatch, [
        _gap_named(CARD, [{"quote": "rails loose", "at": None}]),
        _gap_named(C3, [{"quote": "toe board", "at": None}])])
    assert cards[CARD]["evidence"] == []
    assert cards[CARD]["options"][0]["label"] == "Remind the inspector to recheck"
    assert cards[C3]["evidence"] == [{"quote": "toe board", "at": None}]
    assert cards[C3]["options"][0]["label"] == "Remind Tom to recheck"


def test_mirror_path_still_removes(env, monkeypatch):
    env.mirror[("F", "2026-11-09")] = {"aaaa"}
    rows = [_gap_named(evidence=[{"quote": "q", "at": None}])]
    [card] = _list(env, monkeypatch, rows).values()
    assert card["evidence"] == []
    assert card["options"][0]["label"] == "Remind the inspector to recheck"


def test_unresolvable_gap_source_is_removed(env, monkeypatch):
    env.sources = []
    [card] = _list(env, monkeypatch, [_gap_named()]).values()
    assert card["options"][0]["label"] == "Remind the inspector to recheck"


def test_walk_with_one_deleted_part_is_removed(env, monkeypatch):
    parts = [{"user_folder": "F", "report_date": "2026-11-09", "session": "sidaaaa"},
             {"user_folder": "F", "report_date": "2026-11-09", "session": "sidcccc"}]
    env.sources = [{"card_id": CARD, "user_folder": "F", "report_date": "2026-11-09",
                    "session": "sidaaaa", "walk_key": "wk1", "walk_parts": parts}]
    env.prefixes[("F", "2026-11-09")] = ["extractions/F/2026-11-09/sidcccc"]
    rows = [_gap_named(evidence=[{"quote": "q", "at": None}])]
    [card] = _list(env, monkeypatch, rows).values()
    assert card["evidence"] == []
    assert card["options"][0]["label"] == "Remind the inspector to recheck"
    env.prefixes.clear()
    [card] = _list(env, monkeypatch, rows).values()
    assert card["evidence"] == [{"quote": "q", "at": None}]
    assert card["options"][0]["label"] == "Remind Tom to recheck"


def test_walk_whose_parts_do_not_resolve_is_removed(env, monkeypatch):
    env.sources = [{"card_id": CARD, "user_folder": "F", "report_date": "2026-11-09",
                    "session": "sidaaaa", "walk_key": "wk1", "walk_parts": []}]
    rows = [_gap_named(evidence=[{"quote": "q", "at": None}])]
    [card] = _list(env, monkeypatch, rows).values()
    assert card["evidence"] == []
    assert card["options"][0]["label"] == "Remind the inspector to recheck"


def test_overdue_removed_item_loses_text_and_person(env, monkeypatch):
    env.overdue[C2] = ("2026-11-01", False)
    env.overdue[C3] = (None, False)
    cards = _list(env, monkeypatch, [_overdue_row(C2), _overdue_row(C3, "Book the pump")])
    assert cards[C2]["question"] == "A to-do that was removed was due 1 Nov 2026, still open"
    assert cards[C2]["options"][0]["label"] == "Remind the owner"
    assert cards[C2]["options"][1]["label"] == "Mark done"
    assert cards[C3]["question"] == "A to-do that was removed is past its date, still open"
    assert "Book the pump" not in json.dumps(cards[C3])
    assert "Mike" not in json.dumps(cards[C3]["options"])


def test_overdue_live_item_unchanged_and_no_report_lookup(env, monkeypatch):
    [card] = _list(env, monkeypatch, [_overdue_row()]).values()
    assert card["question"] == "'Order the rebar' was due 1 Nov 2026, still open"
    assert card["options"][0]["label"] == "Remind Mike"
    # an overdue card has no report behind it: it is never sent down the report path
    assert not any(c[0] == "evidence_sources" for c in env.calls)


def test_backlog_card_untouched(env, monkeypatch):
    backlog = _card_row(card_id=C2, rule_id="todo.overdue_backlog", kind="fact",
                        question="3 to-dos on UC PK are more than 3 days overdue",
                        options=[{"id": "open_list", "label": "Open the list",
                                  "recommended": True}])
    cards = _list(env, monkeypatch, [backlog])
    assert cards[C2]["question"] == "3 to-dos on UC PK are more than 3 days overdue"
    assert cards[C2]["options"][0]["label"] == "Open the list"
    assert env.calls == []


def test_removed_card_keeps_the_response_shape(env, monkeypatch):
    env.overdue[C2] = ("2026-11-01", False)
    env.sources = []
    cards = _list(env, monkeypatch, [_gap_named(), _overdue_row(C2)])
    keys = {"card_id", "company_name", "site_name", "rule_id", "kind", "purpose", "urgency",
            "question", "consequence", "created_at", "shadow_label", "label_note", "options",
            "evidence", "recipients"}
    for c in cards.values():
        assert set(c) == keys
        assert isinstance(c["question"], str) and isinstance(c["evidence"], list)
        for o in c["options"]:
            assert isinstance(o["label"], str) and isinstance(o["recommended"], bool)


def test_query_count_is_bounded_per_page(env, monkeypatch):
    rows, sources = [], []
    for i in range(40):
        cid = f"{i:08d}-0000-0000-0000-000000000000"
        rows.append(_gap_named(cid, [{"quote": "q", "at": None}]))
        sources.append({"card_id": cid, "user_folder": "F",
                        "report_date": "2026-11-0%d" % (8 + i % 2), "session": f"sid{i:04d}",
                        "walk_key": None, "walk_parts": []})
    for i in range(40, 60):
        rows.append(_overdue_row(f"{i:08d}-0000-0000-0000-000000000000"))
    env.sources = sources
    _list(env, monkeypatch, rows)
    kinds = [c[0] for c in env.calls]
    assert kinds.count("evidence_sources") == 1
    assert kinds.count("overdue_item_states") == 1
    assert kinds.count("mirror") == 2 and kinds.count("prefixes") == 2       # 2 distinct days


def test_tombstone_lookup_failure_raises(env, monkeypatch):
    def boom(conn, folder, date):
        raise RuntimeError("db down")
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes", boom)
    with pytest.raises(RuntimeError):
        org._removed_source_cards(None, [_gap_named()])


def test_session_under_prefixes_spellings_and_boundaries():
    f, d = "Ben_Lin", "2026-11-09"
    under = org._session_under_prefixes
    assert under(f, d, "sidabcd", [f"extractions/{f}/{d}/abcd"])
    assert under(f, d, "abcd", [f"extractions/{f}/{d}/sidabcd"])
    assert under(f, d, "sidabcd", [f"extractions/{f}/{d}/"])            # whole day
    # fail closed (review M-1): a deeper prefix, or a base that extends the session id
    assert under(f, d, "sidabcd", [f"extractions/{f}/{d}/sidabcd/part"])
    assert under(f, d, "sidabcd", [f"extractions/{f}/{d}/sidabcd_x"])
    assert under(f, d, "abcd", [f"extractions/{f}/{d}/abcd_x"])
    assert not under(f, d, "sidabcd", [f"extractions/{f}/{d}/sidzzzz"])
    assert not under(f, d, "sidabcd", [f"extractions/Ben_Lin2/{d}/sidabcd"])
    assert not under(f, d, "sidabcd", [f"extractions/{f}/2026-11-10/sidabcd"])
