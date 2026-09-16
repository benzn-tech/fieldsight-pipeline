"""org-api decides WHAT a report covers; the worker decides how it reads. The artifact
is the whole contract between them, so this pins the three keys generation needs."""
import json

import pytest

import lambda_org_api as org

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
DATE = "2026-07-25"
S1300 = "Benl1_2026-07-25_13-00-11"
KEY_1300 = f"extractions/Ada_L/{DATE}/{S1300}.json"
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
