"""
Tests for src/lambda_item_writer.py — Phase 4b, Task 3 (TDD).

Style mirrors tests/unit/test_lambda_ingest.py: FakeConn/FakeS3 + monkeypatch
on repositories.topics/companies and on lambda_ingest's identity-bridge
functions (resolve_site/resolve_user/_map_action_items/_map_safety), which
this writer REUSES via `import lambda_ingest` (not copied) — patching
iw.lambda_ingest.<fn> patches the one shared module object, same as patching
`lambda_ingest.<fn>` directly.
"""
import io
import json

import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """report_already_ingested governs what conn.execute(...).fetchone()
    returns -- only the I-4 report-source-key query ever calls .fetchone()
    in production code, so a single flag suffices even though every
    conn.execute() call (including the I-3 advisory lock) shares it."""

    def __init__(self, report_already_ingested=False):
        self.executed = []
        self.report_already_ingested = report_already_ingested

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeCursor({"?column?": 1} if self.report_already_ingested else None)


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
    """Task 3 (authority-flip plan): the pictures-prefix listing paginator.
    Mirrors tests/unit/test_lambda_ingest.py's _FakePaginator exactly."""

    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        contents = [{"Key": k} for k in self.objects if k.startswith(Prefix)]
        yield {"Contents": contents}


EXTRACTION_KEY = "extractions/Jarley_Trainor/2026-07-06/Benl1_2026-07-06_10-00-00.json"


def make_extraction(**overrides):
    extraction = {
        "schema_version": 1,
        "user_folder": "Jarley_Trainor",
        "date": "2026-07-06",
        "session_base": "Benl1_2026-07-06_10-00-00",
        "source_transcripts": ["Benl1_2026-07-06_10-00-00.json"],
        "extracted_at": "2026-07-06T10:05:00Z",
        "declared_site": None,
        "topics": [{
            "topic_title": "Safety Briefing",
            "category": "safety",
            "summary": "Discussed PPE requirements.",
            "time_range": "10:00 – 10:05",
            "participants": ["Jarley Trainor"],
            "action_items": [
                {"action": "Order more hard hats", "responsible": "Bob", "deadline": "Friday"}
            ],
            "safety_flags": [
                {"risk_level": "medium", "observation": "Missing barrier tape",
                 "recommended_action": "Install tape"}
            ],
        }],
    }
    extraction.update(overrides)
    return extraction


