# tests/unit/test_lambda_ingest_reindex.py
import json

import pytest

mod = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeS3:
    def __init__(self, objects):
        self._objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": type("B", (), {"read": lambda s: self._objects[Key]})()}


def test_vectors_event_applies_reindex(monkeypatch):
    result = {"topic_id": "t-1", "site_id": "s-1", "user_id": "u-9",
              "report_date": "2026-07-16",
              "source_s3_key": "reports/2026-07-16/Ada_L/daily_report.json",
              "chunks": [{"chunk_type": "topic", "chunk_text": "x",
                          "metadata": {}, "embedding": [0.1] * 1024}]}
    vkey = "reindex_requests/2026-07-16/Ada_L/t-1.vectors.json"
    s3 = FakeS3({vkey: json.dumps(result).encode("utf-8")})
    monkeypatch.setattr(mod, "s3", lambda: s3)
    monkeypatch.setattr(mod, "get_connection", lambda: FakeConn())
    applied = {}
    monkeypatch.setattr(mod.reindex, "apply_vectors",
                        lambda conn, res: applied.update({"topic": res["topic_id"]}) or 1)

    out = mod.lambda_handler({"Records": [{"s3": {"object": {"key": vkey}}}]}, None)
    assert applied["topic"] == "t-1"
