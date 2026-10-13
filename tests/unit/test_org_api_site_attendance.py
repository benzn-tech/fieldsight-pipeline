"""Unit: GET/POST/DELETE /api/org/sites/{id}/attendance — the `manual` roster source
(on-site-roster plan, Task 5).

Harness mirrors test_org_api_compliance.py / test_org_api_speaker_corrections.py: caller and
connection doubles, `org.lambda_handler` driven end to end through the real dispatcher.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
OTHER_SITE_ID = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"

CALLER = {"id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
          "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
          "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-04"}

WORKER = dict(CALLER, id="u-uuid-2", global_role="worker")


def make_event(method, path, sub="sub-1", body=None, params=None):
    return {
        "httpMethod": method, "path": path, "queryStringParameters": params,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}},
    }


def body_of(res):
    return json.loads(res["body"])


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return self


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else dict(WORKER))
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    return monkeypatch


# ---- GET --------------------------------------------------------------------


def test_get_403s_a_site_outside_the_callers_scope(wired):
    res = org.lambda_handler(make_event(
        "GET", f"/api/org/sites/{OTHER_SITE_ID}/attendance", params={"date": "2026-09-30"}),
        None)
    assert res["statusCode"] == 403


def test_get_defaults_the_date_to_nz_today(wired, monkeypatch):
    """The client omits `date` -- default is nz_time.nz_today(), never
    datetime.now().date() (BUG-37). 2026-09-30 11:30 UTC is already 2026-10-01 in NZDT."""
    import datetime as dt

    class FrozenNow:
        @staticmethod
        def now(tz=None):
            return dt.datetime(2026, 9, 30, 11, 30, tzinfo=dt.timezone.utc)

    monkeypatch.setattr(org.nz_time, "datetime", FrozenNow)
    seen = {}
    monkeypatch.setattr(org.site_attendance, "for_day",
                        lambda conn, co, sid, date: seen.update(date=date) or [])
    res = org.lambda_handler(
        make_event("GET", f"/api/org/sites/{SITE_ID}/attendance"), None)
    assert res["statusCode"] == 200, body_of(res)
    assert seen["date"] == "2026-10-01"
    assert body_of(res)["date"] == "2026-10-01"


def test_get_any_member_may_read(wired, monkeypatch):
    monkeypatch.setattr(org.site_attendance, "for_day", lambda conn, co, sid, date: [])
    res = org.lambda_handler(make_event(
        "GET", f"/api/org/sites/{SITE_ID}/attendance", sub="sub-worker",
        params={"date": "2026-09-30"}), None)
    assert res["statusCode"] == 200, body_of(res)


def test_get_lists_rows_with_resolved_identity(wired, monkeypatch):
    monkeypatch.setattr(org.site_attendance, "for_day", lambda conn, co, sid, date: [
        {"id": "row-1", "display_name": "Sam Yu", "employer_name": "Cassidy",
         "user_id": None, "voiceprint_id": "vp-1", "source": "manual",
         "source_ref": "sam yu", "first_seen_at": None, "last_seen_at": None}])
    res = org.lambda_handler(make_event(
        "GET", f"/api/org/sites/{SITE_ID}/attendance", params={"date": "2026-09-30"}), None)
    row = body_of(res)["rows"][0]
    assert row["displayName"] == "Sam Yu" and row["employerName"] == "Cassidy"
    assert row["source"] == "manual"
    assert row["resolved"] == {"userId": None, "voiceprintId": "vp-1"}


def test_get_rejects_a_malformed_date(wired):
    res = org.lambda_handler(make_event(
        "GET", f"/api/org/sites/{SITE_ID}/attendance", params={"date": "30-09-2026"}), None)
    assert res["statusCode"] == 400


# ---- POST -------------------------------------------------------------------


def test_post_requires_a_correction_role(wired):
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sites/{SITE_ID}/attendance", sub="sub-worker",
        body={"date": "2026-09-30", "names": [{"displayName": "Sam Yu"}]}), None)
    assert res["statusCode"] == 403


def test_post_403s_a_site_outside_scope(wired):
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sites/{OTHER_SITE_ID}/attendance",
        body={"date": "2026-09-30", "names": [{"displayName": "Sam Yu"}]}), None)
    assert res["statusCode"] == 403


def test_post_rejects_a_blank_name(wired):
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sites/{SITE_ID}/attendance",
        body={"date": "2026-09-30", "names": [{"displayName": "   "}]}), None)
    assert res["statusCode"] == 400


def test_post_rejects_a_bad_date(wired):
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sites/{SITE_ID}/attendance",
        body={"date": "30/09/2026", "names": [{"displayName": "Sam Yu"}]}), None)
    assert res["statusCode"] == 400


def test_post_rejects_more_than_fifty_names(wired):
    names = [{"displayName": f"Person {i}"} for i in range(51)]
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sites/{SITE_ID}/attendance",
        body={"date": "2026-09-30", "names": names}), None)
    assert res["statusCode"] == 400


def test_post_upserts_as_source_manual_and_returns_the_listing(wired, monkeypatch):
    captured = {}

    def fake_upsert(conn, company_id, site_id, attend_date, rows, source, created_by=None):
        captured.update(company_id=company_id, site_id=site_id, attend_date=attend_date,
                        rows=rows, source=source, created_by=created_by)
        return {"inserted": 1, "updated": 0}

    monkeypatch.setattr(org.site_attendance, "upsert", fake_upsert)
    monkeypatch.setattr(org.site_attendance, "for_day", lambda conn, co, sid, date: [
        {"id": "row-1", "display_name": "Sam Yu", "employer_name": None,
         "user_id": None, "voiceprint_id": None, "source": "manual",
         "source_ref": "sam yu", "first_seen_at": None, "last_seen_at": None}])
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sites/{SITE_ID}/attendance",
        body={"date": "2026-09-30",
             "names": [{"displayName": "Sam Yu", "employerName": "Cassidy"}]}), None)
    assert res["statusCode"] == 200, body_of(res)
    body = body_of(res)
    assert body["inserted"] == 1 and body["updated"] == 0
    assert body["rows"][0]["displayName"] == "Sam Yu"
    assert captured["source"] == "manual"
    assert captured["company_id"] == "c-uuid-1", "company comes from the caller, never the body"


# ---- DELETE -------------------------------------------------------------------


def test_delete_requires_a_correction_role(wired):
    res = org.lambda_handler(make_event(
        "DELETE", f"/api/org/sites/{SITE_ID}/attendance/row-1", sub="sub-worker",
        params={"date": "2026-09-30"}), None)
    assert res["statusCode"] == 403


def test_delete_refuses_a_non_manual_row(wired, monkeypatch):
    monkeypatch.setattr(org.site_attendance, "for_day", lambda conn, co, sid, date: [
        {"id": "row-1", "display_name": "Sam Yu", "employer_name": None,
         "user_id": None, "voiceprint_id": None, "source": "graph_calendar",
         "source_ref": "e1", "first_seen_at": None, "last_seen_at": None}])
    res = org.lambda_handler(make_event(
        "DELETE", f"/api/org/sites/{SITE_ID}/attendance/row-1",
        params={"date": "2026-09-30"}), None)
    assert res["statusCode"] == 409


def test_delete_removes_a_manual_row(wired, monkeypatch):
    monkeypatch.setattr(org.site_attendance, "for_day", lambda conn, co, sid, date: [
        {"id": "row-1", "display_name": "Sam Yu", "employer_name": None,
         "user_id": None, "voiceprint_id": None, "source": "manual",
         "source_ref": "sam yu", "first_seen_at": None, "last_seen_at": None}])
    monkeypatch.setattr(org.site_attendance, "remove",
                        lambda conn, co, sid, date, row_id: 1)
    res = org.lambda_handler(make_event(
        "DELETE", f"/api/org/sites/{SITE_ID}/attendance/row-1",
        params={"date": "2026-09-30"}), None)
    assert res["statusCode"] == 200, body_of(res)
    assert body_of(res)["removed"] == 1


def test_delete_rejects_a_missing_date(wired):
    res = org.lambda_handler(make_event(
        "DELETE", f"/api/org/sites/{SITE_ID}/attendance/row-1"), None)
    assert res["statusCode"] == 400
