"""Session scoping (#11) — `session_scope`, the `session_id` field on
render_report_shape's topic shape, and `GET /api/org/sessions`.

Design: docs/superpowers/specs/2026-07-25-meeting-scoped-action-export.md §3.

The invariant these tests exist to protect: session MEMBERSHIP comes from the
extraction S3 key and nothing else. `time_range` is LLM free text and may only
LABEL a session — a malformed, absent or lying range must never move a topic
into the wrong meeting's export.
"""
import json
from datetime import datetime, timedelta

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
import session_scope  # noqa: E402  (after the psycopg importorskip gate)

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
OTHER_SITE_ID = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"

CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}

# Real key shapes, verbatim from the pipeline:
#   lambda_extract_session writes extractions/{folder}/{date}/{session_base}.json
#   lambda_ingest's report path stamps  reports/{date}/{folder}/daily_report.json
KEY_1300 = "extractions/Ada_L/2026-07-25/Benl1_2026-07-25_13-00-11.json"
KEY_1405 = "extractions/Ada_L/2026-07-25/Benl1_2026-07-25_14-05-00.json"
REPORT_KEY = "reports/2026-07-25/Ada_L/daily_report.json"


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def make_event(method, path, sub="sub-1", params=None):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": params,
        "body": None,
        "requestContext": {"authorizer": {"claims": {"sub": sub}}},
    }


def body_of(res):
    return json.loads(res["body"])


def _row(**over):
    base = {
        "id": "t-1", "site_id": SITE_ID, "site_name": "UC PK", "user_name": "Ada L",
        "source_s3_key": KEY_1300, "category": "progress", "title": "Slab pour",
        "summary": "Discussed the pour.", "time_range": "13:00 – 13:40",
        "participants": ["Ben", "Neil"], "work_class": "work",
        "action_items": [], "safety_observations": [], "findings": [], "photos": [],
    }
    base.update(over)
    return base


def _action(status="open", aid="a-1"):
    return {"id": aid, "text": "Fix it", "responsible": "Neil", "deadline": None,
            "deadline_text": None, "priority": "high", "status": status}


@pytest.fixture
def wired(monkeypatch):
    """Caller resolved, no redactions, ALL-scope site reach, folder resolvable."""
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org.users, "get_by_folder_name",
                        lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    monkeypatch.setattr(org.redactions, "list_active_for_topics", lambda conn, ids: {})
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    # No recordings row by default -> the RealPTT/worker-device common case,
    # where `ended_at` can only come from the LLM's time_range (design §3.2).
    monkeypatch.setattr(org.recordings, "duration_for_media",
                        lambda conn, cid, folder, date, sb: None)
    return monkeypatch


def _wire_rows(wired, rows):
    wired.setattr(org.topics, "list_topics_for_source_prefix", lambda conn, prefix: list(rows))


def _get(params=None, sub="sub-1"):
    return org.lambda_handler(make_event("GET", "/api/org/sessions", sub=sub, params=params), None)


DAY = {"date": "2026-07-25", "user": "Ada_L"}


# ----------------------------------------------------------
# 1. session_id derivation from real key shapes
# ----------------------------------------------------------

def test_session_id_derived_from_extraction_key():
    assert session_scope.session_ref(KEY_1300) == \
        ("Benl1_2026-07-25_13-00-11", session_scope.KIND_EXTRACTION)
    # VAD-offset suffixes never appear on the OUTPUT key (they are stripped
    # upstream by session_base_from_key), so the basename IS the session_base.
    assert session_scope.session_id_from_source_key(KEY_1405) == "Benl1_2026-07-25_14-05-00"


def test_session_id_null_for_report_sourced_key_and_kind_says_why():
    sid, kind = session_scope.session_ref(REPORT_KEY)
    assert sid is None
    assert kind == session_scope.KIND_REPORT      # "no session exists", not "unknown"


