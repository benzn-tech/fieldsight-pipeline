"""Integration: task numbers through the REAL write paths (action packs Task 1, spec §14 D1).

  * lambda_item_writer.write_extraction_items, twice on one key (live then final pass): the
    carried stable_id keeps its number, a new item takes the next one, nothing is renumbered.
  * lambda_ingest.ingest_report: report-sourced items are numbered too.
  * The deploy window (sam deploy runs before migrate): with action_task_numbers missing,
    the writer still writes the extraction unchanged.

Real committed connections (not the `db` fixture -- its rollback would hide the first pass
from the second). Harness copied from test_question_answered_survives.py /
test_ingest_carries_forward.py ("integration tests in this repo do not import from
tests/unit"). Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import io
import json
import os
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration

DATE = "2026-11-01"

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")
lambda_ingest = pytest.importorskip(
    "lambda_ingest", reason="requires psycopg (installed in CI)")

MIGRATION_0093 = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "migrations",
    "0093_action_task_numbers.sql")


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


def _extraction(tier, extracted_at, texts):
    return {"schema_version": 1, "tier": tier, "extracted_at": extracted_at, "topics": [{
        "topic_title": "Deliveries", "category": "progress", "summary": "s",
        "time_range": "10:00 – 10:05", "participants": [],
        "action_items": [{"action": t} for t in texts], "safety_flags": []}]}


@pytest.fixture
def world(migrated_db_url):
    seed = get_connection(migrated_db_url, autocommit=True)
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(seed, f"TNW-Co-{tag}")
    site_name = f"TNW-Site-{tag}"
    site = sites.create_site(seed, co["id"], site_name)
    folder = f"TNW-{tag}"
    user = users.upsert_field_only_user(seed, co["id"], folder, "Ada", "L", "worker")
    memberships.add_membership(seed, user["id"], site["id"], "worker")
    try:
        yield {"seed": seed, "co": co, "site": site, "site_name": site_name,
               "folder": folder, "tag": tag}
    finally:
        seed.execute("DELETE FROM action_task_numbers WHERE site_id=%s", (site["id"],))
        seed.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
        seed.execute("DELETE FROM users WHERE company_id=%s", (co["id"],))
        seed.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
        seed.close()


def _live_numbers(seed, key):
    """{text: (stable_id, task_no)} for the LIVE rows of `key`."""
    rows = seed.execute(
        "SELECT a.text, a.stable_id, n.task_no FROM action_items a "
        "JOIN topics t ON t.id = a.topic_id "
        "LEFT JOIN action_task_numbers n ON n.stable_id = a.stable_id "
        "WHERE t.source_s3_key=%s AND t.superseded_at IS NULL", (key,)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _wire_writer(monkeypatch, migrated_db_url, fake_s3, on_connect=None):
    pending = [on_connect] if on_connect else []

    def connect(*a, **k):
        conn = get_connection(migrated_db_url)
        if pending:                      # the write transaction only, never a later one
            pending.pop()(conn)
        return conn
    monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
    monkeypatch.setattr(lambda_item_writer, "get_connection", connect)
    monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)


def test_numbers_survive_re_extraction_with_carry(monkeypatch, migrated_db_url, world):
    seed, folder = world["seed"], world["folder"]
    key = f"extractions/{folder}/{DATE}/sid{world['tag']}.json"
    fake_s3 = _FakeS3({key: json.dumps(_extraction(
        "live", "2026-11-01T10:00:00Z", ["Order timber for level 2", "Book the crane"]))})
    _wire_writer(monkeypatch, migrated_db_url, fake_s3)

    assert lambda_item_writer.write_extraction_items(DATE, folder, key)["topics"] == 1
    live = _live_numbers(seed, key)
    assert sorted(n for _s, n in live.values()) == [1, 2]

    # Final pass: same two items (carried by text) plus a new one.
    fake_s3.objects[key] = json.dumps(_extraction(
        "final", "2026-11-01T10:30:00Z",
        ["Order timber for level 2", "Book the crane", "Call the council"]))
    assert lambda_item_writer.write_extraction_items(DATE, folder, key)["topics"] == 1
    final = _live_numbers(seed, key)
    for text in ("Order timber for level 2", "Book the crane"):
        assert final[text] == live[text], f"{text!r} must keep its stable_id AND its number"
    assert final["Call the council"][1] == 3
    # One ledger row per stable_id, nothing burned on the carried rows.
    assert seed.execute("SELECT count(*), max(task_no) FROM action_task_numbers "
                        "WHERE site_id=%s", (world["site"]["id"],)).fetchone() == (3, 3)


def test_ingest_report_numbers_its_items(monkeypatch, migrated_db_url, world):
    seed, folder = world["seed"], world["folder"]
    report_key = f"reports/{DATE}/{folder}/daily_report.json"
    report = {"report_date": DATE, "user_name": "Ada L", "site": world["site_name"],
              "topics": [{"topic_id": 0, "time_range": "09:00 – 09:05",
                          "topic_title": "Safety", "category": "safety",
                          "participants": [], "summary": "PPE.", "key_decisions": [],
                          "action_items": [{"action": "Order hard hats"},
                                           {"action": "Fix the gate"}],
                          "safety_flags": []}]}
    monkeypatch.setattr(lambda_ingest, "_s3_client", _FakeS3({report_key: json.dumps(report)}))
    monkeypatch.setattr(lambda_ingest, "get_connection",
                        lambda *a, **k: get_connection(migrated_db_url))
    monkeypatch.setattr(lambda_ingest, "_load_vectors", lambda bucket, sidecar_key: {})
    monkeypatch.setattr(lambda_ingest, "embed_from_sidecar",
                        lambda text, vectors: "[" + ",".join(["0.0"] * 1024) + "]")
    monkeypatch.setattr(lambda_ingest, "_load_turns", lambda user_folder, date: [])
    monkeypatch.setattr(lambda_ingest.match_request, "emit", lambda *a, **k: None)

    assert lambda_ingest.ingest_report(DATE, folder, report_key)["topics"] == 1
    first = _live_numbers(seed, report_key)
    assert sorted(n for _s, n in first.values()) == [1, 2]
    # Re-ingest: carried by text, numbers unchanged.
    assert lambda_ingest.ingest_report(DATE, folder, report_key)["topics"] == 1
    assert _live_numbers(seed, report_key) == first


def test_writer_survives_the_deploy_window_without_the_table(monkeypatch, migrated_db_url,
                                                             world, caplog):
    """sam deploy runs before migrate: the new code meets a database without 0093. The
    extraction must still be written, unchanged. The table is hidden by a rename committed
    by the writer's own transaction, and restored afterwards."""
    seed, folder = world["seed"], world["folder"]
    hidden = f"action_task_numbers_hidden_{world['tag']}"
    key = f"extractions/{folder}/{DATE}/sid{world['tag']}.json"
    fake_s3 = _FakeS3({key: json.dumps(_extraction(
        "live", "2026-11-01T10:00:00Z", ["Order timber", "Book the crane"]))})
    _wire_writer(monkeypatch, migrated_db_url, fake_s3,
                 on_connect=lambda c: c.execute(
                     f"ALTER TABLE action_task_numbers RENAME TO {hidden}"))
    caplog.set_level("INFO")
    try:
        result = lambda_item_writer.write_extraction_items(DATE, folder, key)
        assert result == {"skipped": False, "topics": 1}, result
    finally:
        if seed.execute("SELECT to_regclass(%s)", (hidden,)).fetchone()[0] is not None:
            seed.execute(f"ALTER TABLE {hidden} RENAME TO action_task_numbers")
    rows = _live_numbers(seed, key)
    assert set(rows) == {"Order timber", "Book the crane"}
    assert all(n is None for _s, n in rows.values()), "nothing numbered without the table"
    assert "task numbers: table not migrated yet" in caplog.text
