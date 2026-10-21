"""Integration: Task 5 (extractor-declares-item-continuity plan, 2026-09-30) -- the item
writer stores the extractor's item_id on all four child tables, every
list_for_carry_forward returns it, and malformed or duplicated ids are cleaned to NULL
before insert.

Two layers, against a real Postgres:

  * Repository-level (the `db` fixture -- rolled back): drives each of the four child
    repos' insert function directly with one item_id-bearing child and one without, then
    reads item_id back both straight off the table and through list_for_carry_forward.

  * End-to-end (a real committed connection, mirrors
    tests/integration/test_supersede_two_passes.py's own harness -- the writer's
    item_continuity.clean_item_ids call needs to run for real, not stubbed): drives
    lambda_item_writer.write_extraction_items on an extraction whose two action items
    share one item_id and whose finding's item_id is not a UUID at all, and proves every
    one of the three rows lands with item_id NULL while the pass still commits.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import io
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import (action_items, companies, findings, memberships, sites,
                          topic_decisions, topic_questions, topics, users)

import lambda_item_writer

pytestmark = pytest.mark.integration

DATE = "2026-09-30"

ITEM_ID_1 = "11111111-1111-4111-8111-111111111111"
ITEM_ID_2 = "22222222-2222-4222-8222-222222222222"


def _seed(db, tag_suffix=""):
    tag = uuid.uuid4().hex[:8] + tag_suffix
    co = companies.create_company(db, f"ItemId-Co-{tag}")
    site = sites.create_site(db, co["id"], f"ItemId-Site-{tag}")
    user = users.upsert_field_only_user(db, co["id"], f"ItemIdFolder-{tag}", "Fol", "Der",
                                        "worker")
    return co, site, user


def _topic(db, site, user, title):
    return topics.upsert_topic(db, site["id"], DATE, title, user_id=user["id"],
                               source_s3_key=f"extractions/x/{DATE}/{uuid.uuid4().hex}.json")


# ---------------------------------------------------------------------------
# Repository-level: each child table's insert + list_for_carry_forward
# ---------------------------------------------------------------------------

def test_action_items_store_and_return_item_id(db):
    co, site, user = _seed(db)
    topic = topics.upsert_topic(
        db, site["id"], DATE, "T", user_id=user["id"],
        source_s3_key=f"extractions/x/{DATE}/{uuid.uuid4().hex}.json",
        action_items=[{"text": "With id", "item_id": ITEM_ID_1},
                      {"text": "Without id"}])

    rows = db.execute(
        "SELECT text, item_id FROM action_items WHERE topic_id=%s ORDER BY text",
        (topic["id"],)).fetchall()
    by_text = {r[0]: r[1] for r in rows}
    assert str(by_text["With id"]) == ITEM_ID_1
    assert by_text["Without id"] is None

    carry_rows = action_items.list_for_carry_forward(db, [topic["id"]], site["id"])
    assert "item_id" in carry_rows[0]
    by_text = {r["text"]: r["item_id"] for r in carry_rows}
    assert str(by_text["With id"]) == ITEM_ID_1
    assert by_text["Without id"] is None


def test_findings_store_and_return_item_id(db):
    co, site, user = _seed(db)
    topic = _topic(db, site, user, "T")

    findings.insert_findings(db, topic["id"], site["id"], [
        {"observation": "With id", "domain": "safety", "severity": "minor",
         "item_id": ITEM_ID_1},
        {"observation": "Without id", "domain": "safety", "severity": "minor"},
    ])

    rows = db.execute(
        "SELECT observation, item_id FROM findings WHERE topic_id=%s ORDER BY observation",
        (topic["id"],)).fetchall()
    by_text = {r[0]: r[1] for r in rows}
    assert str(by_text["With id"]) == ITEM_ID_1
    assert by_text["Without id"] is None

    carry_rows = findings.list_for_carry_forward(db, [topic["id"]], site["id"])
    assert "item_id" in carry_rows[0]
    by_text = {r["text"]: r["item_id"] for r in carry_rows}
    assert str(by_text["With id"]) == ITEM_ID_1
    assert by_text["Without id"] is None


def test_topic_decisions_store_and_return_item_id(db):
    co, site, user = _seed(db)
    topic = _topic(db, site, user, "T")

    topic_decisions.insert_decisions(db, topic["id"], site["id"], [
        {"decision": "With id", "item_id": ITEM_ID_1},
        {"decision": "Without id"},
    ])

    rows = db.execute(
        "SELECT decision, item_id FROM topic_decisions WHERE topic_id=%s ORDER BY decision",
        (topic["id"],)).fetchall()
    by_text = {r[0]: r[1] for r in rows}
    assert str(by_text["With id"]) == ITEM_ID_1
    assert by_text["Without id"] is None

    carry_rows = topic_decisions.list_for_carry_forward(db, [topic["id"]], site["id"])
    assert "item_id" in carry_rows[0]
    by_text = {r["text"]: r["item_id"] for r in carry_rows}
    assert str(by_text["With id"]) == ITEM_ID_1
    assert by_text["Without id"] is None


def test_topic_questions_store_and_return_item_id(db):
    co, site, user = _seed(db)
    topic = _topic(db, site, user, "T")

    topic_questions.insert_questions(db, topic["id"], site["id"], [
        {"question": "With id", "item_id": ITEM_ID_1},
        {"question": "Without id"},
    ])

    rows = db.execute(
        "SELECT question, item_id FROM topic_questions WHERE topic_id=%s ORDER BY question",
        (topic["id"],)).fetchall()
    by_text = {r[0]: r[1] for r in rows}
    assert str(by_text["With id"]) == ITEM_ID_1
    assert by_text["Without id"] is None

    carry_rows = topic_questions.list_for_carry_forward(db, [topic["id"]], site["id"])
    assert "item_id" in carry_rows[0]
    by_text = {r["text"]: r["item_id"] for r in carry_rows}
    assert str(by_text["With id"]) == ITEM_ID_1
    assert by_text["Without id"] is None


# ---------------------------------------------------------------------------
# End-to-end: malformed / duplicated item_ids are cleaned to NULL before insert
# ---------------------------------------------------------------------------

class _FakeS3:
    """Same minimal double as test_supersede_two_passes.py's own -- integration tests in
    this repo do not import from tests/unit."""

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


def _dup_and_malformed_extraction():
    shared_id = ITEM_ID_1  # worn by TWO action items -- must be nulled on BOTH
    return {
        "schema_version": 1,
        "tier": "final",
        "extracted_at": "2026-09-30T10:00:00Z",
        "topics": [{
            "topic_title": "Duplicate and malformed item_id",
            "category": "progress",
            "summary": "summary",
            "time_range": "10:00 – 10:05",
            "participants": [],
            "action_items": [
                {"action": "Order rebar", "item_id": shared_id},
                {"action": "Order timber", "item_id": shared_id},
            ],
            "findings": [
                {"observation": "Missing guardrail", "domain": "safety", "severity": "minor",
                 "item_id": "nope"},
            ],
            "safety_flags": [],
        }],
    }


def test_duplicate_and_malformed_item_ids_are_stored_as_null(monkeypatch, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = extraction_key = None
    try:
        company_name = f"ItemIdWriter-Co-{tag}"
        co = companies.create_company(seed, company_name)
        site = sites.create_site(seed, co["id"], f"ItemIdWriter-Site-{tag}")
        folder = f"ItemIdWriter-{tag}"
        user = users.upsert_field_only_user(seed, co["id"], folder, "Fol", "Der", "worker")
        memberships.add_membership(seed, user["id"], site["id"], "worker")

        session_base = f"sid{tag}"
        extraction_key = f"extractions/{folder}/{DATE}/{session_base}.json"
        fake_s3 = _FakeS3({extraction_key: json.dumps(_dup_and_malformed_extraction())})

        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)

        result = lambda_item_writer.write_extraction_items(DATE, folder, extraction_key)
        assert result == {"skipped": False, "topics": 1}, result

        action_rows = seed.execute(
            "SELECT a.text, a.item_id FROM action_items a JOIN topics t ON t.id=a.topic_id "
            "WHERE t.source_s3_key=%s ORDER BY a.text", (extraction_key,)).fetchall()
        assert [r[0] for r in action_rows] == ["Order rebar", "Order timber"]
        assert [r[1] for r in action_rows] == [None, None], (
            "an item_id worn by more than one child must be NULL on EVERY copy")

        finding_row = seed.execute(
            "SELECT f.observation, f.item_id FROM findings f JOIN topics t ON t.id=f.topic_id "
            "WHERE t.source_s3_key=%s", (extraction_key,)).fetchone()
        assert finding_row[0] == "Missing guardrail"
        assert finding_row[1] is None, "a malformed item_id must be stored as NULL, not raise"
    finally:
        co_id = co["id"] if co is not None else None
        site_id = site["id"] if site is not None else None
        user_id = user["id"] if user is not None else None
        if site_id is not None:
            seed.execute("DELETE FROM sites WHERE id=%s", (site_id,))
        if user_id is not None:
            seed.execute("DELETE FROM users WHERE id=%s", (user_id,))
        if co_id is not None:
            seed.execute("DELETE FROM companies WHERE id=%s", (co_id,))
        if co_id is not None:
            remaining = seed.execute(
                "SELECT "
                "(SELECT count(*) FROM companies WHERE id=%s), "
                "(SELECT count(*) FROM sites WHERE id=%s), "
                "(SELECT count(*) FROM users WHERE id=%s), "
                "(SELECT count(*) FROM memberships WHERE site_id=%s), "
                "(SELECT count(*) FROM topics WHERE site_id=%s)",
                (co_id, site_id, user_id, site_id, site_id),
            ).fetchone()
            assert remaining == (0, 0, 0, 0, 0), f"leaked rows after cleanup: {remaining}"
        seed.close()
