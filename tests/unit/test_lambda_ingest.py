"""
Tests for src/lambda_ingest.py — Phase 4a ingest lambda (TDD).

Style mirrors tests/unit/test_lambda_org_api.py: FakeConn + monkeypatch on
module-level boto3 client globals (_s3_client / _bedrock_client) and on the
repositories/chunking/transcript_utils functions the handler calls.
"""
import io
import json

import pytest

ing = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeS3:
    """Minimal S3 client double: object store keyed by S3 key."""

    def __init__(self, objects=None):
        self.objects = objects or {}

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
        contents = [{"Key": k} for k in self.objects if k.startswith(Prefix)]
        yield {"Contents": contents}


REPORT_KEY = "reports/2026-03-02/Jarley_Trainor/daily_report.json"


def make_report(**overrides):
    report = {
        "report_date": "2026-03-02",
        "user_name": "Jarley Trainor",
        "site": "Test Site",
        "topics": [{
            "topic_id": 0,
            "time_range": "09:00 – 09:05",
            "topic_title": "Safety Briefing",
            "category": "safety",
            "participants": ["Jarley Trainor"],
            "summary": "Discussed PPE requirements.",
            "key_decisions": [],
            "action_items": [
                {"action": "Order more hard hats", "responsible": "Bob", "deadline": "Friday"}
            ],
            "safety_flags": [
                {"risk_level": "medium", "observation": "Missing barrier tape",
                 "recommended_action": "Install tape"}
            ],
        }],
    }
    report.update(overrides)
    return report


@pytest.fixture
def wired(monkeypatch):
    """Common wiring: FakeConn, a resolvable site+company, no transcripts,
    and inert repo writes. Individual tests override as needed."""
    monkeypatch.setattr(ing, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(ing, "_s3_client", FakeS3({REPORT_KEY: json.dumps(make_report())}))
    monkeypatch.setattr(ing.companies, "get_company_by_name",
                        lambda conn, name: {"id": "co-1", "name": name})
    monkeypatch.setattr(ing.sites, "get_company_site_by_name",
                        lambda conn, cid, name: {"id": "site-1", "name": name}
                        if name == "Test Site" else None)
    monkeypatch.setattr(ing.users, "list_company_users", lambda conn, cid: [])
    monkeypatch.setattr(ing.chunks, "delete_chunks_for_source", lambda *a, **k: 0)
    monkeypatch.setattr(ing.topics, "delete_topics_for_source", lambda *a, **k: 0)
    monkeypatch.setattr(ing.topics, "delete_topics_for_source_prefix", lambda *a, **k: 0)
    monkeypatch.setattr(ing.topics, "upsert_topic",
                        lambda *a, **k: {"id": "topic-uuid-0"})
    monkeypatch.setattr(ing.chunks, "insert_chunk", lambda *a, **k: {"id": "chunk-x"})
    monkeypatch.setattr(ing, "embed_text", lambda text: "[" + ",".join(["0.0"] * 1024) + "]")
    monkeypatch.setattr(ing, "_load_turns", lambda user_folder, date: [])
    return monkeypatch


# ---------------------------------------------------------------------------
# S3 event / manual / backfill entry points
# ---------------------------------------------------------------------------

def test_s3_event_key_parsing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ing, "ingest_report",
        lambda date, user_folder, key: calls.append((date, user_folder, key))
        or {"skipped": False, "topics": 1, "chunks": 2},
    )
    # S3 event notifications encode spaces as '+' and other specials as %XX —
    # unquote_plus must be applied before the key is used against S3/DB.
    event = {"Records": [{"s3": {"object": {
        "key": "reports/2026-03-02/Jarley+Trainor/daily_report.json"}}}]}

    result = ing.lambda_handler(event, None)

    assert calls == [("2026-03-02", "Jarley Trainor",
                      "reports/2026-03-02/Jarley Trainor/daily_report.json")]
    assert result == {"results": [{"skipped": False, "topics": 1, "chunks": 2}]}


# ---------------------------------------------------------------------------
# Identity bridge — site resolution
# ---------------------------------------------------------------------------

def test_site_bridge_by_report_name(monkeypatch):
    monkeypatch.setattr(
        ing.sites, "get_company_site_by_name",
        lambda conn, cid, name: {"id": "site-1", "name": name}
        if name == "SB1108 Ellesmere College" else None,
    )
    report = {"site": "SB1108 Ellesmere College", "topics": []}

    site = ing.resolve_site(None, "co-1", report, "Jarley_Trainor")

    assert site == {"id": "site-1", "name": "SB1108 Ellesmere College"}


