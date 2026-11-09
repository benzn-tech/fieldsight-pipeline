"""Integration: item-writer's status-reconcile request (R0, Task 4) against a real Postgres.

Two properties a fake connection cannot show:

* a failing candidate lookup must not abort the item write's transaction. Postgres aborts
  the whole transaction on any statement error, so catching the exception in Python is not
  enough -- the lookup needs its own savepoint, or the commit silently rolls back every row
  this pass wrote;
* the real find_items, given the arguments item-writer passes, returns the recorder's open
  item from an EARLIER session that day and never this session's own.

Same committed-connection harness as test_item_writer_event_links.py. All text is synthetic.
Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import io
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration

DATE = "2026-11-10"

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


def _artifact(extracted_at, action_text):
    return {
        "schema_version": 1, "tier": "final", "extracted_at": extracted_at,
        "topics": [{
            "topic_title": "Synthetic topic", "category": "progress", "summary": "s",
            "time_range": "10:00 – 10:05", "participants": [], "safety_flags": [],
            "action_items": [{"action": action_text}],
        }],
    }


@pytest.fixture
def world(monkeypatch, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    made = {}
    try:
        made["co"] = companies.create_company(seed, f"Recon-Co-{tag}")
        made["site"] = sites.create_site(seed, made["co"]["id"], f"Recon-Site-{tag}")
        folder = f"ReconW-{tag}"
        made["user"] = users.upsert_field_only_user(seed, made["co"]["id"], folder,
                                                    "Fol", "Der", "worker")
        memberships.add_membership(seed, made["user"]["id"], made["site"]["id"], "worker")
        fake_s3 = _FakeS3({})
        emitted = []
        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)
        monkeypatch.setattr(lambda_item_writer, "RECONCILE_STATUS", True)
        monkeypatch.setattr(lambda_item_writer.reconcile_request, "emit",
                            lambda s3, bucket, body: emitted.append(body) or "k")

        def write(artifact):
            key = f"extractions/{folder}/{DATE}/sid{uuid.uuid4().hex}.json"
            fake_s3.objects[key] = json.dumps(artifact)
            return key, lambda_item_writer.write_extraction_items(DATE, folder, key)

        yield {"seed": seed, "write": write, "emitted": emitted, "folder": folder, **made}
    finally:
        if "site" in made:
            seed.execute("DELETE FROM sites WHERE id=%s", (made["site"]["id"],))
        if "user" in made:
            seed.execute("DELETE FROM users WHERE id=%s", (made["user"]["id"],))
        if "co" in made:
            seed.execute("DELETE FROM companies WHERE id=%s", (made["co"]["id"],))
        seed.close()


def _live_actions(w, key):
    return [r[0] for r in w["seed"].execute(
        "SELECT a.text FROM action_items a JOIN topics t ON t.id = a.topic_id "
        "WHERE t.source_s3_key = %s AND t.superseded_at IS NULL", (key,)).fetchall()]


def test_find_items_error_does_not_abort_item_write(world, monkeypatch):
    def broken_find_items(conn, **kw):
        conn.execute("SELECT * FROM no_such_table_reconcile_r0")
    monkeypatch.setattr(lambda_item_writer.reconcile_request.item_history, "find_items",
                        broken_find_items)

    key, res = world["write"](_artifact("2026-11-10T10:05:00Z", "Synthetic task one"))

    assert res == {"skipped": False, "topics": 1}, res
    # Read on a separate connection: the rows are only here if the transaction COMMITTED.
    assert _live_actions(world, key) == ["Synthetic task one"]
    assert world["emitted"] == []


def test_earlier_sessions_open_item_is_offered_and_this_sessions_is_not(world):
    first_key, res1 = world["write"](_artifact("2026-11-10T09:05:00Z", "Synthetic earlier task"))
    assert res1["skipped"] is False
    # The first session found nothing (it is the only one) and so emitted nothing.
    assert world["emitted"] == []

    second_key, res2 = world["write"](_artifact("2026-11-10T11:05:00Z", "Synthetic later task"))
    assert res2["skipped"] is False

    (body,) = world["emitted"]
    assert body["extraction_key"] == second_key
    assert body["site_id"] == str(world["site"]["id"])
    texts = [c["text"] for c in body["candidates"]]
    assert texts == ["Synthetic earlier task"]
    cand = body["candidates"][0]
    assert cand["first_seen_date"] == DATE and cand["recorded_by"] == world["folder"]
    assert isinstance(cand["stable_id"], str)
    json.dumps(body)   # JSON-safe as built