@pytest.fixture
def wired(monkeypatch):
    """Common wiring: FakeConn, a resolvable site+company, inert repo
    writes, and a resolve_site/resolve_user identity bridge stubbed to hit.
    Individual tests override as needed."""
    monkeypatch.setattr(iw, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(iw, "_s3_client", FakeS3({EXTRACTION_KEY: json.dumps(make_extraction())}))
    monkeypatch.setattr(iw.companies, "get_company_by_name",
                        lambda conn, name: {"id": "co-1", "name": name})
    monkeypatch.setattr(iw.lambda_ingest, "resolve_site",
                        lambda conn, cid, report, user_folder: {"id": "site-1", "name": "Test Site"})
    monkeypatch.setattr(iw.lambda_ingest, "resolve_user", lambda conn, cid, user_folder: None)
    monkeypatch.setattr(iw.recordings, "site_for_media", lambda *a, **k: None)
    monkeypatch.setattr(iw.topics, "delete_topics_for_source", lambda *a, **k: 0)
    monkeypatch.setattr(iw.topics, "upsert_topic", lambda *a, **k: {"id": "topic-uuid-0"})
    monkeypatch.setattr(iw.findings, "insert_findings", lambda *a, **k: [])
    # match_request.emit does a real s3.put_object -- FakeS3 above only
    # implements get_object, so stub emit to a no-op by default here;
    # tests that care about the emit call override this explicitly.
    monkeypatch.setattr(iw.match_request, "emit", lambda *a, **k: None)
    # video-keyframe plan: same reasoning for the keyframe_requests/ emit, and
    # keep emission OFF by default (module constant is env-gated at import) so
    # the pre-existing tests never trip it.
    monkeypatch.setattr(iw.keyframe_request, "emit", lambda *a, **k: None)
    monkeypatch.setattr(iw, "EMIT_KEYFRAME_REQUESTS", False)
    return monkeypatch


# ---------------------------------------------------------------------------
# S3 event key parsing — depth-exact extractions/{user}/{date}/{name}.json
# ---------------------------------------------------------------------------

def test_key_parsing_depth_exact(monkeypatch):
    calls = []
    monkeypatch.setattr(
        iw, "write_extraction_items",
        lambda date, user_folder, key: calls.append((date, user_folder, key))
        or {"skipped": False, "topics": 1},
    )
    event = {"Records": [
        # normal depth-exact key, with '+'-encoded space (S3 event encoding)
        {"s3": {"object": {
            "key": "extractions/Jarley+Trainor/2026-07-06/Benl1_2026-07-06_10-00-00.json"}}},
        # too deep -> must be skipped, not dispatched
        {"s3": {"object": {
            "key": "extractions/Jarley_Trainor/2026-07-06/extra/depth.json"}}},
        # wrong suffix -> must be skipped
        {"s3": {"object": {
            "key": "extractions/Jarley_Trainor/2026-07-06/notjson.txt"}}},
    ]}

    result = iw.lambda_handler(event, None)

    assert calls == [("2026-07-06", "Jarley Trainor",
                      "extractions/Jarley Trainor/2026-07-06/Benl1_2026-07-06_10-00-00.json")]
    assert result == {"results": [{"skipped": False, "topics": 1}]}


# ---------------------------------------------------------------------------
# Site identity bridge — extraction has no 'site' field -> resolve_site is
# called with an empty report dict (falls to the primary_site mapping
# chain); a double miss skips with zero writes.
# ---------------------------------------------------------------------------

def test_site_bridge_fallback_and_skip(wired):
    resolve_calls = []
    wired.setattr(
        iw.lambda_ingest, "resolve_site",
        lambda conn, cid, report, user_folder:
            resolve_calls.append((report, user_folder)) or None,
    )
    write_calls = []
    wired.setattr(iw.topics, "delete_topics_for_source",
                  lambda *a, **k: write_calls.append("delete_topics"))
    wired.setattr(iw.topics, "upsert_topic",
                  lambda *a, **k: write_calls.append("upsert_topic") or {"id": "x"})

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    # resolve_site called with {} (no report-level site) -> falls through
    # to the user_mapping.json primary_site bridge inside the real fn.
    assert resolve_calls == [({}, "Jarley_Trainor")]
    assert result["skipped"] is True
    assert write_calls == []  # zero repo writes on a double identity-bridge miss


# ---------------------------------------------------------------------------
# Idempotency — source-key delete before insert
# ---------------------------------------------------------------------------

def test_idempotent_delete_before_insert(wired):
    order = []
    wired.setattr(iw.topics, "delete_topics_for_source",
                  lambda *a, **k: order.append("delete_topics"))
    wired.setattr(iw.topics, "upsert_topic",
                  lambda *a, **k: order.append("upsert_topic") or {"id": "topic-uuid-0"})

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert order == ["delete_topics", "upsert_topic"]

    # and it must be keyed on THIS extraction's key
    wired.setattr(iw.topics, "delete_topics_for_source", lambda conn, key: order.append(key))
    order.clear()
    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)
    assert order[0] == EXTRACTION_KEY


# ---------------------------------------------------------------------------
# Topic children mapped through lambda_ingest's _map_action_items/_map_safety
# ---------------------------------------------------------------------------

def test_topic_children_mapped(wired):
    captured = []
    wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            captured.append(kw) or {"id": "topic-uuid-0"},
    )

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert len(captured) == 1
    kw = captured[0]
    assert kw["source_s3_key"] == EXTRACTION_KEY
    assert kw["category"] == "safety"
    assert kw["summary"] == "Discussed PPE requirements."
    # _map_action_items: 'action' -> 'text'; 'deadline' "Friday" is not an
    # ISO date -> dropped to None (same rule as lambda_ingest's reports), but
    # 'deadline_text' keeps the raw string (Task 2, authority-flip plan).
    assert kw["action_items"] == [{
        "text": "Order more hard hats", "responsible": "Bob",
        "deadline": None, "deadline_text": "Friday", "priority": None,
    }]
    # Phase F Task 23 (D8 retirement, spec §8): the item-writer no longer
    # passes safety= to upsert_topic at all -- findings (inserted separately,
    # below) are the single source of truth for safety, and the
    # safety_observations dual-write INSERT this kwarg used to trigger is
    # gone. safety_flags stays in the extraction JSON itself (chunking.py /
    # lambda_ask_agent.py still read topic['safety_flags'] for RAG embedding
    # text) -- only the Aurora write is stopped.
    assert "safety" not in kw