def test_site_bridge_fallback_slug(monkeypatch):
    mapping = {
        "sites": {"sb1108-ellesmere": {"name": "SB1108 Ellesmere College"}},
        "mapping": {"Benl1": {"name": "Jarley Trainor", "primary_site": "sb1108-ellesmere"}},
    }
    monkeypatch.setattr(ing, "load_mapping", lambda: mapping)
    monkeypatch.setattr(
        ing.sites, "get_company_site_by_name",
        lambda conn, cid, name: {"id": "site-9", "name": name}
        if name == "SB1108 Ellesmere College" else None,
    )
    # report['site'] does not match anything directly -> must fall through
    # to the user_mapping.json primary_site slug bridge.
    report = {"site": "some transcription noise", "topics": []}

    site = ing.resolve_site(None, "co-1", report, "Jarley_Trainor")

    assert site == {"id": "site-9", "name": "SB1108 Ellesmere College"}


def test_site_bridge_miss_skips(wired):
    # Real 2026-03-20 case: report['site'] is not a real site, and the user
    # isn't in user_mapping.json either -> skip, zero writes.
    wired.setattr(ing, "_s3_client", FakeS3({REPORT_KEY: json.dumps(
        make_report(site="BD Opportunity Brainstorm"))}))
    wired.setattr(ing.sites, "get_company_site_by_name", lambda conn, cid, name: None)
    wired.setattr(ing, "load_mapping", lambda: {"sites": {}, "mapping": {}})

    write_calls = []
    wired.setattr(ing.chunks, "delete_chunks_for_source",
                  lambda *a, **k: write_calls.append("delete_chunks"))
    wired.setattr(ing.topics, "delete_topics_for_source",
                  lambda *a, **k: write_calls.append("delete_topics"))
    wired.setattr(ing.topics, "upsert_topic",
                  lambda *a, **k: write_calls.append("upsert_topic") or {"id": "x"})
    wired.setattr(ing.chunks, "insert_chunk",
                  lambda *a, **k: write_calls.append("insert_chunk"))

    result = ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert result["skipped"] is True
    assert "BD Opportunity Brainstorm" in result["reason"]
    assert write_calls == []  # zero repo writes on a double identity-bridge miss


# ---------------------------------------------------------------------------
# Idempotency — source-key delete before insert
# ---------------------------------------------------------------------------

def test_idempotent_source_delete_before_insert(wired):
    order = []
    wired.setattr(ing.chunks, "delete_chunks_for_source",
                  lambda *a, **k: order.append("delete_chunks"))
    wired.setattr(ing.topics, "delete_topics_for_source",
                  lambda *a, **k: order.append("delete_topics"))
    wired.setattr(ing.topics, "upsert_topic",
                  lambda *a, **k: order.append("upsert_topic") or {"id": "topic-uuid-0"})
    wired.setattr(ing.chunks, "insert_chunk",
                  lambda *a, **k: order.append("insert_chunk") or {"id": "chunk-x"})

    ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert order.index("delete_chunks") < order.index("upsert_topic")
    assert order.index("delete_topics") < order.index("upsert_topic")
    assert order.index("upsert_topic") < order.index("insert_chunk")


# ---------------------------------------------------------------------------
# Topic uuid -> chunk topic_id
# ---------------------------------------------------------------------------

def test_topic_uuid_flows_to_chunks(wired):
    wired.setattr(ing.topics, "upsert_topic", lambda *a, **k: {"id": "topic-uuid-77"})
    inserted = []

    def fake_insert_chunk(conn, site_id, report_date, chunk_type, chunk_text, embedding, **kw):
        inserted.append(kw)
        return {"id": "chunk-x"}

    wired.setattr(ing.chunks, "insert_chunk", fake_insert_chunk)

    ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert inserted  # at least the topic chunk was inserted
    assert all(kw["topic_id"] == "topic-uuid-77" for kw in inserted)


# ---------------------------------------------------------------------------
# Embedding format
# ---------------------------------------------------------------------------

def test_embedding_string_format(monkeypatch):
    fake_vector = [float(i) / 10 for i in range(1024)]

    class FakeBedrock:
        def invoke_model(self, modelId, body):
            assert modelId == ing.BEDROCK_MODEL_ID
            payload = json.loads(body)
            assert payload["inputText"] == "hello world"
            return {"body": io.BytesIO(json.dumps({"embedding": fake_vector}).encode("utf-8"))}

    monkeypatch.setattr(ing, "_bedrock_client", FakeBedrock())

    result = ing.embed_text("hello world")

    assert result.startswith("[")
    assert result.endswith("]")
    values = result[1:-1].split(",")
    assert len(values) == 1024
    assert float(values[0]) == 0.0


