"""POST /days/{date}/report/preview and /days/{date}/report.

A day report is every reportable topic of one person's day, across meetings,
through the same scope core as a meeting report. Spec 2026-09-15 §5.1, §5.2.
"""
import json
import re

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

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
    monkeypatch.setattr(org, "s3", lambda: type("S", (), {
        "put_object": staticmethod(lambda **kw: puts.append(kw))})())
    return monkeypatch, puts


def _rows(mp, rows):
    mp.setattr(org.topics, "list_topics_for_source_prefix", lambda conn, prefix, **k: list(rows))


ROWS = [
    _row(id="t-late", source_s3_key=KEY_1300, title="Late in first meeting", time_range="13:30 – 13:40"),
    _row(id="t-second", source_s3_key=KEY_1405, title="Second meeting", time_range="14:05 – 14:20"),
    _row(id="t-early", source_s3_key=KEY_1300, title="Early in first meeting", time_range="13:05 – 13:10"),
    _row(id="t-personal", source_s3_key=KEY_1300, title="Personal", work_class="non_work"),
]


def _preview(params=None, body=None):
    return org.lambda_handler(_event("POST", f"/api/org/days/{DATE}/report/preview",
                                     params or {"user": "Ada_L"}, body), None)


def _generate(body, params=None):
    return org.lambda_handler(_event("POST", f"/api/org/days/{DATE}/report",
                                     params or {"user": "Ada_L"}, body), None)


def test_a_day_preview_spans_every_meeting_in_order(day):
    mp, _ = day
    _rows(mp, ROWS)
    res = _preview()
    assert res["statusCode"] == 200
    b = json.loads(res["body"])
    assert b["scope"] == "day"
    assert [t["topic_title"] for t in b["topics"]] == [
        "Early in first meeting", "Late in first meeting", "Second meeting"]
    assert b["sessionIds"] == [S1300, S1405]
    assert b["siteNames"] == ["UC PK"]
    assert b["title"] == f"{DATE} · UC PK"
    assert b["fieldDefaults"]["site"] == "UC PK"


def test_a_day_with_nothing_reportable_says_so(day):
    mp, _ = day
    _rows(mp, [_row(id="t-p", work_class="non_work")])
    res = _preview()
    assert res["statusCode"] == 404
    assert json.loads(res["body"])["error"] == "nothing to report for that day"


def test_a_bad_date_is_a_400(day):
    mp, _ = day
    _rows(mp, ROWS)
    res = org.lambda_handler(_event("POST", "/api/org/days/nope/report/preview", {"user": "Ada_L"}), None)
    assert res["statusCode"] == 400


def test_an_empty_selection_is_refused(day):
    mp, _ = day
    _rows(mp, ROWS)
    assert _preview(body={"topicRowIds": []})["statusCode"] == 400


def test_a_selection_narrows_the_day(day):
    mp, _ = day
    _rows(mp, ROWS)
    b = json.loads(_preview(body={"topicRowIds": ["t-second", "t-personal"]})["body"])
    assert [t["topic_title"] for t in b["topics"]] == ["Second meeting"]
    assert b["sessionIds"] == [S1405]


def test_generate_enqueues_a_day_artifact_under_the_day_segment(day):
    mp, puts = day
    _rows(mp, ROWS)
    res = _generate({"deliver": "download"})
    assert res["statusCode"] == 202
    b = json.loads(res["body"])
    assert b["scope"] == "day" and re.fullmatch(r"[0-9a-f]{32}", b["requestId"])
    assert b["resultKey"] == f"session_report_results/Ada_L/{DATE}/day/{b['requestId']}.json"
    assert len(puts) == 1
    assert puts[0]["Key"] == f"session_report_requests/Ada_L/{DATE}/day/{b['requestId']}.json"
    art = json.loads(puts[0]["Body"])
    assert art["scope"] == "day"
    assert art["sessionIds"] == [S1300, S1405]
    assert art["mirrorFolders"] == ["Ada_L"]
    assert art["resultKey"] == b["resultKey"]
    assert "sessionId" not in art


def test_email_without_recipients_is_refused_and_nothing_is_enqueued(day):
    mp, puts = day
    _rows(mp, ROWS)
    assert _generate({"deliver": "email"})["statusCode"] == 400
    assert puts == []


def test_a_failed_tombstone_lookup_enqueues_nothing(day):
    mp, puts = day
    _rows(mp, ROWS)

    def boom(conn, folder=None, date=None):
        raise RuntimeError("redactions unreadable")
    mp.setattr(org.redactions, "deleted_source_prefixes", boom)
    assert _generate({"deliver": "download"})["statusCode"] == 500
    assert puts == []


def test_the_folder_gate_is_the_media_one(day):
    mp, puts = day
    _rows(mp, ROWS)
    seen = {}

    def gate(conn, caller, user, what="media"):
        seen["what"] = what
        return None, org.error("not permitted to view this user's day report", 403)
    mp.setattr(org, "_resolve_org_media_folder", gate)
    assert _generate({"deliver": "download"})["statusCode"] == 403
    assert seen["what"] == "day report" and puts == []