# ---------------------------------------------------------------------------
# Task 2 (authority-flip plan) -- item-writer passes time_range/participants
# through to topics.upsert_topic (migration 0011 columns; Task 1 already
# landed the repo-layer plumbing, this is the writer actually calling it).
# ---------------------------------------------------------------------------

def test_item_writer_passes_time_range_and_participants(wired):
    captured = []
    wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            captured.append(kw) or {"id": "topic-uuid-0"},
    )

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert len(captured) == 1
    assert captured[0]["time_range"] == "10:00 – 10:05"
    assert captured[0]["participants"] == ["Jarley Trainor"]


def test_legacy_extraction_without_time_range_writes_null(wired):
    # Pre-authority-flip extraction JSON still in S3 has no time_range/
    # participants keys on the topic at all -- t.get(...) -> None -> NULL,
    # never a KeyError.
    legacy_topic = {
        "topic_title": "Safety Briefing",
        "category": "safety",
        "summary": "Discussed PPE requirements.",
        "action_items": [],
        "safety_flags": [],
    }
    wired.setattr(
        iw, "_s3_client",
        FakeS3({EXTRACTION_KEY: json.dumps(make_extraction(topics=[legacy_topic]))}),
    )
    captured = []
    wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            captured.append(kw) or {"id": "topic-uuid-0"},
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}
    assert len(captured) == 1
    assert captured[0]["time_range"] is None
    assert captured[0]["participants"] is None


# ---------------------------------------------------------------------------
# Task 8 (life-conversation-separation plan) -- item-writer passes
# work_class/work_confidence/is_mixed through to topics.upsert_topic
# (migration 0011-follow-on columns; Task 1 landed the repo-layer plumbing,
# Task 7 landed the classifier populating these keys on the topic dict).
# ---------------------------------------------------------------------------

def test_item_writer_passes_work_class_fields(wired):
    captured = {}

    def fake_upsert(conn, site_id, report_date, title, **kw):
        captured.update(kw)
        return {"id": "topic-uuid-0"}

    topic = make_extraction()["topics"][0]
    topic.update(work_class="non_work", work_confidence=0.9, is_mixed=True)
    wired.setattr(
        iw, "_s3_client",
        FakeS3({EXTRACTION_KEY: json.dumps(make_extraction(topics=[topic]))}),
    )
    wired.setattr(iw.topics, "upsert_topic", fake_upsert)

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert captured["work_class"] == "non_work"
    assert captured["work_confidence"] == 0.9 and captured["is_mixed"] is True


def test_item_writer_sanitizes_garbage_work_class_fields(wired):
    # A raw bad LLM value (e.g. "personal", or a non-numeric confidence)
    # would otherwise raise inside the CHECK-constrained INSERT and abort
    # the whole session's topics/findings write (Fable review #7) -- the
    # writer must sanitize invalid values to NULL instead of raising.
    captured = {}

    def fake_upsert(conn, site_id, report_date, title, **kw):
        captured.update(kw)
        return {"id": "topic-uuid-0"}

    topic = make_extraction()["topics"][0]
    topic.update(work_class="personal", work_confidence="high", is_mixed=True)
    wired.setattr(
        iw, "_s3_client",
        FakeS3({EXTRACTION_KEY: json.dumps(make_extraction(topics=[topic]))}),
    )
    wired.setattr(iw.topics, "upsert_topic", fake_upsert)

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert captured["work_class"] is None
    assert captured["work_confidence"] is None


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

def test_summary_result_shape(wired):
    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}


def test_company_missing_raises(wired):
    wired.setattr(iw.companies, "get_company_by_name", lambda conn, name: None)

    with pytest.raises(RuntimeError, match="org seed"):
        iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)


def test_user_bridge_miss_does_not_skip(wired):
    # A user-bridge miss (unlike a site-bridge miss) must NOT skip the
    # extraction -- it just flows user_id=None into upsert_topic (mirrors
    # lambda_ingest.resolve_user's contract).
    wired.setattr(iw.lambda_ingest, "resolve_user", lambda conn, cid, user_folder: None)
    seen = []
    wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            seen.append(kw.get("user_id")) or {"id": "topic-uuid-0"},
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result["skipped"] is False
    assert seen == [None]