def test_session_kind_distinguishes_no_session_from_unknown():
    assert session_scope.session_ref(None) == (None, session_scope.KIND_UNKNOWN)
    assert session_scope.session_ref("") == (None, session_scope.KIND_UNKNOWN)
    # Wrong depth / not .json -> we decline to guess rather than mis-parse.
    assert session_scope.session_ref(
        "extractions/Ada_L/2026-07-25/nested/x.json")[1] == session_scope.KIND_UNKNOWN
    assert session_scope.session_ref(
        "extractions/Ada_L/2026-07-25/x.txt")[1] == session_scope.KIND_UNKNOWN
    # All three "no id" cases are still separable by kind:
    assert session_scope.session_ref(REPORT_KEY)[1] != session_scope.session_ref(None)[1]


def test_session_start_is_parsed_from_session_base_not_time_range():
    assert session_scope.session_start("Benl1_2026-07-25_13-00-11") == \
        datetime(2026, 7, 25, 13, 0, 11)
    # Hyphens-only filename shape (transcript_utils handles both).
    assert session_scope.session_start("Benl1_2026-07-25-13-00-11") == \
        datetime(2026, 7, 25, 13, 0, 11)
    assert session_scope.session_start("no-timestamp-here") is None
    assert session_scope.session_start(None) is None


def test_item_writer_reuses_the_shared_extraction_key_parse():
    """One definition of the key shape: the writer that STAMPS source_s3_key
    and the readers that parse it back must not drift."""
    import lambda_item_writer as w
    assert w.EXTRACTION_KEY_RE is session_scope.EXTRACTION_KEY_RE
    assert w._parse_extraction_key is session_scope.parse_extraction_key
    assert w._parse_extraction_key(KEY_1300) == \
        ("Ada_L", "2026-07-25", "Benl1_2026-07-25_13-00-11")


# ----------------------------------------------------------
# render_report_shape exposes the session (the whole #11 gap)
# ----------------------------------------------------------

def test_render_report_shape_exposes_session_id_per_topic():
    shape = org.render_report_shape(
        [_row(id="t-1", source_s3_key=KEY_1300), _row(id="t-2", source_s3_key=KEY_1405)],
        None, "2026-07-25", "Ada_L")
    assert [t["session_id"] for t in shape["topics"]] == \
        ["Benl1_2026-07-25_13-00-11", "Benl1_2026-07-25_14-05-00"]
    assert {t["session_kind"] for t in shape["topics"]} == {"extraction"}


def test_render_report_shape_report_sourced_topic_has_null_session():
    shape = org.render_report_shape([_row(source_s3_key=REPORT_KEY)], None, "2026-07-25", "Ada_L")
    assert shape["topics"][0]["session_id"] is None
    assert shape["topics"][0]["session_kind"] == "report"


def test_render_report_shape_never_leaks_the_raw_s3_key():
    shape = org.render_report_shape([_row(source_s3_key=KEY_1300)], None, "2026-07-25", "Ada_L")
    assert KEY_1300 not in json.dumps(shape)
    assert "source_s3_key" not in shape["topics"][0]


# ----------------------------------------------------------
# 2. Membership never depends on time_range
# ----------------------------------------------------------

def test_membership_ignores_malformed_or_absent_time_range(wired):
    """Two topics of the 13:00 session — one with a garbage range, one with
    none at all — plus one topic of the 14:05 session whose range LIES and
    claims 13:10. Membership must follow the key, not the text."""
    _wire_rows(wired, [
        _row(id="t-1", source_s3_key=KEY_1300, time_range="sometime after lunch"),
        _row(id="t-2", source_s3_key=KEY_1300, time_range=None),
        _row(id="t-3", source_s3_key=KEY_1405, time_range="13:10 – 13:20"),
    ])
    body = body_of(_get(DAY))
    by_id = {s["session_id"]: s for s in body["sessions"]}
    assert by_id["Benl1_2026-07-25_13-00-11"]["topic_row_ids"] == ["t-1", "t-2"]
    assert by_id["Benl1_2026-07-25_14-05-00"]["topic_row_ids"] == ["t-3"]
    # ...and the deterministic start still comes from session_base, so the
    # lying range never even affects the ordering.
    assert by_id["Benl1_2026-07-25_14-05-00"]["started_at"] == "2026-07-25T14:05:00"


