"""org-api decides WHAT a report covers; the worker decides how it reads. The artifact
is the whole contract between them, so this pins the three keys generation needs."""
import json

import pytest

import lambda_org_api as org

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
DATE = "2026-07-25"
S1300 = "Benl1_2026-07-25_13-00-11"
S1405 = "Benl1_2026-07-25_14-05-00"
KEY_1300 = f"extractions/Ada_L/{DATE}/{S1300}.json"
KEY_1405 = f"extractions/Ada_L/{DATE}/{S1405}.json"
CALLER = {"id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
          "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _event(method, path, params=None, body=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": params,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


def _row(**over):
    base = {"id": "t-1", "site_id": SITE_ID, "site_name": "UC PK", "user_name": "Ada L",
            "source_s3_key": KEY_1300, "category": "progress", "title": "Slab pour",
            "summary": "Discussed the pour.", "time_range": "13:00 – 13:40",
            "participants": ["Ben"], "work_class": "work", "action_items": [],
            "safety_observations": [], "findings": [], "photos": []}
    base.update(over)
    return base


ROWS = [
    _row(id="t-1", source_s3_key=KEY_1300, title="Slab pour", time_range="13:00 – 13:40"),
    _row(id="t-2", source_s3_key=KEY_1300, title="Personal call",
         time_range="13:40 – 13:45", work_class="non_work"),
]


@pytest.fixture
def day(monkeypatch):
    puts = []
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org.users, "get_by_folder_name",
                        lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    monkeypatch.setattr(org.redactions, "list_active_for_topics", lambda conn, ids: {})
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes",
                        lambda conn, folder=None, date=None: [])
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org.recordings, "duration_for_media",
                        lambda conn, cid, folder, date, sb: None)
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix, **k: list(ROWS))
    monkeypatch.setattr(org, "s3", lambda: type("S", (), {
        "put_object": staticmethod(lambda **kw: puts.append(kw))})())
    return monkeypatch, puts


def _body(**kw):
    base = {"deliver": "download", "templateId": "personal-meeting", "templateVersion": 3}
    base.update(kw)
    return base


def _generate_raw(day_fixture, body):
    _mp, puts = day_fixture
    res = org.lambda_handler(_event("POST", f"/api/org/days/{DATE}/report",
                                    {"user": "Ada_L"}, body), None)
    return res, puts


@pytest.fixture
def day_generate_raw(day):
    def _call(body):
        return _generate_raw(day, body)
    return _call


@pytest.fixture
def day_generate(day):
    def _call(body):
        res, puts = _generate_raw(day, body)
        assert res["statusCode"] == 202, res
        assert len(puts) == 1
        return puts[0]
    return _call


def test_a_named_template_reaches_the_worker_with_the_window(day_generate):
    put = day_generate(_body(**{"from": "09:00", "to": "11:30"}))
    artifact = json.loads(put["Body"])
    assert artifact["generate"] == {"templateId": "personal-meeting", "templateVersion": 3}
    assert artifact["window"] == {"from": "09:00", "to": "11:30"}


def test_an_unknown_template_is_refused_before_anything_is_enqueued(day_generate_raw):
    res, puts = day_generate_raw(_body(templateId="does-not-exist"))
    assert res["statusCode"] == 400
    assert "template" in json.loads(res["body"])["error"].lower()
    assert puts == [], "nothing may be enqueued for a template that does not exist"


def test_the_excluded_topics_travel_with_their_times(day_generate):
    put = day_generate(_body())
    artifact = json.loads(put["Body"])
    assert {"id", "time_range"} <= set(artifact["excludedTopics"][0])


def test_a_request_without_a_template_is_still_the_old_assembled_report(day_generate):
    put = day_generate({"deliver": "download"})
    artifact = json.loads(put["Body"])
    assert "generate" not in artifact


# ----------------------------------------------------------
# IMPORTANT 3 — deliver="email" is refused for a generate request at the door.
# The worker's generate branch always writes emailed: false and never sends, so
# accepting the combination here would silently drop a report the caller
# believes was emailed.
# ----------------------------------------------------------

def test_day_generate_with_email_delivery_is_refused_before_anything_is_enqueued(day_generate_raw):
    res, puts = day_generate_raw(_body(deliver="email", recipients=["a@x.nz"]))
    assert res["statusCode"] == 400
    assert "download" in json.loads(res["body"])["error"].lower()
    assert puts == [], "nothing may be enqueued for a template+email combination"


def test_day_email_delivery_without_a_template_is_still_accepted(day_generate):
    # Without a template, deliver="email" is the pre-existing assembled-report
    # path, which the worker DOES send -- only a NAMED-TEMPLATE + email combo
    # is refused.
    put = day_generate({"deliver": "email", "recipients": ["a@x.nz"]})
    artifact = json.loads(put["Body"])
    assert "generate" not in artifact
    assert artifact["deliver"] == "email"


# ----------------------------------------------------------
# CRITICAL 1 — the deletion-tombstone arm.
#
# `_report_rows_in_scope` (which decides what a report may CONTAIN) drops a row
# for four reasons: not an extraction key, wrong session, redacted-or-non_work,
# or a source key under a deleted (tombstoned) prefix. `_excluded_topics_for`
# (which decides what the worker is TOLD to cut) must name a row it drops for
# ANY of the reasons that survive to it -- a "deleted" recording is a reversible
# mask in this codebase, the audio is still in the bucket, so a topic dropped
# only by a recording tombstone must still be named or its speech can reach
# the model prompt unclipped.
# ----------------------------------------------------------

DELETED_PREFIX = f"extractions/Ada_L/{DATE}/{S1405}"