# ---------------------------------------------------------------------------
# I-3 regression test: the advisory lock is acquired (on this extraction's
# key) before delete_topics_for_source/upsert_topic -- serializes concurrent
# writers on the same key, since delete-then-insert isn't concurrency-safe
# and upsert_topic is INSERT-only.
# ---------------------------------------------------------------------------

def test_advisory_lock_acquired_before_delete_and_insert(wired):
    conn = FakeConn()
    wired.setattr(iw, "get_connection", lambda *a, **k: conn)

    order = []
    wired.setattr(iw.topics, "delete_topics_for_source",
                  lambda *a, **k: order.append("delete_topics"))
    wired.setattr(iw.topics, "upsert_topic",
                  lambda *a, **k: order.append("upsert_topic") or {"id": "topic-uuid-0"})

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert conn.executed[0] == ("SELECT pg_advisory_xact_lock(hashtext(%s))", (EXTRACTION_KEY,))
    assert order == ["delete_topics", "upsert_topic"]


# ---------------------------------------------------------------------------
# I-4 regression test: when a row already exists for this (date, user_folder)
# nightly report's source_s3_key, the extraction is superseded -- skip with
# zero writes (no delete, no upsert).
# ---------------------------------------------------------------------------

def test_report_already_ingested_supersedes_late_extraction(wired):
    conn = FakeConn(report_already_ingested=True)
    wired.setattr(iw, "get_connection", lambda *a, **k: conn)

    write_calls = []
    wired.setattr(iw.topics, "delete_topics_for_source",
                  lambda *a, **k: write_calls.append("delete_topics"))
    wired.setattr(iw.topics, "upsert_topic",
                  lambda *a, **k: write_calls.append("upsert_topic") or {"id": "x"})

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {
        "skipped": True,
        "reason": "nightly report already ingested — late session extraction superseded",
    }
    assert write_calls == []
    # the report-source-key query used the nightly report's own contract key,
    # not the (unrelated) session extraction key
    report_query = [c for c in conn.executed if "topics" in c[0] and "advisory" not in c[0]]
    assert report_query == [(
        "SELECT 1 FROM topics WHERE source_s3_key=%s LIMIT 1",
        ("reports/2026-07-06/Jarley_Trainor/daily_report.json",),
    )]


# ---------------------------------------------------------------------------
# Task 4 — match_request.emit is called with the freshly-written topics
# AFTER the connection block commits, and only on a successful non-empty
# write (never on a skip or a zero-topic extraction).
# ---------------------------------------------------------------------------

def test_match_request_emitted_after_multi_topic_write(wired):
    calls = []
    wired.setattr(
        iw.match_request, "emit",
        lambda s3_client, bucket, site_id, report_date, source_key, topics:
            calls.append((bucket, site_id, report_date, source_key, topics))
            or "match_requests/site-1/2026-07-06/abc123.json",
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}
    assert len(calls) == 1
    bucket, site_id, report_date, source_key, topics = calls[0]
    assert bucket == iw.S3_BUCKET
    assert site_id == "site-1"
    assert report_date == "2026-07-06"
    assert source_key == EXTRACTION_KEY
    assert topics == [{
        "topic_id": "topic-uuid-0",
        "title": "Safety Briefing",
        "summary": "Discussed PPE requirements.",
        "user_id": None,
        "action_items": [{"text": "Order more hard hats"}],
        "findings": [],
    }]