# ---------------------------------------------------------------------------
# Backfill failure isolation
# ---------------------------------------------------------------------------

def test_backfill_isolates_failures(monkeypatch):
    keys = ["reports/2026-03-01/A/daily_report.json",
            "reports/2026-03-02/B/daily_report.json",
            "reports/2026-03-03/C/daily_report.json"]
    monkeypatch.setattr(ing, "_list_report_keys", lambda: iter(keys))

    def fake_ingest(date, user_folder, key):
        if user_folder == "B":
            raise RuntimeError("boom")
        if user_folder == "C":
            return {"skipped": True, "reason": "no site match"}
        return {"skipped": False, "topics": 1, "chunks": 2}

    monkeypatch.setattr(ing, "ingest_report", fake_ingest)

    result = ing.run_backfill()

    assert result["processed"] == 1
    assert result["skipped"] == [{"key": keys[2], "reason": "no site match"}]
    assert result["failed"] == [{"key": keys[1], "error": "boom"}]

    # {"backfill": true} on lambda_handler must dispatch to run_backfill.
    monkeypatch.setattr(ing, "run_backfill", lambda: {"processed": 9, "skipped": [], "failed": []})
    assert ing.lambda_handler({"backfill": True}, None) == {"processed": 9, "skipped": [], "failed": []}


# ---------------------------------------------------------------------------
# User bridge — null on miss
# ---------------------------------------------------------------------------

def test_user_bridge_null_on_miss(wired):
    wired.setattr(ing.users, "list_company_users",
                  lambda conn, cid: [{"id": "u-1", "first_name": "Someone", "last_name": "Else"}])

    user_id = ing.resolve_user(None, "co-1", "Jarley_Trainor")
    assert user_id is None

    # a miss must NOT skip the report -- only a site-bridge miss does that --
    # it just flows user_id=None into the topic/chunk inserts.
    seen_user_ids = []
    wired.setattr(ing.topics, "upsert_topic",
                  lambda conn, site_id, report_date, title, **kw:
                      seen_user_ids.append(kw.get("user_id")) or {"id": "topic-uuid-0"})
    wired.setattr(ing.chunks, "insert_chunk",
                  lambda conn, site_id, report_date, chunk_type, chunk_text, embedding, **kw:
                      seen_user_ids.append(kw.get("user_id")) or {"id": "chunk-x"})

    result = ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert result["skipped"] is False
    assert seen_user_ids and all(uid is None for uid in seen_user_ids)


# ---------------------------------------------------------------------------
# Phase 4b — nightly report supersedes that day's session-sourced (live
# extraction) items
# ---------------------------------------------------------------------------

def test_ingest_supersedes_session_items(wired):
    calls = []
    wired.setattr(ing.topics, "delete_topics_for_source_prefix",
                  lambda conn, prefix: calls.append(prefix) or 0)

    ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert calls == ["extractions/Jarley_Trainor/2026-03-02/"]


# ---------------------------------------------------------------------------
# C1 regression — NULL-user same-site/same-date reports must NOT delete each
# other (source-key deletes carry the report's own key, never a shared scope)
# ---------------------------------------------------------------------------

def test_null_user_reports_do_not_collide(wired, monkeypatch):
    key_a = "reports/2026-03-02/MPI1/daily_report.json"
    key_b = "reports/2026-03-02/MPI2/daily_report.json"
    fake_s3 = FakeS3({
        key_a: json.dumps(make_report(user_name="MPI1")),
        key_b: json.dumps(make_report(user_name="MPI2")),
    })
    monkeypatch.setattr(ing, "s3", lambda: fake_s3)

    deleted_keys = []
    wired.setattr(ing.chunks, "delete_chunks_for_source",
                  lambda conn, key: deleted_keys.append(("chunks", key)) or 0)
    wired.setattr(ing.topics, "delete_topics_for_source",
                  lambda conn, key: deleted_keys.append(("topics", key)) or 0)
    wired.setattr(ing.topics, "upsert_topic", lambda *a, **k: {"id": "t-uuid"})
    wired.setattr(ing.chunks, "insert_chunk", lambda *a, **k: {"id": "c-uuid"})
    # user bridge misses for both (wired fixture's list_company_users is empty)

    ing.ingest_report("2026-03-02", "MPI1", key_a)
    ing.ingest_report("2026-03-02", "MPI2", key_b)

    keys_used = {k for _, k in deleted_keys}
    assert keys_used == {key_a, key_b}
    assert deleted_keys.count(("chunks", key_a)) == 1
    assert deleted_keys.count(("chunks", key_b)) == 1
