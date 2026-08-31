# tests/unit/test_reindex.py
import json

import pytest

reindex = pytest.importorskip("reindex", reason="requires psycopg (installed in CI)")


class FakeS3:
    def __init__(self):
        self.puts = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts[Key] = json.loads(Body)


def test_keys():
    assert reindex.request_key("2026-07-16", "Ada_L", "t-1") == \
        "reindex_requests/2026-07-16/Ada_L/t-1.json"
    assert reindex.vectors_key("2026-07-16", "Ada_L", "t-1") == \
        "reindex_vectors/2026-07-16/Ada_L/t-1.json"


def test_enqueue_writes_request_with_topic_chunks_and_aliases(monkeypatch):
    topic_row = {"id": "t-1", "site_id": "s-1", "user_id": "u-9",
                 "source_s3_key": "reports/2026-07-16/Ada_L/daily_report.json",
                 "report_date": "2026-07-16", "site_name": "Alpha",
                 "user_name": "Ada L", "time_range": "09:00 - 09:30",
                 "title": "Corrected slab", "category": "progress",
                 "participants": [], "summary": "poured raft",
                 "action_items": [], "safety_observations": [], "findings": [],
                 "photos": []}
    monkeypatch.setattr(reindex.topics, "get_topic_full", lambda conn, tid: topic_row)
    monkeypatch.setattr(reindex.aliases, "list_active",
                        lambda conn, cid, site_ids=None: [
                            {"wrong_term": "Mackon", "right_term": "McCahon"}])
    monkeypatch.setattr(reindex, "_company_id_for_site", lambda conn, sid: "co-1")
    # Task 1b: render_report_shape (invoked here via the lazy lambda_org_api
    # import) now looks up redaction status whenever a real conn is passed;
    # this test's conn is a bare object() with no .cursor(), so stub it.
    import lambda_org_api
    monkeypatch.setattr(lambda_org_api.redactions, "list_active_for_topics",
                        lambda conn, ids: {})
    # Task 5: enqueue_topic_reindex's delete-only guard also checks redaction
    # status directly; stub it too (this test's conn has no .cursor()).
    monkeypatch.setattr(reindex.redactions, "is_topic_redacted", lambda conn, tid: False)
    # The guard now checks the SOURCE arm too, because the topic arm names a uuid
    # that the nightly rebuild replaces. Same reason this one is stubbed.
    monkeypatch.setattr(reindex.redactions, "is_source_deleted", lambda conn, key: False)

    s3 = FakeS3()
    key = reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-1", "Ada_L", "2026-07-16")
    assert key == "reindex_requests/2026-07-16/Ada_L/t-1.json"
    req = s3.puts[key]
    assert req["topic_id"] == "t-1"
    assert req["site_id"] == "s-1"
    assert req["report_key"] == "reports/2026-07-16/Ada_L/daily_report.json"
    assert req["aliases"] == [{"wrong_term": "Mackon", "right_term": "McCahon"}]
    assert any("Corrected slab" in c["chunk_text"] for c in req["topic_chunks"])


def test_apply_vectors_deletes_then_inserts(monkeypatch):
    deleted, inserted = {}, []
    monkeypatch.setattr(reindex.chunks, "delete_chunks_for_topic",
                        lambda conn, tid: deleted.setdefault("tid", tid))
    monkeypatch.setattr(reindex.chunks, "insert_chunk",
                        lambda conn, *a, **k: inserted.append((a, k)))
    result = {
        "topic_id": "t-1", "site_id": "s-1", "user_id": "u-9",
        "report_date": "2026-07-16",
        "source_s3_key": "reports/2026-07-16/Ada_L/daily_report.json",
        "chunks": [
            {"chunk_type": "topic", "chunk_text": "x", "metadata": {},
             "embedding": [0.1] * 1024},
        ],
    }
    n = reindex.apply_vectors(object(), result)
    assert n == 1
    assert deleted["tid"] == "t-1"
    assert inserted[0][1]["topic_id"] == "t-1"
    assert inserted[0][1]["source_s3_key"].endswith("daily_report.json")


# --------------------------------------------------------------------------
# The arm that survives the rebuild
# --------------------------------------------------------------------------

DELETED_SOURCE = "extractions/Ada_L/2026-07-16/sid" + "b" * 32 + ".json"


def _topic_row(source):
    return {"id": "t-9", "site_id": "s-1", "user_id": "u-9",
            "source_s3_key": source, "report_date": "2026-07-16",
            "site_name": "Alpha", "user_name": "Ada L", "time_range": "09:00 - 09:30",
            "title": "Slab pour", "category": "progress", "participants": [],
            "summary": "poured raft", "action_items": [], "safety_observations": [],
            "findings": [], "photos": []}


def _wire(monkeypatch, *, topic_redacted, source_deleted, source=DELETED_SOURCE):
    monkeypatch.setattr(reindex.topics, "get_topic_full",
                        lambda conn, tid: _topic_row(source))
    monkeypatch.setattr(reindex.aliases, "list_active",
                        lambda conn, cid, site_ids=None: [])
    monkeypatch.setattr(reindex, "_company_id_for_site", lambda conn, sid: "co-1")
    import lambda_org_api
    monkeypatch.setattr(lambda_org_api.redactions, "list_active_for_topics",
                        lambda conn, ids: {})
    monkeypatch.setattr(reindex.redactions, "is_topic_redacted",
                        lambda conn, tid: topic_redacted)
    monkeypatch.setattr(reindex.redactions, "is_source_deleted",
                        lambda conn, key: source_deleted)


def test_a_rebuilt_topic_whose_recording_was_deleted_is_not_re_embedded(monkeypatch):
    """The state the day after a delete: `lambda_ingest` re-inserted the day's
    topics under new uuids, so the topic-keyed tombstone no longer names this
    row and `is_topic_redacted` says False. Only the source arm still knows.

    This is the one read in the system where missing a deletion does not merely
    show the content -- it EMBEDS it, putting deleted text back into the index
    Ask retrieves from, where it can then be quoted at the person who deleted it.
    """
    _wire(monkeypatch, topic_redacted=False, source_deleted=True)
    s3 = FakeS3()
    key = reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-9", "Ada_L", "2026-07-16")
    req = s3.puts[key]
    assert req["delete_only"] is True, "a deleted recording's text was queued for embedding"
    assert req["topic_chunks"] == []


def test_the_topic_arm_still_works_on_its_own(monkeypatch):
    """Before the rebuild, the topic arm is the one that fires. Adding the source
    arm must not have replaced it."""
    _wire(monkeypatch, topic_redacted=True, source_deleted=False)
    s3 = FakeS3()
    key = reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-9", "Ada_L", "2026-07-16")
    assert s3.puts[key]["delete_only"] is True


def test_a_live_topic_is_still_embedded(monkeypatch):
    """The cost of the fix, bounded: nothing that should be indexed stopped being."""
    _wire(monkeypatch, topic_redacted=False, source_deleted=False)
    s3 = FakeS3()
    key = reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-9", "Ada_L", "2026-07-16")
    req = s3.puts[key]
    assert not req.get("delete_only")
    assert any("Slab pour" in c["chunk_text"] for c in req["topic_chunks"])
