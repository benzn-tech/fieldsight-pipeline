"""Integration: Track B final wave (Ruling R19) -- lambda_ingest's report path carries a
human's tick forward across its own re-ingest, against a real Postgres (skipped without
TEST_DATABASE_URL -- a skip is NOT a pass).

Final review Important #1: `ingest_report` supersedes topics on every re-ingest (Track B
Task 3) but had no carry-forward, no decisions/questions dual-write and no orphan metric --
a tick on a report-sourced action item was silently orphaned. This drives the REAL
`lambda_ingest.ingest_report` twice on the same report_key (report-key idempotency, the
source-key supersede this task always runs), with a status tick made between the two passes,
proving the new live row keeps the ticked row's stable_id and status.

Harness copied from tests/integration/test_question_answered_survives.py's own
_FakeS3/_FakePaginator (that file's own docstring: "integration tests in this repo do not
import from tests/unit"), adapted for lambda_ingest -- a real committed connection (not the
`db` fixture -- its rollback would hide the first pass's writes from the second).
"""
import io
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration

DATE = "2026-09-30"

lambda_ingest = pytest.importorskip(
    "lambda_ingest", reason="requires psycopg (installed in CI)")


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


ACTION_TEXT = "Order more hard hats before Friday's delivery"

_CANNED_EMBEDDING = "[" + ",".join(["0.0"] * 1024) + "]"


def _report(site_name, user_name):
    return {
        "report_date": DATE,
        "user_name": user_name,
        "site": site_name,
        "topics": [{
            "topic_id": 0,
            "time_range": "09:00 – 09:05",
            "topic_title": "Safety Briefing",
            "category": "safety",
            "participants": [user_name],
            "summary": "Discussed PPE requirements.",
            "key_decisions": [],
            "action_items": [
                {"action": ACTION_TEXT, "responsible": "Bob", "deadline": "Friday"},
            ],
            "safety_flags": [],
        }],
    }


def test_a_ticked_action_item_survives_the_reports_own_reingest(monkeypatch, migrated_db_url):
    """The defect this task exists to fix: a person ticks a report-sourced action item off
    while the report is re-ingested (a nightly re-run, or a later correction), and the tick
    must land on the NEW row -- by stable_id, carried across the supersession Task 3 proved
    happens underneath, matched by Task 4/5's carry_forward, now wired onto the report path
    (Ruling R19) the exact same way lambda_item_writer's extraction path already was."""
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = None
    try:
        company_name = f"IngestCF-Co-{tag}"
        co = companies.create_company(seed, company_name)
        site_name = f"IngestCF-Site-{tag}"
        site = sites.create_site(seed, co["id"], site_name)
        folder = f"IngestCF-{tag}"
        user_name = "Ada L"
        user = users.upsert_field_only_user(seed, co["id"], folder, "Ada", "L", "worker")
        memberships.add_membership(seed, user["id"], site["id"], "worker")

        report_key = f"reports/{DATE}/{folder}/daily_report.json"
        report = _report(site_name, user_name)
        fake_s3 = _FakeS3({report_key: json.dumps(report)})

        monkeypatch.setattr(lambda_ingest, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_ingest, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_ingest, "_load_vectors", lambda bucket, sidecar_key: {})
        monkeypatch.setattr(lambda_ingest, "embed_from_sidecar",
                            lambda text, vectors: _CANNED_EMBEDDING)
        monkeypatch.setattr(lambda_ingest, "_load_turns", lambda user_folder, date: [])
        monkeypatch.setattr(lambda_ingest.match_request, "emit", lambda *a, **k: None)

        result_1 = lambda_ingest.ingest_report(DATE, folder, report_key)
        assert result_1["skipped"] is False and result_1["topics"] == 1, result_1

        live_item = seed.execute(
            "SELECT a.id, a.stable_id, a.status FROM action_items a "
            "JOIN topics t ON t.id = a.topic_id "
            "WHERE t.source_s3_key=%s AND a.text=%s",
            (report_key, ACTION_TEXT)).fetchone()
        assert live_item is not None, "the first ingest must have written the action item"
        live_id, live_stable_id, live_status = live_item
        assert live_status == "open"

        # Tick it off, directly (the dispatch's own acceptance shape: status='done',
        # updated_by set) -- proving the DB round-trip, not any particular PATCH endpoint's
        # authority ladder (action_items are addressed by durable id there, not stable_id;
        # topic_questions' PATCH endpoint is what test_question_answered_survives.py proves
        # for the stable_id-addressed shape).
        seed.execute(
            "UPDATE action_items SET status='done', updated_by=%s WHERE id=%s",
            (user["id"], live_id))

        # Re-ingest the SAME report_key, SAME action item text -- report-key idempotency
        # (Task 3's supersede_topics_for_source, which runs on EVERY report re-ingest,
        # flip or not) retires the row just ticked and writes a fresh one.
        result_2 = lambda_ingest.ingest_report(DATE, folder, report_key)
        assert result_2["skipped"] is False and result_2["topics"] == 1, result_2

        new_item = seed.execute(
            "SELECT a.id, a.stable_id, a.carried_from, a.status, a.updated_by "
            "FROM action_items a JOIN topics t ON t.id = a.topic_id "
            "WHERE t.source_s3_key=%s AND t.superseded_at IS NULL AND a.text=%s",
            (report_key, ACTION_TEXT)).fetchone()
        assert new_item is not None, "the re-ingest must have written the new live row"
        new_id, new_stable_id, new_carried_from, new_status, new_updated_by = new_item
        assert new_id != live_id, "must be the NEW row, not the old (now superseded) one"
        assert new_stable_id == live_stable_id, (
            "the ticked action item's stable_id must survive the report's own re-ingest")
        assert new_carried_from == live_id
        assert new_status == "done", "the tick must survive the re-ingest"
        assert str(new_updated_by) == str(user["id"])

        topic_counts = seed.execute(
            "SELECT count(*) FILTER (WHERE superseded_at IS NULL), "
            "count(*) FILTER (WHERE superseded_at IS NOT NULL) "
            "FROM topics WHERE source_s3_key=%s", (report_key,)).fetchone()
        assert topic_counts == (1, 1), (
            "the first pass's topic must be SUPERSEDED, not deleted, and exactly one live "
            "topic must remain", topic_counts)
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
                "(SELECT count(*) FROM users WHERE company_id=%s), "
                "(SELECT count(*) FROM topics WHERE site_id=%s), "
                "(SELECT count(*) FROM action_items WHERE site_id=%s), "
                "(SELECT count(*) FROM report_chunks WHERE site_id=%s)",
                (co_id, site_id, co_id, site_id, site_id, site_id),
            ).fetchone()
            assert remaining == (0, 0, 0, 0, 0, 0), f"leaked rows after cleanup: {remaining}"
        seed.close()
