"""
Tests for src/lambda_ingest.py — Phase 4a ingest lambda (TDD).

Style mirrors tests/unit/test_lambda_org_api.py: FakeConn + monkeypatch on
module-level boto3 client globals (_s3_client) and on the
repositories/chunking/transcript_utils functions the handler calls.

Phase 4d: embeddings now come from an S3 vector sidecar (embed-report writes
embeddings/{date}/{user}/vectors.json = {sha256(chunk_text[:8000]): [floats]});
Bedrock is retired from this lambda entirely -- no test here may reference it.
"""
import hashlib
import io
import json

import pytest

ing = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")

# Captured before any fixture monkeypatches embed_from_sidecar, so tests that
# want the REAL sidecar-lookup behavior (not the wired fixture's canned stub)
# can restore it.
_real_embed_from_sidecar = ing.embed_from_sidecar


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeS3Exceptions:
    class NoSuchKey(Exception):
        pass


class FakeS3:
    """Minimal S3 client double: object store keyed by S3 key."""

    exceptions = _FakeS3Exceptions

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
    monkeypatch.setattr(ing.users, "get_by_folder_name", lambda conn, cid, folder_name: None)
    monkeypatch.setattr(ing.chunks, "delete_chunks_for_source", lambda *a, **k: 0)
    monkeypatch.setattr(ing.topics, "delete_topics_for_source", lambda *a, **k: 0)
    monkeypatch.setattr(ing.topics, "delete_topics_for_source_prefix", lambda *a, **k: 0)
    monkeypatch.setattr(ing.topics, "upsert_topic",
                        lambda *a, **k: {"id": "topic-uuid-0"})
    monkeypatch.setattr(ing.chunks, "insert_chunk", lambda *a, **k: {"id": "chunk-x"})
    monkeypatch.setattr(ing, "_load_vectors", lambda bucket, sidecar_key: {})
    monkeypatch.setattr(ing, "embed_from_sidecar",
                        lambda text, vectors: "[" + ",".join(["0.0"] * 1024) + "]")
    monkeypatch.setattr(ing, "_load_turns", lambda user_folder, date: [])
    # match_request.emit does a real s3.put_object -- FakeS3 above only
    # implements get_object/get_paginator, so stub emit to a no-op by
    # default here (Task 4); tests that care about the emit call override
    # this explicitly (mirrors test_lambda_item_writer.py's wired fixture).
    monkeypatch.setattr(ing.match_request, "emit", lambda *a, **k: None)
    return monkeypatch


# ---------------------------------------------------------------------------
# S3 event / manual / backfill entry points
# ---------------------------------------------------------------------------

def test_handler_parses_embeddings_event_key(monkeypatch):
    # Phase 4d: the fs-ingest-report trigger migrated from reports/ to
    # embeddings/...vectors.json (embed-report writes the sidecar, which is
    # what now fires ingest). Handler must derive (date, user_folder,
    # report_key) from THAT shape, not the old reports/ key.
    calls = []
    monkeypatch.setattr(
        ing, "ingest_report",
        lambda date, user_folder, key: calls.append((date, user_folder, key))
        or {"skipped": False, "topics": 1, "chunks": 2},
    )
    # S3 event notifications encode spaces as '+' and other specials as %XX —
    # unquote_plus must be applied before the key is used against S3/DB.
    event = {"Records": [{"s3": {"object": {
        "key": "embeddings/2026-03-02/Jarley+Trainor/vectors.json"}}}]}

    result = ing.lambda_handler(event, None)

    assert calls == [("2026-03-02", "Jarley Trainor",
                      "reports/2026-03-02/Jarley Trainor/daily_report.json")]
    assert result == {"results": [{"skipped": False, "topics": 1, "chunks": 2}]}


def test_handler_ignores_non_embeddings_key(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ing, "ingest_report",
        lambda date, user_folder, key: calls.append((date, user_folder, key)),
    )
    event = {"Records": [{"s3": {"object": {"key": "audio_segments/foo/2026-03-02/x.wav"}}}]}

    result = ing.lambda_handler(event, None)

    assert calls == []
    assert result == {"results": []}


# ---------------------------------------------------------------------------
# Identity bridge — site resolution
# ---------------------------------------------------------------------------

def test_resolve_site_name_primary_path(monkeypatch):
    monkeypatch.setattr(
        ing.sites, "get_company_site_by_name",
        lambda conn, cid, name: {"id": "site-1", "name": name}
        if name == "SB1108 Ellesmere College" else None,
    )
    report = {"site": "SB1108 Ellesmere College", "topics": []}

    site = ing.resolve_site(None, "co-1", report, "Jarley_Trainor")

    assert site == {"id": "site-1", "name": "SB1108 Ellesmere College"}


