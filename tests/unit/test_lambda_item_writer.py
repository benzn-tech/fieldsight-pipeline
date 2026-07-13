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
    monkeypatch.setattr(iw.topics, "delete_topics_for_source", lambda *a, **k: 0)
    monkeypatch.setattr(iw.topics, "upsert_topic", lambda *a, **k: {"id": "topic-uuid-0"})
    monkeypatch.setattr(iw.findings, "insert_findings", lambda *a, **k: [])
    # match_request.emit does a real s3.put_object -- FakeS3 above only
    # implements get_object, so stub emit to a no-op by default here;
    # tests that care about the emit call override this explicitly.
    monkeypatch.setattr(iw.match_request, "emit", lambda *a, **k: None)
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
    # ISO date -> dropped to None (same rule as lambda_ingest's reports).
    assert kw["action_items"] == [{
        "text": "Order more hard hats", "responsible": "Bob",
        "deadline": None, "priority": None,
    }]
    # _map_safety: no 'location' column source, 'recommended_action' dropped.
    assert kw["safety"] == [{
        "observation": "Missing barrier tape", "risk_level": "medium",
    }]


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
