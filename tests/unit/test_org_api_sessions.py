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
# Session title — Tier-2 T1 (cosmetic label for the report picker/modal)
# ----------------------------------------------------------

def test_session_title_picks_most_salient_topic_by_open_actions():
    srows = [
        {"title": "Slab pour", "action_items": [_action("open")]},
        {"title": "Drainage inspection", "action_items": [_action("open"), _action("open")]},
    ]
    assert org._session_title(srows, "UC PK") == "Drainage inspection"


def test_session_title_ties_break_by_first_appearance():
    srows = [
        {"title": "First topic", "action_items": [_action("open")]},
        {"title": "Second topic", "action_items": [_action("open")]},
    ]
    assert org._session_title(srows, "UC PK") == "First topic"


def test_session_title_skips_untitled_topics():
    srows = [
        {"title": "   ", "action_items": [_action("open"), _action("open")]},
        {"title": "Real title", "action_items": [_action("open")]},
    ]
    assert org._session_title(srows, "UC PK") == "Real title"


def test_session_title_count_fallback_when_no_titles():
    srows = [{"title": "", "action_items": []}, {"title": None, "action_items": []}]
    assert org._session_title(srows, "UC PK") == "2 topics · UC PK"


def test_session_title_singular_and_site_fallback():
    assert org._session_title([{"title": "", "action_items": []}], None) == "1 topic · site"
    # title is never authoritative — it only labels
    assert org._session_title([{"title": "One", "action_items": []}], "S") == "One"


def test_endpoint_exposes_session_title(wired):
    _wire_rows(wired, [
        _row(id="t-1", source_s3_key=KEY_1300, title="Morning check-in",
             action_items=[_action("open")]),
        _row(id="t-2", source_s3_key=KEY_1300, title="Slab pour",
             action_items=[_action("open"), _action("open")]),
    ])
    s = body_of(_get(DAY))["sessions"][0]
    assert s["title"] == "Slab pour"   # lead = most open actions


# ----------------------------------------------------------
# Tier-2 T2 — POST /sessions/{id}/report/preview (review-modal content)
# ----------------------------------------------------------

SESSION_1300 = "Benl1_2026-07-25_13-00-11"
PREVIEW_PARAMS = {"date": "2026-07-25", "user": "Ada_L"}


def _preview(session_id, params=None, sub="sub-1"):
    path = f"/api/org/sessions/{session_id}/report/preview"
    return org.lambda_handler(make_event("POST", path, sub=sub, params=params), None)


def test_preview_assembles_only_this_sessions_content(wired):
    _wire_rows(wired, [
        _row(id="t-1", source_s3_key=KEY_1300, title="Morning check-in",
             summary="Discussed the plan.", participants=["Ben", "Neil"],
             action_items=[_action("open", "a-1")]),
        _row(id="t-2", source_s3_key=KEY_1300, title="Slab pour",
             participants=["Neil"], action_items=[_action("open", "a-2"), _action("open", "a-3")]),
        _row(id="t-other", source_s3_key=KEY_1405, title="Other meeting"),  # different session
    ])
    b = body_of(_preview(SESSION_1300, PREVIEW_PARAMS))
    assert b["sessionId"] == SESSION_1300
    assert b["title"] == "Slab pour"                     # most open actions
    assert b["date"] == "2026-07-25"
    titles = {t["topic_title"] for t in b["topics"]}
    assert titles == {"Morning check-in", "Slab pour"}   # never the 14:05 session's topic
    assert b["fieldDefaults"]["attendees"] == b["participants"]
    assert b["fieldDefaults"]["date"] == "2026-07-25"


def test_preview_excludes_redacted_and_non_work(wired):
    _wire_rows(wired, [
        _row(id="t-work", source_s3_key=KEY_1300, title="Work topic", action_items=[_action("open")]),
        _row(id="t-personal", source_s3_key=KEY_1300, title="Personal", work_class="non_work"),
        _row(id="t-removed", source_s3_key=KEY_1300, title="Removed"),
    ])
    wired.setattr(org.redactions, "list_active_for_topics",
                  lambda conn, ids: {"t-removed": {"id": "r-1"}})
    b = body_of(_preview(SESSION_1300, PREVIEW_PARAMS))
    assert {t["topic_title"] for t in b["topics"]} == {"Work topic"}


def test_preview_unknown_session_is_404(wired):
    _wire_rows(wired, [_row(id="t-1", source_s3_key=KEY_1300)])
    # a valid-shaped but non-matching session_base -> no topics in scope
    assert _preview("Benl1_2026-07-25_08-00-00", PREVIEW_PARAMS)["statusCode"] == 404


