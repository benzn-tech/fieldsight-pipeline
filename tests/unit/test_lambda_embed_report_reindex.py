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