def test_unparseable_time_range_degrades_end_label_only(wired):
    _wire_rows(wired, [_row(source_s3_key=KEY_1300, time_range="whenever")])
    s = body_of(_get(DAY))["sessions"][0]
    assert s["started_at"] == "2026-07-25T13:00:11"     # authoritative, unaffected
    assert s["ended_at"] is None                        # cosmetic, degrades to null
    assert s["label"] == "13:00 – ?"


def test_end_label_prefers_recorded_duration_over_time_range(wired):
    wired.setattr(org.recordings, "duration_for_media",
                  lambda conn, cid, folder, date, sb: 3600.0)
    _wire_rows(wired, [_row(source_s3_key=KEY_1300, time_range="13:00 – 13:40")])
    s = body_of(_get(DAY))["sessions"][0]
    assert s["ended_at"] == "2026-07-25T14:00:11"       # start + duration, one clock
    assert s["label"] == "13:00 – 14:00"


# ----------------------------------------------------------
# 3. Gap-merge threshold (display grouping only)
# ----------------------------------------------------------

def _blocked(gaps_minutes):
    """Build sessions 60 min long separated by `gaps_minutes`, return blocks."""
    sessions, t = [], datetime(2026, 7, 25, 9, 0, 0)
    for i, gap in enumerate([None] + list(gaps_minutes)):
        if gap is not None:
            t = t + timedelta(minutes=gap)
        sessions.append({"session_id": f"s{i}", "_start_dt": t, "_end_dt": t + timedelta(minutes=60)})
        t = t + timedelta(minutes=60)
    return [s["block"] for s in session_scope.assign_blocks(sessions)]


def test_gap_merge_just_under_threshold_merges():
    assert session_scope.SESSION_GAP_MINUTES == 15
    assert _blocked([14]) == [1, 1]
    assert _blocked([15]) == [1, 1]        # exactly at the threshold still merges (<=)


def test_gap_merge_just_over_threshold_splits():
    assert _blocked([16]) == [1, 2]
    assert _blocked([120]) == [1, 2]


def test_gap_merge_chains_and_restarts():
    # 10-min gap merges, then a 90-min gap starts a new block, then 5 merges.
    assert _blocked([10, 90, 5]) == [1, 1, 2, 2]


def test_gap_merge_never_merges_across_an_unknown_end():
    """Conservative (design §3.3): an unknown end is not evidence of adjacency,
    so the two render adjacent and the user multi-selects instead."""
    start = datetime(2026, 7, 25, 9, 0, 0)
    sessions = [
        {"session_id": "a", "_start_dt": start, "_end_dt": None},
        {"session_id": "b", "_start_dt": start + timedelta(minutes=61), "_end_dt": None},
    ]
    assert [s["block"] for s in session_scope.assign_blocks(sessions)] == [1, 2]


def test_gap_merge_surfaces_on_the_endpoint(wired):
    """Two recordings 5 minutes apart — one meeting split by a stop/restart."""
    _wire_rows(wired, [
        _row(id="t-1", source_s3_key=KEY_1300, time_range="13:00 – 14:00"),
        _row(id="t-2", source_s3_key=KEY_1405, time_range="14:05 – 14:30"),
    ])
    body = body_of(_get(DAY))
    assert [s["block"] for s in body["sessions"]] == [1, 1]      # merged for display
    assert [s["session_id"] for s in body["sessions"]] == \
        ["Benl1_2026-07-25_13-00-11", "Benl1_2026-07-25_14-05-00"]  # still two ids
    assert body["gap_minutes"] == 15


# ----------------------------------------------------------
# 4. ACL — a caller must never see sessions for a folder it can't view
# ----------------------------------------------------------

