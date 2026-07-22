# tests/unit/test_lambda_embed_report_reindex.py
import json

import pytest

mod = pytest.importorskip("lambda_embed_report",
                          reason="requires psycopg (installed in CI)")


class FakeS3:
    def __init__(self, objects):
        self._objects = objects
        self.puts = {}

    def get_object(self, Bucket, Key):
        return {"Body": type("B", (), {"read": lambda s: self._objects[Key]})()}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts[Key] = json.loads(Body)


def test_reindex_event_embeds_topic_chunks_and_writes_vectors(monkeypatch):
    req = {
        "topic_id": "t-1", "site_id": "s-1", "user_id": "u-9",
        "report_date": "2026-07-16",
        "source_s3_key": "reports/2026-07-16/Ada_L/daily_report.json",
        "report_key": None, "topic_seq": 0, "folder": "Ada_L", "date": "2026-07-16",
        "aliases": [], "topic_chunks": [
            {"chunk_type": "topic", "chunk_text": "Corrected slab", "metadata": {}}],
    }
    key = "reindex_requests/2026-07-16/Ada_L/t-1.json"
    s3 = FakeS3({key: json.dumps(req).encode("utf-8")})
    monkeypatch.setattr(mod, "s3", lambda: s3)
    monkeypatch.setattr(mod.dashscope_utils, "embed", lambda texts: [[0.5] * 1024 for _ in texts])

    out = mod.lambda_handler({"Records": [{"s3": {"object": {"key": key}}}]}, None)
    vkey = "reindex_vectors/2026-07-16/Ada_L/t-1.json"
    assert vkey in s3.puts
    result = s3.puts[vkey]
    assert result["topic_id"] == "t-1"
    assert result["chunks"][0]["chunk_text"] == "Corrected slab"
    assert len(result["chunks"][0]["embedding"]) == 1024


def test_enqueue_delete_only_for_non_work(monkeypatch):
    import reindex
    puts = {}

    class S3:
        def put_object(self, Bucket, Key, Body, ContentType):
            import json as _j
            puts["key"] = Key; puts["body"] = _j.loads(Body)

    monkeypatch.setattr(reindex.topics, "get_topic_full",
                        lambda conn, tid: {"id": tid, "site_id": "s1", "user_id": None,
                                           "report_date": "2026-07-21",
                                           "source_s3_key": "extractions/U/2026-07-21/x.json",
                                           "work_class": "non_work"})
    monkeypatch.setattr(reindex, "_company_id_for_site", lambda conn, sid: "c1")
    monkeypatch.setattr(reindex.aliases, "list_active", lambda *a, **k: [])
    monkeypatch.setattr(reindex.redactions, "is_topic_redacted", lambda conn, tid: False)
    key = reindex.enqueue_topic_reindex(S3(), "bucket", object(), "t-1", "U", "2026-07-21")
    assert key is not None
    assert puts["body"]["topic_chunks"] == []          # delete-only: no vectors re-inserted
    assert puts["body"]["delete_only"] is True


def test_embed_writes_empty_vectors_artifact_for_delete_only(monkeypatch):
    # A delete_only request with no chunks must still emit a vectors artifact
    # (chunks: []) so ingest deletes the topic's vectors -- NOT be skipped.
    req = {
        "topic_id": "t-1", "site_id": "s-1", "user_id": "u-9",
        "report_date": "2026-07-16",
        "source_s3_key": "reports/2026-07-16/Ada_L/daily_report.json",
        "report_key": None, "topic_seq": None, "folder": "Ada_L", "date": "2026-07-16",
        "aliases": [], "topic_chunks": [], "delete_only": True,
    }
    key = "reindex_requests/2026-07-16/Ada_L/t-1.json"
    s3 = FakeS3({key: json.dumps(req).encode("utf-8")})
    monkeypatch.setattr(mod, "s3", lambda: s3)
    monkeypatch.setattr(mod.dashscope_utils, "embed", lambda texts: [[0.5] * 1024 for _ in texts])

    out = mod.lambda_handler({"Records": [{"s3": {"object": {"key": key}}}]}, None)
    vkey = "reindex_vectors/2026-07-16/Ada_L/t-1.json"
    assert vkey in s3.puts
    result = s3.puts[vkey]
    assert result["topic_id"] == "t-1"
    assert result["chunks"] == []
