"""Integration: PR #972 review, fix round 1 #1 -- a report-sourced topic-type chunk,
bound (on an authority-flip defer day) to an extraction topic, must still be reachable by
a recording delete after that extraction topic is later superseded and R21 unbinds it.

Before this fix, `chunk_report`'s topic-type chunks carried no `metadata.source_files`
(unlike `chunk_transcripts`' transcript_window chunks -- see `chunking._window_metadata`).
On a defer day the chunk's `topic_id` is an EXTRACTION topic (matched by
`_match_report_topics_to_extraction`), not this report_key's own row, so it can outlive
this ingest and later be superseded by an unrelated re-extraction of the same session --
R21 then unbinds it (`topic_id -> NULL`). With no `source_files`,
`chunks.SESSION_CHUNK_PREDICATE` could match this chunk by NEITHER arm any more, so
`archive_chunks_for_session` (the recording-delete path) could not find it: a deleted
recording's report-topic summary stayed searchable -- a privacy leak. Fixed by stamping the
matched extraction topic's own `source_s3_key` (which contains the exact session base) into
the chunk's `source_files`.

Drives the real `lambda_ingest.ingest_report` (defer branch) against a real Postgres, then
the same repository calls `src/lambda_org_api.py`'s `delete_recordings_endpoint` issues for
one recording (`redactions.create_recording_tombstone` +
`chunks.archive_chunks_for_session`) -- not the full HTTP handler, which needs a caller/auth
shape this test does not exercise.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import io
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import chunks, companies, memberships, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-09-30"
_CANNED_EMBEDDING = "[" + ",".join(["0.0"] * 1024) + "]"

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
            "action_items": [],
            "safety_flags": [],
        }],
    }


def test_a_defer_day_topic_chunk_is_still_deletable_after_its_extraction_topic_is_superseded(
        monkeypatch, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = None
    try:
        company_name = f"DeferDelete-Co-{tag}"
        co = companies.create_company(seed, company_name)
        site_name = f"DeferDelete-Site-{tag}"
        site = sites.create_site(seed, co["id"], site_name)
        folder = f"DeferDelete-{tag}"
        user_name = "Ada L"
        user = users.upsert_field_only_user(seed, co["id"], folder, "Ada", "L", "worker")
        memberships.add_membership(seed, user["id"], site["id"], "worker")

        session_base = f"sid{tag}"
        ext_key = f"extractions/{folder}/{DATE}/{session_base}.json"
        # The day's authoritative extraction topic -- present BEFORE the report ingest, so
        # `_should_defer` (has_topics_for_source_prefix) takes the defer branch. Same title
        # as the report topic below so `_overlap_or_title_score` matches on title alone
        # (ratio 1.0) -- no time-overlap arithmetic needed for this test.
        ext_topic = topics.upsert_topic(
            seed, site["id"], DATE, "Safety Briefing", user_id=user["id"],
            source_s3_key=ext_key, summary="live extraction summary")

        report_key = f"reports/{DATE}/{folder}/daily_report.json"
        report = _report(site_name, user_name)
        fake_s3 = _FakeS3({report_key: json.dumps(report)})

        monkeypatch.setattr(lambda_ingest, "AUTHORITY_FLIP", True)
        monkeypatch.setattr(lambda_ingest, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_ingest, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_ingest, "_load_vectors", lambda bucket, sidecar_key: {})
        monkeypatch.setattr(lambda_ingest, "embed_from_sidecar",
                            lambda text, vectors: _CANNED_EMBEDDING)
        monkeypatch.setattr(lambda_ingest, "_load_turns", lambda user_folder, date: [])
        monkeypatch.setattr(lambda_ingest.match_request, "emit", lambda *a, **k: None)

        result = lambda_ingest.ingest_report(DATE, folder, report_key)
        assert result["skipped"] is False, result
        assert result["topics"] == 1, "the report topic must have matched the ext topic"

        chunk = seed.execute(
            "SELECT id, topic_id, metadata FROM report_chunks WHERE source_s3_key=%s "
            "AND chunk_type='topic'", (report_key,)).fetchone()
        assert chunk is not None, "the defer-day ingest must still write a topic chunk"
        chunk_id, chunk_topic_id, chunk_metadata = chunk
        assert str(chunk_topic_id) == str(ext_topic["id"]), (
            "the topic chunk must be bound to the matched EXTRACTION topic")
        assert chunk_metadata.get("source_files") == [ext_key], (
            "a topic-type report chunk must carry the matched extraction topic's own "
            "source_s3_key as its source_files -- the only thing that lets a recording "
            "delete still find it once its topic_id is unbound")

        # A LATER, unrelated re-extraction of the SAME session supersedes the extraction
        # topic -- R21 unbinds the chunk (topic_id -> NULL) in the same call.
        topics.supersede_topics_for_source(seed, ext_key, "final:later-pass")
        unbound = seed.execute(
            "SELECT topic_id FROM report_chunks WHERE id=%s", (chunk_id,)).fetchone()
        assert unbound[0] is None, "R21 must have unbound the chunk by now"

        # The same repository calls delete_recordings_endpoint issues (lambda_org_api.py)
        # for one recording: tombstone the source prefix, enumerate its topics (superseded
        # or not), then archive its chunks by session.
        batch_id = str(uuid.uuid4())
        prefix = f"extractions/{folder}/{DATE}/{session_base}"
        redactions.create_recording_tombstone(
            seed, co["id"], prefix, "deleted by the user", user["id"], "worker",
            batch_id=batch_id)
        topic_ids = [row["id"] for row in topics.list_topics_for_source_prefix(
            seed, prefix, include_superseded=True)]
        assert str(ext_topic["id"]) in [str(t) for t in topic_ids]

        archived = chunks.archive_chunks_for_session(
            seed, session_base, topic_ids, batch_id, company_id=co["id"])

        assert archived == 1, (
            "the topic chunk must be archivable by its source_files arm even though its "
            "topic_id is now NULL -- otherwise a deleted recording's report-topic summary "
            "stays searchable")
        still_live = seed.execute(
            "SELECT 1 FROM report_chunks WHERE id=%s", (chunk_id,)).fetchone()
        assert still_live is None, "the chunk must no longer be in the live table"

        results = chunks.search_chunks(seed, [0.0] * 1024, [site["id"]], k=5)
        assert chunk_id not in [r["id"] for r in results], (
            "a deleted recording's report-topic chunk must not be returned by search")
    finally:
        co_id = co["id"] if co is not None else None
        site_id = site["id"] if site is not None else None
        user_id = user["id"] if user is not None else None
        # report_chunks_archive deliberately carries no foreign keys (0044's `LIKE
        # report_chunks` copies none) -- an archived row must not be reachable by CASCADE
        # from a topic/site that later changes, but that also means deleting the site below
        # does not clean it up for us.
        if site_id is not None:
            seed.execute("DELETE FROM report_chunks_archive WHERE site_id=%s", (site_id,))
        if co_id is not None:
            # redactions.actor_user_id REFERENCES users(id) with no ON DELETE action --
            # the tombstone created above (actor_user_id=user["id"]) blocks deleting the
            # user below unless this row goes first.
            seed.execute("DELETE FROM redactions WHERE company_id=%s", (co_id,))
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
                "(SELECT count(*) FROM report_chunks WHERE site_id=%s), "
                "(SELECT count(*) FROM report_chunks_archive WHERE site_id=%s)",
                (co_id, site_id, co_id, site_id, site_id, site_id),
            ).fetchone()
            assert remaining == (0, 0, 0, 0, 0, 0), f"leaked rows after cleanup: {remaining}"
        seed.close()
