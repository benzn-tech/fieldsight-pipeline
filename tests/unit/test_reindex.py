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