def test_acl_graded_denies_folder_the_caller_cannot_view(wired):
    """GRADED_ROLES on: the gate is /timeline's own `_can_view_folder`."""
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.scope, "visible_scope", lambda conn, caller: {
        "site_ids": {SITE_ID}, "user_scope": "SELF+WORKERS", "author_ids": {"u-uuid-1"},
        "self_folder": "Ada_L", "self_user_id": "u-uuid-1",
        "company_id": "c-uuid-1", "cross_company": False,
    })
    called = []
    wired.setattr(org.topics, "list_topics_for_source_prefix",
                  lambda conn, prefix: called.append(prefix) or [])
    res = _get({"date": "2026-07-25", "user": "Someone_Else"})
    assert res["statusCode"] == 403
    assert called == []                    # denied BEFORE any Aurora read


def test_acl_graded_allows_folder_the_caller_can_view(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.scope, "visible_scope", lambda conn, caller: {
        "site_ids": {SITE_ID}, "user_scope": "SITE", "author_ids": None,
        "self_folder": "Ada_L", "self_user_id": "u-uuid-1",
        "company_id": "c-uuid-1", "cross_company": False,
    })
    wired.setattr(org.memberships, "caller_site_roles", lambda conn, uid: {SITE_ID: "worker"})
    _wire_rows(wired, [_row(source_s3_key=KEY_1300)])
    res = _get({"date": "2026-07-25", "user": "Neil_B"})
    assert res["statusCode"] == 200
    assert len(body_of(res)["sessions"]) == 1


