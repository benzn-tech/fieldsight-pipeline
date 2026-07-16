"""
Unit tests for lambda_voice_audit — SP-Ask Task 6 (TDD). get_connection and the
repositories are stubbed so no DB is needed; asserts company_id resolution +
best-effort posture.
"""
import pytest

lva = pytest.importorskip("lambda_voice_audit", reason="requires psycopg import path")


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def wire(monkeypatch, *, user=None, insert=None):
    monkeypatch.setattr(lva, "get_connection", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(lva.users, "get_user_by_sub", lambda conn, sub: user)
    calls = []

    def fake_insert(conn, caller_sub, transcript, answer, company_id=None):
        calls.append({"caller_sub": caller_sub, "transcript": transcript,
                      "answer": answer, "company_id": company_id})
        return "row-1" if insert is None else insert
    monkeypatch.setattr(lva.voice_ask_log, "insert_voice_ask", fake_insert)
    return calls


def test_writes_row_with_resolved_company(monkeypatch):
    calls = wire(monkeypatch, user={"id": "u-1", "company_id": "c-9"})
    res = lva.lambda_handler(
        {"caller_sub": "sub-1", "transcript": "t", "answer": "a"}, None)
    assert res["written"] is True
    assert res["id"] == "row-1"
    assert calls[0] == {"caller_sub": "sub-1", "transcript": "t",
                        "answer": "a", "company_id": "c-9"}


def test_unprovisioned_caller_writes_null_company(monkeypatch):
    calls = wire(monkeypatch, user=None)
    lva.lambda_handler({"caller_sub": "sub-x", "transcript": "t", "answer": "a"}, None)
    assert calls[0]["company_id"] is None


def test_missing_caller_sub_not_written(monkeypatch):
    calls = wire(monkeypatch, user=None)
    res = lva.lambda_handler({"transcript": "t", "answer": "a"}, None)
    assert res["written"] is False
    assert calls == []


def test_db_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(lva, "get_connection",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    res = lva.lambda_handler({"caller_sub": "s", "transcript": "t", "answer": "a"}, None)
    assert res["written"] is False
    assert "db down" in res["error"]