def test_match_request_not_emitted_on_identity_skip(wired):
    calls = []
    wired.setattr(iw.match_request, "emit", lambda *a, **k: calls.append(a) or None)
    wired.setattr(
        iw.lambda_ingest, "resolve_site",
        lambda conn, cid, report, user_folder: None,
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result["skipped"] is True
    assert calls == []


def test_match_request_not_emitted_on_zero_topics(wired):
    calls = []
    wired.setattr(iw.match_request, "emit", lambda *a, **k: calls.append(a) or None)
    wired.setattr(
        iw, "_s3_client",
        FakeS3({EXTRACTION_KEY: json.dumps(make_extraction(topics=[]))}),
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 0}
    assert calls == []


# ---------------------------------------------------------------------------
# Task 2 (programme-impact-link plan) — item-writer persists rich findings
# to Aurora via repositories.findings.insert_findings, in the SAME
# connection/transaction as the topic upsert, and carries the new finding
# uuids in the match_requests/ artifact snapshot.
# ---------------------------------------------------------------------------

FINDINGS_PAYLOAD = [
    {"observation": "Missing barrier tape at north stairwell", "domain": "safety",
     "severity": "major", "entity": {"name": "ABC Scaffolding", "trade": "scaffolding"},
     "recommended_action": "Install tape immediately"},
    {"observation": "Slab pour running two days behind", "domain": "progress",
     "severity": "minor", "entity": {"name": "ABC Concrete", "trade": "concrete"},
     "recommended_action": None},
]


def test_writes_findings_rows_per_topic(wired):
    conn_holder = {}
    wired.setattr(iw, "get_connection",
                  lambda *a, **k: conn_holder.setdefault("conn", FakeConn()))
    wired.setattr(
        iw, "_s3_client",
        FakeS3({EXTRACTION_KEY: json.dumps(
            make_extraction(topics=[{
                **make_extraction()["topics"][0],
                "findings": FINDINGS_PAYLOAD,
            }]))}),
    )
    calls = []
    wired.setattr(
        iw.findings, "insert_findings",
        lambda conn, topic_id, site_id, findings_list:
            calls.append((conn, topic_id, site_id, findings_list)) or [],
    )

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert len(calls) == 1
    conn, topic_id, site_id, findings_list = calls[0]
    # SAME connection as the topic upsert -- inherits the I-3 advisory lock
    # and I-4 supersession-guard transaction.
    assert conn is conn_holder["conn"]
    assert topic_id == "topic-uuid-0"  # row["id"] from the stubbed upsert_topic
    assert site_id == "site-1"         # site["id"] from the stubbed resolve_site
    assert findings_list == FINDINGS_PAYLOAD


# ---------------------------------------------------------------------------
# Phase F Task 23 (D8 retirement, spec §8) -- stop the _derive_safety_flags
# dual-write INSERT into safety_observations. findings.insert_findings (Task
# 2 above) is the ONLY Aurora write for a topic's safety data now; the
# extraction JSON's topic['safety_flags'] (populated by lambda_extract_
# session._derive_safety_flags, still consumed by chunking.py's RAG
# embedding text -- untouched by this task) is no longer mapped into
# topics.upsert_topic's `safety=` kwarg, so upsert_topic's own
# `INSERT INTO safety_observations` loop never fires.
# ---------------------------------------------------------------------------

def test_findings_written_and_safety_observations_no_longer_inserted(wired):
    upsert_calls = []
    wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            upsert_calls.append(kw) or {"id": "topic-uuid-0"},
    )
    finding_calls = []
    wired.setattr(
        iw.findings, "insert_findings",
        lambda conn, topic_id, site_id, findings_list:
            finding_calls.append(findings_list) or [],
    )
    wired.setattr(
        iw, "_s3_client",
        FakeS3({EXTRACTION_KEY: json.dumps(
            make_extraction(topics=[{
                **make_extraction()["topics"][0],
                "findings": FINDINGS_PAYLOAD,
            }]))}),
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}
    # findings remain the source of truth -- still written.
    assert finding_calls == [FINDINGS_PAYLOAD]
    # the dual-write is gone: upsert_topic never receives a `safety` kwarg,
    # so its own safety_observations INSERT loop (`for o in (safety or []):`)
    # has nothing to iterate and never executes.
    assert len(upsert_calls) == 1
    assert "safety" not in upsert_calls[0]


def test_artifact_topics_carry_finding_ids(wired):
    wired.setattr(
        iw, "_s3_client",
        FakeS3({EXTRACTION_KEY: json.dumps(
            make_extraction(topics=[{
                **make_extraction()["topics"][0],
                "findings": FINDINGS_PAYLOAD,
            }]))}),
    )
    wired.setattr(
        iw.findings, "insert_findings",
        lambda conn, topic_id, site_id, findings_list: [
            {"id": "finding-uuid-1", "observation": "Missing barrier tape at north stairwell",
             "domain": "safety", "severity": "major",
             "entity_name": "ABC Scaffolding", "entity_trade": "scaffolding"},
            {"id": "finding-uuid-2", "observation": "Slab pour running two days behind",
             "domain": "progress", "severity": "minor",
             "entity_name": "ABC Concrete", "entity_trade": "concrete"},
        ],
    )
    emitted = []
    wired.setattr(
        iw.match_request, "emit",
        lambda s3_client, bucket, site_id, report_date, source_key, topics:
            emitted.append(topics) or "match_requests/site-1/2026-07-06/abc123.json",
    )

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert len(emitted) == 1
    topic_snapshot = emitted[0][0]
    assert topic_snapshot["findings"] == [
        {"finding_id": "finding-uuid-1", "observation": "Missing barrier tape at north stairwell",
         "domain": "safety", "severity": "major",
         "entity_name": "ABC Scaffolding", "entity_trade": "scaffolding"},
        {"finding_id": "finding-uuid-2", "observation": "Slab pour running two days behind",
         "domain": "progress", "severity": "minor",
         "entity_name": "ABC Concrete", "entity_trade": "concrete"},
    ]
    # finding_id must be a str (the repo returns a uuid.UUID/asyncpg-style
    # object in prod; the artifact is JSON so it must already be stringified).
    assert all(isinstance(f["finding_id"], str) for f in topic_snapshot["findings"])


