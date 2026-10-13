"""Unit: the serialized /timeline 200 carries each to-do's version.

Driven through `_render_timeline_for_user` with the REAL render_report_shape, so
the assertion is on the JSON body a browser receives rather than on repository
output -- the layer where mention_count was once silently dropped.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
DATE, USER = "2026-09-01", "Ada_L"
CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
    "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}
ROW = {
    "id": "t-1", "site_id": SITE_ID, "site_name": "Alpha", "user_name": "Ada L",
    "source_s3_key": f"extractions/{USER}/{DATE}/sid" + "a" * 32 + ".json",
    "category": "progress", "title": "Slab", "summary": "s", "time_range": None,
    "participants": [], "safety_observations": [], "findings": [], "photos": [],
    "action_items": [{"id": "a-1", "text": "Order timber", "responsible": None,
                      "deadline": None, "deadline_text": None, "priority": None,
                      "status": "open", "edit_count": 1}],
}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_timeline_body_carries_version(monkeypatch):
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: prefix.startswith("extractions/"))
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix, **kw: [dict(ROW)])
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org, "_get_lake_json", lambda key: None)
    monkeypatch.setattr(org.redactions, "list_active_for_topics", lambda conn, ids: {})
    monkeypatch.setattr(org.recordings, "day_stats",
                        lambda conn, company, folder, date: {"sessions": 0, "duration_s": 0})
    monkeypatch.setattr(org.recordings, "photo_list_for_day",
                        lambda conn, company, folder, date: [])
    monkeypatch.setattr(org.redactions, "deleted_photo_keys",
                        lambda conn, company, keys=None: set())

    res = org._render_timeline_for_user(FakeConn(), dict(CALLER), DATE, USER)

    assert res["statusCode"] == 200
    item = json.loads(res["body"])["topics"][0]["action_items"][0]
    assert item["version"] == 2