def test_resolve_site_fallback_via_slug_or_membership(monkeypatch):
    # report['site'] does not match anything directly -> resolve_site must
    # fall through to a DB-driven lookup of the REPORTING USER's own site
    # membership (folder_name -> user row -> accessible_site_ids -> site) --
    # never a name-matching heuristic against user_mapping.json.
    monkeypatch.setattr(ing.sites, "get_company_site_by_name",
                        lambda conn, cid, name: None)
    monkeypatch.setattr(
        ing.users, "get_by_folder_name",
        lambda conn, cid, folder_name: {"id": "u-9", "global_role": "worker"}
        if folder_name == "Jarley_Trainor" else None,
    )
    monkeypatch.setattr(
        ing.memberships, "accessible_site_ids",
        lambda conn, user_id, global_role: ["site-9"] if user_id == "u-9" else [],
    )
    monkeypatch.setattr(
        ing.sites, "get_site",
        lambda conn, site_id: {"id": "site-9", "name": "SB1108 Ellesmere College"}
        if site_id == "site-9" else None,
    )
    report = {"site": "some transcription noise", "topics": []}

    site = ing.resolve_site(None, "co-1", report, "Jarley_Trainor")

    assert site == {"id": "site-9", "name": "SB1108 Ellesmere College"}


def test_resolve_site_fallback_skips_for_all_scope(monkeypatch):
    # F4 (Fable review): admin/gm have no single "home" site --
    # accessible_site_ids returns EVERY company site for ALL scope with no
    # ordering, so falling back to site_ids[0] would attribute the report to
    # an arbitrary site. The membership fallback must be skipped (None) for
    # ALL-scope (admin/gm) users -- never called at all.
    monkeypatch.setattr(ing.sites, "get_company_site_by_name",
                        lambda conn, cid, name: None)
    monkeypatch.setattr(
        ing.users, "get_by_folder_name",
        lambda conn, cid, folder_name: {"id": "u-admin", "global_role": "admin"}
        if folder_name == "Ben_Lin" else None,
    )
    called = []
    monkeypatch.setattr(
        ing.memberships, "accessible_site_ids",
        lambda conn, user_id, global_role: called.append(user_id) or ["site-1", "site-2"],
    )
    report = {"site": "some transcription noise", "topics": []}

    site = ing.resolve_site(None, "co-1", report, "Ben_Lin")

    assert site is None
    assert called == []  # ALL-scope skip happens before accessible_site_ids is ever called


def test_resolve_site_double_miss_skips(monkeypatch):
    # Real 2026-03-20 case: report['site'] is not a real site, and the
    # reporting user has no resolvable site membership either -> None
    # (caller skips, never invents a site).
    monkeypatch.setattr(ing.sites, "get_company_site_by_name",
                        lambda conn, cid, name: None)
    monkeypatch.setattr(ing.users, "get_by_folder_name",
                        lambda conn, cid, folder_name: None)
    report = {"site": "BD Opportunity Brainstorm", "topics": []}

    site = ing.resolve_site(None, "co-1", report, "Jarley_Trainor")

    assert site is None


def test_site_bridge_miss_skips(wired):
    # Real 2026-03-20 case: report['site'] is not a real site, and the
    # reporting user has no resolvable identity/membership either -> skip,
    # zero writes (full ingest_report path, not just resolve_site).
    wired.setattr(ing, "_s3_client", FakeS3({REPORT_KEY: json.dumps(
        make_report(site="BD Opportunity Brainstorm"))}))
    wired.setattr(ing.sites, "get_company_site_by_name", lambda conn, cid, name: None)

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
# S3 vector-sidecar key derivation (reports/... -> embeddings/...vectors.json)
# ---------------------------------------------------------------------------

def test_sidecar_key_derivation():
    assert ing._sidecar_key("reports/2026-03-02/Jarley_Trainor/daily_report.json") == \
        "embeddings/2026-03-02/Jarley_Trainor/vectors.json"


def test_sidecar_key_derivation_bad_shape_raises():
    with pytest.raises(ValueError):
        ing._sidecar_key("reports/daily_report.json")


# ---------------------------------------------------------------------------
# embed_from_sidecar — sha256(text[:8000]) lookup into the vector-sidecar map
# ---------------------------------------------------------------------------

def test_embed_from_sidecar_hit():
    text = "hello world"
    h = hashlib.sha256(text[:8000].encode("utf-8")).hexdigest()
    vectors = {h: [0.1, 0.2, 0.3]}

    result = ing.embed_from_sidecar(text, vectors)

    assert result == "[0.1,0.2,0.3]"


def test_embed_from_sidecar_truncates_at_8000_chars_before_hashing():
    # Load-bearing: the embed side hashes the SAME truncated text. A chunk
    # longer than 8000 chars must hash on the first 8000 chars only, or every
    # lookup for long chunks would miss.
    long_text = "x" * 9000
    h = hashlib.sha256(long_text[:8000].encode("utf-8")).hexdigest()
    vectors = {h: [9.9]}

    result = ing.embed_from_sidecar(long_text, vectors)

    assert result == "[9.9]"