def test_a_topic_hidden_only_by_a_deleted_recording_is_named_as_excluded(day):
    mp, puts = day
    rows = [
        _row(id="t-kept", source_s3_key=KEY_1300, title="Slab pour", time_range="13:00 – 13:40"),
        # Not redacted, not non_work -- the ONLY reason this drops out of the
        # report is the recording tombstone on its session's source prefix.
        _row(id="t-tombstoned", source_s3_key=KEY_1405, title="Deleted recording",
             time_range="14:05 – 14:10"),
    ]
    mp.setattr(org.topics, "list_topics_for_source_prefix", lambda conn, prefix, **k: list(rows))
    mp.setattr(org.redactions, "deleted_source_prefixes",
              lambda conn, folder=None, date=None: [DELETED_PREFIX] if folder == "Ada_L" else [])
    res, puts_out = _generate_raw((mp, puts), _body())
    assert res["statusCode"] == 202
    artifact = json.loads(puts_out[0]["Body"])
    excluded_ids = {t["id"] for t in artifact["excludedTopics"]}
    assert "t-tombstoned" in excluded_ids
    assert "t-kept" not in excluded_ids
    # the content the worker still renders never includes the tombstoned topic
    assert {t["topic_title"] for t in artifact["content"]["topics"]} == {"Slab pour"}


# ----------------------------------------------------------
# CRITICAL 1 (contributing cause) — a non-extraction row must never reach the
# worker's exclusion list, the same cut `_report_rows_in_scope` makes. Such a
# row could never correspond to a stretch of recorded speech, so an
# unparseable time_range on it must not abort a generate request for a window
# that could never have contained it.
# ----------------------------------------------------------

REPORT_KEY = f"reports/Ada_L/{DATE}.json"


def test_a_non_extraction_row_never_becomes_an_excluded_topic(day):
    mp, puts = day
    rows = [
        _row(id="t-kept", source_s3_key=KEY_1300, title="Slab pour", time_range="13:00 – 13:40"),
        # A report-sourced row, marked non_work, with an unparseable time_range --
        # if this reached the worker's excludedTopics it would abort every generate
        # for this folder/date over a row that was never part of the transcript
        # timeline in the first place.
        _row(id="t-report-row", source_s3_key=REPORT_KEY, title="Whole day",
             time_range="not a time", work_class="non_work"),
    ]
    mp.setattr(org.topics, "list_topics_for_source_prefix", lambda conn, prefix, **k: list(rows))
    res, puts_out = _generate_raw((mp, puts), _body())
    assert res["statusCode"] == 202
    artifact = json.loads(puts_out[0]["Body"])
    excluded_ids = {t["id"] for t in artifact["excludedTopics"]}
    assert "t-report-row" not in excluded_ids


# ----------------------------------------------------------
# IMPORTANT 2/3 — session-scoped generate: `session_report_generate` must get
# the same `generate`/`window`/`excludedTopics` treatment as the day route,
# and its `excludedTopics` must never name a topic from a DIFFERENT session.
# ----------------------------------------------------------

SESSION_ROWS = [
    _row(id="t-1300-work", source_s3_key=KEY_1300, title="Slab pour", time_range="13:00 – 13:40"),
    _row(id="t-1300-personal", source_s3_key=KEY_1300, title="Personal call",
         time_range="13:40 – 13:45", work_class="non_work"),
    # A different session's excluded topic -- must never leak into S1300's
    # excludedTopics even though it fails the same exclusion rule.
    _row(id="t-1405-personal", source_s3_key=KEY_1405, title="Other session's personal",
         time_range="14:05 – 14:10", work_class="non_work"),
]


def _session_generate_raw(day_fixture, session_id, body):
    mp, puts = day_fixture
    mp.setattr(org.topics, "list_topics_for_source_prefix",
              lambda conn, prefix, **k: list(SESSION_ROWS))
    res = org.lambda_handler(_event("POST", f"/api/org/sessions/{session_id}/report",
                                    {"date": DATE, "user": "Ada_L"}, body), None)
    return res, puts


def test_session_generate_reaches_the_worker_with_generate_and_window(day):
    res, puts = _session_generate_raw(day, S1300, _body(**{"from": "09:00", "to": "11:30"}))
    assert res["statusCode"] == 202, res
    assert len(puts) == 1
    artifact = json.loads(puts[0]["Body"])
    assert artifact["generate"] == {"templateId": "personal-meeting", "templateVersion": 3}
    assert artifact["window"] == {"from": "09:00", "to": "11:30"}


def test_session_generate_without_a_template_is_still_the_old_assembled_report(day):
    res, puts = _session_generate_raw(day, S1300, {"deliver": "download"})
    assert res["statusCode"] == 202, res
    artifact = json.loads(puts[0]["Body"])
    assert "generate" not in artifact


def test_session_generate_an_unknown_template_is_refused_before_anything_is_enqueued(day):
    res, puts = _session_generate_raw(day, S1300, _body(templateId="does-not-exist"))
    assert res["statusCode"] == 400
    assert "template" in json.loads(res["body"])["error"].lower()
    assert puts == []


def test_session_generate_excluded_topics_are_scoped_to_the_session(day):
    res, puts = _session_generate_raw(day, S1300, _body())
    assert res["statusCode"] == 202, res
    artifact = json.loads(puts[0]["Body"])
    excluded_ids = {t["id"] for t in artifact["excludedTopics"]}
    # this session's own excluded topic, never the 14:05 session's
    assert excluded_ids == {"t-1300-personal"}


def test_session_generate_with_email_delivery_is_refused_before_anything_is_enqueued(day):
    res, puts = _session_generate_raw(
        day, S1300, _body(deliver="email", recipients=["a@x.nz"]))
    assert res["statusCode"] == 400
    assert "download" in json.loads(res["body"])["error"].lower()
    assert puts == [], "nothing may be enqueued for a template+email combination"
