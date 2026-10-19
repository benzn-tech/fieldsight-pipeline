"""Ingest's documents route: the key decides, the artifact must agree with its key, a
failure never leaves the document pending (R9), and lambda_ingest reaches the apply module
only lazily (R14: lambda_item_writer imports lambda_ingest at module level)."""
import ast
import contextlib
import io
import json

import pytest

li = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")
import project_doc_apply as pda  # noqa: E402

CO = "c0000000-0000-0000-0000-000000000001"
DOC = "d0000000-0000-0000-0000-000000000001"
KEY = f"project_doc_vectors/{CO}/{DOC}.json"


class _S3:
    def __init__(self, body=None, raw=None, exc=None):
        self.body, self.raw, self.exc = body, raw, exc

    def get_object(self, Bucket, Key):
        if self.exc:
            raise self.exc
        data = self.raw if self.raw is not None else json.dumps(self.body).encode()
        return {"Body": io.BytesIO(data)}


class _Conn:
    """Only what the route touches: a transaction context. The SQL runs in the
    integration test."""
    def __init__(self):
        self.transactions = 0

    @contextlib.contextmanager
    def transaction(self):
        self.transactions += 1
        yield


@pytest.fixture
def world(monkeypatch):
    w = {"applied": [], "failed": [], "conns": [], "apply_raises": None, "fail_raises": None}

    def apply_result(conn, result):
        assert conn.transactions == 1           # inside conn.transaction() (Task 0 carry)
        if w["apply_raises"]:
            raise w["apply_raises"]
        w["applied"].append(result)
        return "indexed"

    def fail_pending(conn, doc_id, company_id):
        assert conn.transactions == 1
        if w["fail_raises"]:
            raise w["fail_raises"]
        w["failed"].append((doc_id, company_id))
        return True

    def get_connection():
        c = _Conn()
        w["conns"].append(c)
        return contextlib.nullcontext(c)

    monkeypatch.setattr(pda.project_docs, "apply_result", apply_result)
    monkeypatch.setattr(pda.project_docs, "fail_pending", fail_pending)
    monkeypatch.setattr(pda, "get_connection", get_connection)
    return w


def test_the_artifact_is_applied(world):
    out = pda.apply_key(KEY, _S3({"document_id": DOC, "company_id": CO, "status": "indexed"}), "b")
    assert out == {"project_doc": KEY, "outcome": "indexed"} and len(world["applied"]) == 1
    assert world["failed"] == []


def test_an_artifact_that_disagrees_with_its_key_is_not_applied(world):
    out = pda.apply_key(KEY, _S3({"document_id": DOC, "company_id": "other"}), "b")
    assert out["outcome"] == "mismatch" and world["applied"] == []
    # the key's document is not left pending -- marked by the KEY's ids, not the body's
    assert world["failed"] == [(DOC, CO)]


def test_a_key_that_is_not_a_vectors_key_is_ignored(world):
    assert pda.apply_key("project_doc_vectors/x.json", _S3({}), "b")["outcome"] == "ignored"
    assert world["conns"] == []


@pytest.mark.parametrize("s3", [_S3(raw=b"{not json"), _S3(raw=b"[1, 2]"),
                                _S3(exc=RuntimeError("AccessDenied"))])
def test_an_unreadable_artifact_marks_the_document_error(world, s3):
    out = pda.apply_key(KEY, s3, "b")
    assert out["outcome"] == "unreadable_artifact"
    assert world["applied"] == [] and world["failed"] == [(DOC, CO)]


def test_an_apply_that_raises_marks_the_document_error_on_a_fresh_connection(world, caplog):
    world["apply_raises"] = RuntimeError("boom")
    out = pda.apply_key(KEY, _S3({"document_id": DOC, "company_id": CO}), "b")
    assert out == {"project_doc": KEY, "outcome": "error"}
    assert world["failed"] == [(DOC, CO)] and len(world["conns"]) == 2
    assert any(r.exc_info for r in caplog.records)         # logger.exception, not swallowed


def test_when_even_the_error_mark_fails_the_exception_reaches_s3_for_redelivery(world):
    world["apply_raises"] = RuntimeError("db down")
    world["fail_raises"] = RuntimeError("db still down")
    with pytest.raises(RuntimeError, match="db still down"):
        pda.apply_key(KEY, _S3({"document_id": DOC, "company_id": CO}), "b")


def test_lambda_ingest_routes_the_prefix(monkeypatch):
    seen = []
    monkeypatch.setattr(pda, "apply_key", lambda key, client, bucket: seen.append(key) or {})
    monkeypatch.setattr(li, "s3", lambda: object())
    li.lambda_handler({"Records": [{"s3": {"object": {"key": KEY}}}]}, None)
    assert seen == [KEY]
    assert li.PROJECT_DOC_VECTORS_RE.match(KEY)
    assert not li.INDEX_VECTORS_RE.match(KEY)
    assert not li.REINDEX_VECTORS_RE.match(KEY)
    for other in ("index_vectors/a/b/c.json", "reindex_vectors/a/b/c.json",
                  "embeddings/2026-10-16/Ben/x.json"):
        assert not li.PROJECT_DOC_VECTORS_RE.match(other)


def test_lambda_ingest_imports_the_apply_module_only_inside_the_handler():
    src = open(li.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top for a in n.names} | {n.module for n in top
                                                       if isinstance(n, ast.ImportFrom)}
    assert not any(n and ("project_doc" in n) for n in names)
    assert "import project_doc_apply" in src