def test_embed_from_sidecar_missing_raises():
    with pytest.raises(KeyError, match="no precomputed vector for chunk hash"):
        ing.embed_from_sidecar("some text", {})


# ---------------------------------------------------------------------------
# ingest_report end-to-end: loads the sidecar, looks up real chunk-text
# hashes, and insert_chunk receives the looked-up vector (not a Bedrock call).
# ---------------------------------------------------------------------------

def test_ingest_reads_sidecar(wired):
    report = ing.json.loads(json.dumps(make_report()))
    expected_chunks = ing.chunk_report(report)  # no transcripts -> topic chunks only
    vectors = {}
    for c in expected_chunks:
        h = hashlib.sha256(c["chunk_text"][:8000].encode("utf-8")).hexdigest()
        vectors[h] = [0.5, 0.25]

    load_calls = []
    wired.setattr(ing, "_load_vectors",
                  lambda bucket, sidecar_key: load_calls.append((bucket, sidecar_key)) or vectors)
    # Undo the wired fixture's canned embed_from_sidecar stub -- exercise the
    # REAL sha256 lookup for this test.
    wired.setattr(ing, "embed_from_sidecar", _real_embed_from_sidecar)

    inserted_embeddings = []
    wired.setattr(
        ing.chunks, "insert_chunk",
        lambda conn, site_id, report_date, chunk_type, chunk_text, embedding, **kw:
            inserted_embeddings.append(embedding) or {"id": "chunk-x"},
    )

    result = ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert result["skipped"] is False
    assert load_calls == [(ing.S3_BUCKET, "embeddings/2026-03-02/Jarley_Trainor/vectors.json")]
    assert inserted_embeddings  # at least the topic chunk was inserted
    assert all(e == "[0.5,0.25]" for e in inserted_embeddings)
    assert not hasattr(ing, "bedrock")  # bedrock() removed entirely


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


def test_backfill_still_lists_reports(monkeypatch):
    # The ingest trigger migrated to embeddings/...vectors.json, but backfill
    # is unchanged: it still lists the reports/ prefix directly (each
    # ingest_report call derives its own sidecar path from the report key).
    listed_prefixes = []

    class _FakeBackfillPaginator:
        def paginate(self, Bucket, Prefix):
            listed_prefixes.append(Prefix)
            yield {"Contents": [{"Key": "reports/2026-03-02/A/daily_report.json"},
                                 {"Key": "embeddings/2026-03-02/A/vectors.json"}]}

    class _FakeBackfillS3:
        def get_paginator(self, op):
            assert op == "list_objects_v2"
            return _FakeBackfillPaginator()

    monkeypatch.setattr(ing, "s3", lambda: _FakeBackfillS3())
    calls = []
    monkeypatch.setattr(
        ing, "ingest_report",
        lambda date, user_folder, key: calls.append(key)
        or {"skipped": False, "topics": 0, "chunks": 0},
    )

    result = ing.run_backfill()

    assert listed_prefixes == [ing.REPORTS_PREFIX]
    assert ing.REPORTS_PREFIX == "reports/"
    # Non-report keys under the same listing (e.g. a stray embeddings/ key)
    # must not be treated as a report.
    assert calls == ["reports/2026-03-02/A/daily_report.json"]
    assert result["processed"] == 1


# ---------------------------------------------------------------------------
# User bridge — direct folder_name lookup, null on miss
# ---------------------------------------------------------------------------

def test_resolve_user_by_folder_name(monkeypatch):
    monkeypatch.setattr(
        ing.users, "get_by_folder_name",
        lambda conn, cid, folder_name: {"id": "u-42", "folder_name": folder_name}
        if folder_name == "Jarley_Trainor" else None,
    )

    user_id = ing.resolve_user(None, "co-1", "Jarley_Trainor")

    assert user_id == "u-42"


def test_resolve_user_miss_none(monkeypatch):
    monkeypatch.setattr(ing.users, "get_by_folder_name",
                        lambda conn, cid, folder_name: None)

    assert ing.resolve_user(None, "co-1", "Unknown_Person") is None


def test_resolve_user_field_only_folder_hits(monkeypatch):
    # Task 2 enrollment: field_only reporters (MPI1/MPI2/James_Lamb/
    # Jack_Gibson) get a folder_name-keyed row with no Cognito login -- they
    # must resolve exactly like a login user, straight off folder_name (no
    # name-join heuristic).
    monkeypatch.setattr(
        ing.users, "get_by_folder_name",
        lambda conn, cid, folder_name: {"id": "u-field-1", "kind": "field_only"}
        if folder_name == "MPI1" else None,
    )

    user_id = ing.resolve_user(None, "co-1", "MPI1")

    assert user_id == "u-field-1"