def test_no_findings_key_still_works(wired):
    # Legacy extraction JSON (pre-#46 extractions still in S3, and the
    # report/ingest path which never carries findings) has no "findings"
    # key on the topic at all -- must not crash, and insert_findings must
    # be called with [] so the artifact still gets a "findings": [] key.
    calls = []
    wired.setattr(
        iw.findings, "insert_findings",
        lambda conn, topic_id, site_id, findings_list:
            calls.append(findings_list) or [],
    )
    emitted = []
    wired.setattr(
        iw.match_request, "emit",
        lambda s3_client, bucket, site_id, report_date, source_key, topics:
            emitted.append(topics) or "match_requests/site-1/2026-07-06/abc123.json",
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}
    assert calls == [[]]
    assert emitted[0][0]["findings"] == []


# ---------------------------------------------------------------------------
# video-keyframe plan, Task 2 -- item-writer emits a keyframe_requests/
# artifact post-commit, gated by EMIT_KEYFRAME_REQUESTS, carrying ONLY the
# topics whose time_range passes the >=2-minute gate (keyframe_seconds
# non-empty), with their durable topic ids + time_ranges.
# ---------------------------------------------------------------------------

def _two_topic_extraction():
    """One gate-passing topic (5 min) + one gated-out topic (same-minute)."""
    passing = {
        "topic_title": "Long talk-to-camera", "category": "progress",
        "summary": "Walkthrough.", "time_range": "10:00 – 10:05",
        "action_items": [], "safety_flags": [],
    }
    gated = {
        "topic_title": "Quick note", "category": "general",
        "summary": "Brief.", "time_range": "12:14 – 12:14",
        "action_items": [], "safety_flags": [],
    }
    return make_extraction(topics=[passing, gated])


def _upsert_incrementing(wired):
    """Give each upserted topic a distinct id so emit payloads are checkable."""
    counter = {"n": 0}

    def fake_upsert(conn, site_id, report_date, title, **kw):
        counter["n"] += 1
        return {"id": f"topic-{counter['n']}"}

    wired.setattr(iw.topics, "upsert_topic", fake_upsert)


def test_keyframe_request_emitted_only_for_gated_topics_when_enabled(wired):
    wired.setattr(iw, "EMIT_KEYFRAME_REQUESTS", True)
    wired.setattr(iw, "_s3_client", FakeS3({EXTRACTION_KEY: json.dumps(_two_topic_extraction())}))
    _upsert_incrementing(wired)
    calls = []
    wired.setattr(
        iw.keyframe_request, "emit",
        lambda s3_client, bucket, user_folder, date, session_base, extraction_key, topics:
            calls.append((bucket, user_folder, date, session_base, extraction_key, topics))
            or "keyframe_requests/Jarley_Trainor/2026-07-06/abc.json",
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 2}
    assert len(calls) == 1
    bucket, user_folder, date, session_base, extraction_key, topics = calls[0]
    assert bucket == iw.S3_BUCKET
    assert user_folder == "Jarley_Trainor"
    assert date == "2026-07-06"
    assert session_base == "Benl1_2026-07-06_10-00-00"
    assert extraction_key == EXTRACTION_KEY
    # ONLY the 5-min topic (topic-1); the same-minute topic is gated out.
    assert topics == [{"topic_id": "topic-1", "time_range": "10:00 – 10:05"}]


def test_keyframe_request_not_emitted_when_no_gated_topic(wired):
    wired.setattr(iw, "EMIT_KEYFRAME_REQUESTS", True)
    gated_only = {
        "topic_title": "Quick note", "category": "general", "summary": "Brief.",
        "time_range": "12:12 – 12:13", "action_items": [], "safety_flags": [],
    }
    wired.setattr(iw, "_s3_client",
                  FakeS3({EXTRACTION_KEY: json.dumps(make_extraction(topics=[gated_only]))}))
    calls = []
    wired.setattr(iw.keyframe_request, "emit", lambda *a, **k: calls.append(a) or None)

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}
    assert calls == []