def test_preview_requires_valid_date(wired):
    _wire_rows(wired, [_row(source_s3_key=KEY_1300)])
    assert _preview(SESSION_1300, {"user": "Ada_L"})["statusCode"] == 400
    assert _preview(SESSION_1300, {"date": "nope", "user": "Ada_L"})["statusCode"] == 400


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
        "title": "Slab pour",                            # T1 cosmetic label (both topics' title)
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


# ----------------------------------------------------------
# Tier-2 T3 — POST /sessions/{id}/report (async enqueue of the generate job)
#
# org-api is in-VPC (no python-docx, and BUG-36 forbids reaching SES / invoking
# a lambda without an endpoint), so it does NOT render the doc: it re-derives the
# session scope, assembles the reviewed content + the user's confirmed fields, and
# writes ONE request artifact to session_report_requests/ (the match_requests/
# pattern). A non-VPC worker consumes it. These tests pin the enqueue contract.
# ----------------------------------------------------------

GEN_PARAMS = {"date": "2026-07-25", "user": "Ada_L"}
GEN_BODY = {
    "title": "Morning site inspection",
    "attendees": ["Ben", "Neil", "James"],
    "fields": {"weather": "Fine", "sign_off": "A. L"},
    "deliver": "download",
}


@pytest.fixture
def s3_puts(wired):
    """Capture s3().put_object calls so a test can inspect the enqueued artifact."""
    puts = []

    class _FakeS3:
        def put_object(self, **kw):
            puts.append(kw)
            return {}

    wired.setattr(org, "s3", lambda: _FakeS3())
    return puts


def _generate(session_id, body, params=GEN_PARAMS, sub="sub-1"):
    ev = make_event("POST", f"/api/org/sessions/{session_id}/report", sub=sub, params=params)
    ev["body"] = json.dumps(body) if body is not None else None
    return org.lambda_handler(ev, None)


def _two_topic_session(wired):
    _wire_rows(wired, [
        _row(id="t-1", source_s3_key=KEY_1300, title="Morning check-in",
             participants=["Ben", "Neil"], action_items=[_action("open", "a-1")]),
        _row(id="t-2", source_s3_key=KEY_1300, title="Slab pour",
             participants=["Neil"], action_items=[_action("open", "a-2"), _action("open", "a-3")]),
        _row(id="t-other", source_s3_key=KEY_1405, title="Other meeting"),   # different session
    ])


def test_generate_returns_202_queued_with_a_poll_handle(wired, s3_puts):
    _two_topic_session(wired)
    res = _generate(SESSION_1300, GEN_BODY)
    assert res["statusCode"] == 202
    b = body_of(res)
    assert b["status"] == "queued"
    assert b["sessionId"] == SESSION_1300
    assert b["resultKey"].startswith("session_report_results/")   # frontend polls this


def test_generate_writes_one_request_artifact_scoped_to_the_session(wired, s3_puts):
    _two_topic_session(wired)
    _generate(SESSION_1300, GEN_BODY)
    assert len(s3_puts) == 1
    put = s3_puts[0]
    assert put["Key"].startswith("session_report_requests/")
    req = json.loads(put["Body"])
    # scope re-derived server-side from session_id: never the 14:05 session's topic
    assert {t["topic_title"] for t in req["content"]["topics"]} == {"Morning check-in", "Slab pour"}


def test_generate_carries_the_users_confirmed_fields(wired, s3_puts):
    _two_topic_session(wired)
    _generate(SESSION_1300, GEN_BODY)
    req = json.loads(s3_puts[0]["Body"])
    assert req["title"] == "Morning site inspection"       # user override wins over derived title
    assert req["attendees"] == ["Ben", "Neil", "James"]    # user-confirmed (adds James)
    assert req["fields"]["weather"] == "Fine"
    assert req["deliver"] == "download"


def test_generate_re_derives_scope_and_ignores_client_supplied_ids(wired, s3_puts):
    """Scope integrity (spec §6): a caller cannot widen the export to another
    meeting by supplying its own id list — the server derives topic_row_ids
    from session_id alone."""
    _two_topic_session(wired)
    _generate(SESSION_1300, {**GEN_BODY, "topic_row_ids": ["t-other", "t-evil"]})
    req = json.loads(s3_puts[0]["Body"])
    assert sorted(req["content"]["topic_row_ids"]) == ["t-1", "t-2"]   # server's, not the client's


def test_generate_excludes_redacted_and_non_work(wired, s3_puts):
    _wire_rows(wired, [
        _row(id="t-work", source_s3_key=KEY_1300, title="Work topic", action_items=[_action("open")]),
        _row(id="t-personal", source_s3_key=KEY_1300, title="Personal", work_class="non_work"),
        _row(id="t-removed", source_s3_key=KEY_1300, title="Removed"),
    ])
    wired.setattr(org.redactions, "list_active_for_topics",
                  lambda conn, ids: {"t-removed": {"id": "r-1"}})
    _generate(SESSION_1300, GEN_BODY)
    req = json.loads(s3_puts[0]["Body"])
    assert {t["topic_title"] for t in req["content"]["topics"]} == {"Work topic"}