def test_user_bridge_null_on_miss(wired):
    wired.setattr(ing.users, "get_by_folder_name",
                  lambda conn, cid, folder_name: None)

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
    # user bridge misses for both (wired fixture's get_by_folder_name returns None)

    ing.ingest_report("2026-03-02", "MPI1", key_a)
    ing.ingest_report("2026-03-02", "MPI2", key_b)

    keys_used = {k for _, k in deleted_keys}
    assert keys_used == {key_a, key_b}
    assert deleted_keys.count(("chunks", key_a)) == 1
    assert deleted_keys.count(("chunks", key_b)) == 1


def test_ingest_missing_sidecar_skips(wired):
    """M3 (Fable): zero-chunk report has no sidecar -> backfill path's
    _load_vectors 404s -> clean skip, not a failure."""
    def _raise(bucket, key):
        raise ing.s3().exceptions.NoSuchKey("no such key")
    wired.setattr(ing, "_load_vectors", _raise)
    res = ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)
    assert res.get("skipped") is True
    assert "sidecar" in res.get("reason", "")


# ---------------------------------------------------------------------------
# Task 4 — match_request.emit is called with the freshly-written report
# topics AFTER the connection block commits, and only when the report has
# at least one topic (never on a zero-topic report or an identity-bridge skip).
# ---------------------------------------------------------------------------

def test_match_request_emitted_after_report_topic_write(wired):
    calls = []
    wired.setattr(
        ing.match_request, "emit",
        lambda s3_client, bucket, site_id, report_date, source_key, topics:
            calls.append((bucket, site_id, report_date, source_key, topics))
            or "match_requests/site-1/2026-03-02/abc123.json",
    )

    result = ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert result["skipped"] is False
    assert len(calls) == 1
    bucket, site_id, report_date, source_key, topics = calls[0]
    assert bucket == ing.S3_BUCKET
    assert site_id == "site-1"
    assert report_date == "2026-03-02"
    assert source_key == REPORT_KEY
    assert topics == [{
        "topic_id": "topic-uuid-0",
        "title": "Safety Briefing",
        "summary": "Discussed PPE requirements.",
        "user_id": None,
        "action_items": [{"text": "Order more hard hats"}],
    }]


def test_match_request_not_emitted_on_zero_topic_report(wired, monkeypatch):
    calls = []
    wired.setattr(ing.match_request, "emit", lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr(
        ing, "_s3_client",
        FakeS3({REPORT_KEY: json.dumps(make_report(topics=[]))}),
    )

    result = ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert result["skipped"] is False
    assert result["topics"] == 0
    assert calls == []


def test_match_request_not_emitted_on_identity_skip(wired):
    calls = []
    wired.setattr(ing.match_request, "emit", lambda *a, **k: calls.append(a) or None)
    wired.setattr(ing.sites, "get_company_site_by_name", lambda conn, cid, name: None)

    result = ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert result["skipped"] is True
    assert calls == []


class TestResolveCompany:
    """Task 2 (prod-isolation): resolve_company routes a lake object to its
    owning company. Pinned (test stack) vs global-folder (prod stack)."""

    def test_pinned_when_multi_tenant_off(self, monkeypatch):
        monkeypatch.setattr(ing, "MULTI_TENANT", False)
        monkeypatch.setattr(ing.users, "get_by_folder_name_global",
                            lambda conn, f: (_ for _ in ()).throw(AssertionError("must not be called")))
        monkeypatch.setattr(ing.companies, "get_company_by_name",
                            lambda conn, name: {"id": "internal-co", "name": name})
        assert ing.resolve_company(FakeConn(), "Cust_User")["id"] == "internal-co"

    def test_global_folder_lookup_when_on(self, monkeypatch):
        monkeypatch.setattr(ing, "MULTI_TENANT", True)
        monkeypatch.setattr(ing.users, "get_by_folder_name_global",
                            lambda conn, f: {"id": "u1", "company_id": "cust-co", "folder_name": f})
        monkeypatch.setattr(ing.companies, "get_company_by_id",
                            lambda conn, cid: {"id": cid, "name": "Pilot Co"})
        assert ing.resolve_company(FakeConn(), "Cust_User")["id"] == "cust-co"

    def test_falls_back_to_pin_on_unknown_folder(self, monkeypatch):
        monkeypatch.setattr(ing, "MULTI_TENANT", True)
        monkeypatch.setattr(ing.users, "get_by_folder_name_global", lambda conn, f: None)
        monkeypatch.setattr(ing.companies, "get_company_by_name",
                            lambda conn, name: {"id": "internal-co", "name": name})
        assert ing.resolve_company(FakeConn(), "Legacy_Device")["id"] == "internal-co"