def test_keyframe_request_not_emitted_when_flag_disabled(wired):
    wired.setattr(iw, "EMIT_KEYFRAME_REQUESTS", False)
    wired.setattr(iw, "_s3_client", FakeS3({EXTRACTION_KEY: json.dumps(_two_topic_extraction())}))
    _upsert_incrementing(wired)
    calls = []
    wired.setattr(iw.keyframe_request, "emit", lambda *a, **k: calls.append(a) or None)

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 2}
    assert calls == []  # gated topic present, but emission is off


# ---------------------------------------------------------------------------
# Task 3 (authority-flip plan) -- _photos_for_topics pure helper: time-
# correlates S3 pictures (already resolved to {key, filename, hhmm} by the
# BUG-01-safe transcript_utils filename extractor) against each topic's
# 'HH:MM – HH:MM' time_range window.
#
# P2 (2026-07-23 prod-media-binding plan): the helper now DELEGATES to
# photo_binding (shared with lambda_ingest's report path) and the rule
# changed -- strict containment stranded every prod photo by 1-2 minutes
# (topic_photos held 0 rows across all of prod history).
#
# 2026-07-24 correction (supersedes P2's unbounded nearest-wins): a photo
# binds to a topic only if inside its window or within PHOTO_TOLERANCE_MIN
# (2) minutes of an edge; ties -> lowest topic index; beyond that it binds
# to nothing (the never-orphan fallback is gone). Cap raised 5 -> 10 with
# cascade to the next-nearest QUALIFYING topic. The exhaustive rule table
# lives in tests/unit/test_photo_binding.py; the cases below stay here to
# pin the delegation and the module-level aliases.
# ---------------------------------------------------------------------------

def _photo(name, hhmm):
    return {"key": f"users/Jarley_Trainor/pictures/2026-07-06/{name}",
            "filename": name, "hhmm": hhmm}


def test_photo_near_window_binds_via_tolerance():
    # Was test_photo_matches_inside_time_range_only (strict containment): the
    # 10:06 photo, 1 minute outside the window, used to bind to nothing --
    # exactly the prod defect. It's within PHOTO_TOLERANCE_MIN (2 min), so it
    # still qualifies and binds under the current bounded-tolerance rule.
    topics_list = [{"time_range": "10:00 – 10:05"}]
    inside = _photo("a.jpg", "10:02")
    outside = _photo("b.jpg", "10:06")

    result = iw._photos_for_topics([inside, outside], topics_list)

    assert result == {0: [inside, outside]}


def test_photo_attaches_to_first_matching_topic_only():
    # Two topics with overlapping time_range windows -- a photo inside both
    # is equidistant (distance 0 from each), so the tie-break sends it to
    # the lowest-index topic ONLY. Unchanged by the P2 rule change.
    topics_list = [
        {"time_range": "09:00 – 10:00"},
        {"time_range": "09:30 – 11:00"},
    ]
    photo = _photo("a.jpg", "09:45")

    result = iw._photos_for_topics([photo], topics_list)

    assert result == {0: [photo], 1: []}


def test_cap_ten_photos_per_topic():
    # Was test_cap_five_photos_per_topic: the cap is PHOTOS_PER_TOPIC_CAP=10
    # (raised from the report-generator's 5). This day has a single topic, so
    # the 11th photo has nowhere to cascade to and is dropped with a warning.
    topics_list = [{"time_range": "09:00 – 10:00"}]
    photos = [_photo(f"p{i:02d}.jpg", f"09:{i:02d}") for i in range(11)]

    result = iw._photos_for_topics(photos, topics_list)

    assert iw.PHOTOS_PER_TOPIC_CAP == 10
    assert len(result[0]) == 10
    assert result[0] == photos[:10]  # first 10, cap does not reorder


def test_unparseable_time_range_gets_no_photos():
    # Survives the P2 rule change verbatim: a topic with no parseable window
    # never joins the candidate set while another topic has one, and the
    # all-indices result contract is tolerated by .get(i, []).
    topics_list = [
        {"time_range": None},          # missing
        {"time_range": "not a range"},  # unparseable
        {"time_range": "09:00 – 10:00"},  # valid, but doesn't overlap the others
    ]
    photo = _photo("a.jpg", "09:30")

    result = iw._photos_for_topics([photo], topics_list)

    assert result.get(0, []) == []
    assert result.get(1, []) == []
    assert result[2] == [photo]