def test_generate_rejects_unknown_deliver(wired, s3_puts):
    _two_topic_session(wired)
    assert _generate(SESSION_1300, {**GEN_BODY, "deliver": "carrier-pigeon"})["statusCode"] == 400
    assert s3_puts == []                                    # nothing enqueued on a bad request


def test_generate_email_requires_recipients(wired, s3_puts):
    _two_topic_session(wired)
    assert _generate(SESSION_1300, {**GEN_BODY, "deliver": "email"})["statusCode"] == 400
    assert _generate(SESSION_1300, {**GEN_BODY, "deliver": "email",
                                    "recipients": ["a@x.nz"]})["statusCode"] == 202


def test_generate_requires_a_valid_date(wired, s3_puts):
    _two_topic_session(wired)
    assert _generate(SESSION_1300, GEN_BODY, params={"user": "Ada_L"})["statusCode"] == 400
    assert _generate(SESSION_1300, GEN_BODY,
                     params={"date": "nope", "user": "Ada_L"})["statusCode"] == 400


def test_generate_unknown_session_is_404(wired, s3_puts):
    _wire_rows(wired, [_row(id="t-1", source_s3_key=KEY_1300)])
    assert _generate("Benl1_2026-07-25_08-00-00", GEN_BODY)["statusCode"] == 404
    assert s3_puts == []


def test_generate_malformed_body_is_400(wired, s3_puts):
    _two_topic_session(wired)
    ev = make_event("POST", f"/api/org/sessions/{SESSION_1300}/report", params=GEN_PARAMS)
    ev["body"] = "{not json"
    assert org.lambda_handler(ev, None)["statusCode"] == 400


# ----------------------------------------------------------
# Tier-2 T3 — GET /sessions/{id}/report/status (poll the async worker's result)
#
# The frontend polls with the requestId from the 202. The endpoint rebuilds the
# resultKey server-side from (folder, date, session_id, requestId) — a client
# can't point it at another folder's result — reads it, and presigns the doc.
# ----------------------------------------------------------

STATUS_PARAMS = {"date": "2026-07-25", "user": "Ada_L", "requestId": "req123"}
RESULT_KEY = "session_report_results/Ada_L/2026-07-25/Benl1_2026-07-25_13-00-11/req123.json"
DOC_KEY = "session_reports/Ada_L/2026-07-25/Benl1_2026-07-25_13-00-11/req123.docx"


@pytest.fixture
def result_store(wired):
    """Fake S3 for the status read: serve a result JSON (or a NoSuchKey 404) and
    presign the doc."""
    from io import BytesIO
    from botocore.exceptions import ClientError as _CE
    store = {}

    class _S3:
        def get_object(self, Bucket, Key):
            if Key not in store:
                raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {"Body": BytesIO(json.dumps(store[Key]).encode())}

        def generate_presigned_url(self, op, Params, ExpiresIn):
            return f"https://signed.example/{Params['Key']}"

    wired.setattr(org, "s3", lambda: _S3())
    return store


def _status(session_id, params=STATUS_PARAMS, sub="sub-1"):
    return org.lambda_handler(
        make_event("GET", f"/api/org/sessions/{session_id}/report/status", sub=sub, params=params), None)


def test_status_is_pending_before_the_worker_writes_a_result(wired, result_store):
    assert body_of(_status(SESSION_1300))["status"] == "pending"    # result_store empty -> 404 -> pending


def test_status_done_returns_a_presigned_docurl(wired, result_store):
    result_store[RESULT_KEY] = {"status": "done", "docKey": DOC_KEY, "emailed": False}
    b = body_of(_status(SESSION_1300))
    assert b["status"] == "done"
    assert b["docUrl"].endswith("req123.docx")     # presigned GET of the worker's docKey
    assert b["emailed"] is False


def test_status_surfaces_the_worker_error(wired, result_store):
    result_store[RESULT_KEY] = {"status": "error", "error": "document generation unavailable"}
    b = body_of(_status(SESSION_1300))
    assert b["status"] == "error"
    assert "unavailable" in b["error"]


def test_status_requires_date_and_request_id(wired, result_store):
    assert _status(SESSION_1300, {"user": "Ada_L", "requestId": "req123"})["statusCode"] == 400
    assert _status(SESSION_1300, {"date": "2026-07-25", "user": "Ada_L"})["statusCode"] == 400


def test_status_reads_the_server_reconstructed_result_key(wired, result_store):
    """Only finds the result because the endpoint rebuilt exactly RESULT_KEY from
    (folder, date, session, requestId) — scope integrity, not a client-supplied key."""
    result_store[RESULT_KEY] = {"status": "done", "docKey": DOC_KEY}
    assert body_of(_status(SESSION_1300))["status"] == "done"
