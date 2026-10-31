"""Company offboarding, Task 9 (spec 2026-11-01 §4, ruling R6): a late upload of a
suspended / exited company is still extracted -- item-writer writes its topics and
action items with the flag ON. Only email and generated reports are suppressed.

Committed connection, as the item-writer harness in test_project_owned_capture.py
(item-writer opens its own connection). The company is put back to 'active' afterwards
so no inactive company lingers for the choke-point tests that share this database.
"""
import io
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, meeting_session, sites, users

pytestmark = pytest.mark.integration

DATE = "2026-11-01"


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}

    def get_paginator(self, op):
        objs = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in objs if k.startswith(Prefix)]}
        return P()


def _extraction():
    return {"schema_version": 1, "tier": "final", "extracted_at": "2026-11-01T10:30:00Z",
            "topics": [{"topic_title": "Slab pour", "category": "progress",
                        "summary": "poured the level 2 slab", "time_range": "10:00 – 10:05",
                        "participants": [], "safety_flags": [],
                        "action_items": [{"action": "Book the pump for Friday",
                                          "responsible": "Eve", "deadline": None}]}]}


@pytest.fixture
def suspended_world(migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = companies.create_company(seed, f"Late-Co-{tag}")
    site = sites.create_site(seed, co["id"], f"Late-Site-{tag}")
    folder = f"Late-Eve-{tag}"
    eve = users.upsert_field_only_user(seed, co["id"], folder, "Eve", "Xu", "worker")
    session_base = "sid" + (tag + "0" * 32)[:32]
    meeting_session.ensure_open(seed, session_base[3:], co["id"], eve["id"], site["id"],
                                "audio", None)
    seed.execute("UPDATE companies SET status = 'suspended' WHERE id = %s", (co["id"],))
    key = f"extractions/{folder}/{DATE}/{session_base}.json"
    try:
        yield seed, co, site, folder, key
    finally:
        seed.execute("UPDATE companies SET status = 'active' WHERE id = %s", (co["id"],))
        seed.close()


def test_a_suspended_companys_late_upload_still_writes_topics_and_actions(
        monkeypatch, migrated_db_url, suspended_world):
    iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg")
    import tenancy_status
    seed, co, site, folder, key = suspended_world
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    assert tenancy_status.company_status(seed, co["id"]) == "suspended"
    monkeypatch.setattr(iw, "_s3_client", _FakeS3({key: json.dumps(_extraction())}))
    monkeypatch.setattr(iw, "get_connection", lambda *a, **k: get_connection(migrated_db_url))
    monkeypatch.setattr(iw.match_request, "emit", lambda *a, **k: None)
    monkeypatch.setattr(iw, "_request_match", lambda *a, **k: None)

    result = iw.write_extraction_items(DATE, folder, key)

    assert result == {"skipped": False, "topics": 1}, result
    topic = seed.execute("SELECT id, site_id FROM topics WHERE source_s3_key = %s",
                         (key,)).fetchone()
    assert topic is not None and str(topic[1]) == str(site["id"])
    actions = seed.execute("SELECT text FROM action_items WHERE topic_id = %s",
                           (topic[0],)).fetchall()
    assert [a[0] for a in actions] == ["Book the pump for Friday"]