# ---------------------------------------------------------------------------
# Task 3 (authority-flip plan) -- adapter: write_extraction_items lists
# users/{user_folder}/pictures/{date}/ ONCE per invocation (paginator,
# outside the per-topic loop) and passes time-correlated matches into the
# EXISTING topics.upsert_topic(photos=...) support (topics.py:38-42).
# ---------------------------------------------------------------------------

def test_item_writer_upserts_topic_photos(wired):
    pictures_prefix = "users/Jarley_Trainor/pictures/2026-07-06/"
    photo_key = pictures_prefix + "Benl1_2026-07-06_10-02-00.jpg"
    wired.setattr(iw, "_s3_client", FakeS3({
        EXTRACTION_KEY: json.dumps(make_extraction()),
        photo_key: b"",
    }))
    captured = []
    wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            captured.append(kw) or {"id": "topic-uuid-0"},
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}
    assert len(captured) == 1
    # make_extraction()'s one topic has time_range "10:00 – 10:05";
    # the photo's filename encodes 10:02, inside that window.
    assert captured[0]["photos"] == [{"s3_key": photo_key, "caption_text": None}]


def test_missing_pictures_prefix_is_noop(wired):
    # wired's default FakeS3 only has EXTRACTION_KEY -- the pictures prefix
    # listing returns zero Contents (empty prefix -> no-op, not a crash).
    captured = []
    wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            captured.append(kw) or {"id": "topic-uuid-0"},
    )

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result == {"skipped": False, "topics": 1}
    assert len(captured) == 1
    assert captured[0]["photos"] == []


# ---------------------------------------------------------------------------
# G5b -- recordings.site_for_media (app tag) overrides membership resolve_site
# ---------------------------------------------------------------------------

def _capture_topic_site(wired):
    """Capture the site_id positional arg upsert_topic receives; returns the list."""
    captured = []
    wired.setattr(iw.topics, "upsert_topic",
                  lambda conn, site_id, *a, **k: captured.append(site_id) or {"id": "topic-x"})
    return captured


def test_recording_tag_overrides_membership(wired):
    wired.setattr(iw.recordings, "site_for_media", lambda *a, **k: {"id": "site-TAG"})
    wired.setattr(iw.lambda_ingest, "resolve_site", lambda *a, **k: {"id": "site-MEMBER"})
    seen = _capture_topic_site(wired)
    iw.write_extraction_items("2026-07-16", "Ben_Lin", EXTRACTION_KEY)
    assert seen and all(s == "site-TAG" for s in seen)


def test_falls_back_to_membership_when_no_tag(wired):
    wired.setattr(iw.recordings, "site_for_media", lambda *a, **k: None)
    wired.setattr(iw.lambda_ingest, "resolve_site", lambda *a, **k: {"id": "site-MEMBER"})
    seen = _capture_topic_site(wired)
    iw.write_extraction_items("2026-07-16", "Ben_Lin", EXTRACTION_KEY)
    assert seen and all(s == "site-MEMBER" for s in seen)


def test_admin_recording_attributes_via_tag_not_skipped(wired):
    # admin: membership resolver returns None (ALL scope), but the app tag exists
    wired.setattr(iw.recordings, "site_for_media", lambda *a, **k: {"id": "site-TAG"})
    wired.setattr(iw.lambda_ingest, "resolve_site", lambda *a, **k: None)
    seen = _capture_topic_site(wired)
    result = iw.write_extraction_items("2026-07-16", "Ben_Lin", EXTRACTION_KEY)
    assert not result.get("skipped")
    assert seen and all(s == "site-TAG" for s in seen)


def test_no_tag_no_membership_still_skips(wired):
    wired.setattr(iw.recordings, "site_for_media", lambda *a, **k: None)
    wired.setattr(iw.lambda_ingest, "resolve_site", lambda *a, **k: None)
    called = []
    wired.setattr(iw.topics, "upsert_topic",
                  lambda *a, **k: called.append("upsert") or {"id": "x"})
    result = iw.write_extraction_items("2026-07-16", "Ben_Lin", EXTRACTION_KEY)
    assert result.get("skipped") is True
    assert called == []