def test_acl_graded_off_non_all_caller_may_only_read_own_folder(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = _get({"date": "2026-07-25", "user": "Someone_Else"})
    assert res["statusCode"] == 403
    assert org.GRADED_ROLES is False        # the flag-off branch is what ran


def test_acl_clips_rows_to_allowed_sites(wired):
    """The folder gate alone is not enough: a multi-site target's day can span
    sites outside the caller's reach, exactly as /timeline clips them."""
    _wire_rows(wired, [
        _row(id="t-in", source_s3_key=KEY_1300, site_id=SITE_ID),
        _row(id="t-out", source_s3_key=KEY_1405, site_id=OTHER_SITE_ID),
    ])
    body = body_of(_get(DAY))
    assert [s["session_id"] for s in body["sessions"]] == ["Benl1_2026-07-25_13-00-11"]


def test_sessions_requires_a_valid_date(wired):
    assert _get({"user": "Ada_L"})["statusCode"] == 400
    assert _get({"date": "25-07-2026", "user": "Ada_L"})["statusCode"] == 400


# ----------------------------------------------------------
# 5. Counts exclude redacted and non_work
# ----------------------------------------------------------

def test_counts_exclude_redacted_and_non_work_topics(wired):
    _wire_rows(wired, [
        _row(id="t-work", source_s3_key=KEY_1300, participants=["Ben"],
             action_items=[_action("open", "a-1"), _action("done", "a-2")]),
        _row(id="t-personal", source_s3_key=KEY_1300, work_class="non_work",
             participants=["Ben", "Ada's wife"], action_items=[_action("open", "a-3")]),
        _row(id="t-removed", source_s3_key=KEY_1300, participants=["Redacted Person"],
             action_items=[_action("open", "a-4")]),
    ])
    wired.setattr(org.redactions, "list_active_for_topics",
                  lambda conn, ids: {"t-removed": {"id": "r-1"}})
    body = body_of(_get(DAY))
    s = body["sessions"][0]
    assert s["topic_count"] == 1
    assert s["open_action_count"] == 1                  # only the work topic's open item
    assert s["topic_row_ids"] == ["t-work"]             # the export's scope handle
    assert s["participants"] == ["Ben"]                 # excluded topics' names never surface
    assert body["excluded"] == {"non_work": 1, "redacted": 1}


def test_null_work_class_counts_as_work(wired):
    """Life-separation Q1 comparison direction: only an explicit 'non_work'
    excludes. NULL/absent is work — never the other way round."""
    absent = _row(id="t-missing", source_s3_key=KEY_1300, action_items=[_action(aid="a-9")])
    absent.pop("work_class")                            # column absent entirely
    _wire_rows(wired, [
        _row(id="t-null", source_s3_key=KEY_1300, work_class=None, action_items=[_action()]),
        absent,
    ])
    body = body_of(_get(DAY))
    assert body["sessions"][0]["topic_count"] == 2
    assert body["sessions"][0]["open_action_count"] == 2
    assert body["excluded"] == {"non_work": 0, "redacted": 0}


def test_fully_excluded_session_is_dropped_not_listed_empty(wired):
    _wire_rows(wired, [_row(id="t-p", source_s3_key=KEY_1300, work_class="non_work",
                            title="Weekend plans")])
    body = body_of(_get(DAY))
    assert body["sessions"] == []
    assert body["excluded"]["non_work"] == 1
    assert "Weekend plans" not in json.dumps(body)


# ----------------------------------------------------------
# 6/7. Whole-day shapes
# ----------------------------------------------------------

def test_one_continuous_recording_yields_exactly_one_session(wired):
    _wire_rows(wired, [
        _row(id="t-1", source_s3_key=KEY_1300, time_range="13:00 – 13:40",
             participants=["Ben", "Neil"], action_items=[_action("open", "a-1")]),
        _row(id="t-2", source_s3_key=KEY_1300, time_range="13:40 – 14:20",
             participants=["Neil", "James"], action_items=[_action("open", "a-2"),
                                                           _action("done", "a-3")]),
    ])
    body = body_of(_get(DAY))
    assert len(body["sessions"]) == 1
    s = body["sessions"][0]
    assert s == {
        "session_id": "Benl1_2026-07-25_13-00-11",
        "started_at": "2026-07-25T13:00:11",
        "ended_at": "2026-07-25T14:20:00",
        "site_name": "UC PK",
        "topic_count": 2,
        "open_action_count": 2,
        "participants": ["Ben", "Neil", "James"],       # union, deduped, order kept
        "topic_row_ids": ["t-1", "t-2"],
        "label": "13:00 – 14:20",
        "block": 1,
    }


def test_report_only_day_yields_no_sessions_and_is_not_an_error(wired):
    """Pre-flip / zero-extraction-fallback day: one key for the whole day, so
    no session granularity exists. 200 + empty list — the UI renders its
    "Whole day" row. A 404 here would be indistinguishable from a failure."""
    _wire_rows(wired, [_row(id="t-r", source_s3_key=REPORT_KEY)])
    res = _get(DAY)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert body["sessions"] == []
    assert body["excluded"] == {"non_work": 0, "redacted": 0}
    assert body["date"] == "2026-07-25" and body["user"] == "Ada_L"


def test_empty_day_returns_200_with_empty_sessions(wired):
    _wire_rows(wired, [])
    res = _get(DAY)
    assert res["statusCode"] == 200
    assert body_of(res)["sessions"] == []


def test_sessions_are_ordered_by_deterministic_start(wired):
    _wire_rows(wired, [
        _row(id="t-late", source_s3_key=KEY_1405, time_range="09:00 – 09:10"),
        _row(id="t-early", source_s3_key=KEY_1300, time_range="23:00 – 23:30"),
    ])
    body = body_of(_get(DAY))
    assert [s["session_id"] for s in body["sessions"]] == \
        ["Benl1_2026-07-25_13-00-11", "Benl1_2026-07-25_14-05-00"]


def test_session_with_unparseable_base_time_sorts_last_and_never_merges(wired):
    odd = "extractions/Ada_L/2026-07-25/no_base_time.json"
    _wire_rows(wired, [
        _row(id="t-odd", source_s3_key=odd, time_range="13:05 – 13:10"),
        _row(id="t-ok", source_s3_key=KEY_1300),
    ])
    body = body_of(_get(DAY))
    assert [s["session_id"] for s in body["sessions"]] == \
        ["Benl1_2026-07-25_13-00-11", "no_base_time"]
    assert body["sessions"][1]["started_at"] is None
    assert body["sessions"][1]["label"] == "? – ?"
    assert [s["block"] for s in body["sessions"]] == [1, 2]
