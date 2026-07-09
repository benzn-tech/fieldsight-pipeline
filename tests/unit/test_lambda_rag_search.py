import datetime
import json
import uuid

import pytest

rag = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-04",
}


@pytest.fixture
def wired(monkeypatch):
    """Wire a FakeConn and a default admin caller; tests override as needed."""
    monkeypatch.setattr(rag, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(rag.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    return monkeypatch


def make_event(sub="sub-1", query_embedding=None, k=None):
    ev = {"sub": sub,
          "query_embedding": query_embedding if query_embedding is not None else [0.1] * 1024}
    if k is not None:
        ev["k"] = k
    return ev


def test_missing_sub_or_vector_returns_empty(monkeypatch):
    # Guard must fire BEFORE any DB connection is opened.
    def boom(*a, **k):
        raise AssertionError("get_connection must not be called on a guard miss")
    monkeypatch.setattr(rag, "get_connection", boom)

    res = rag.lambda_handler({"sub": None, "query_embedding": [0.1] * 1024}, None)
    assert res == {"chunks": [], "error": "missing sub or query_embedding"}

    res2 = rag.lambda_handler({"sub": "sub-1", "query_embedding": None}, None)
    assert res2 == {"chunks": [], "error": "missing sub or query_embedding"}

    res3 = rag.lambda_handler({"sub": "sub-1", "query_embedding": []}, None)
    assert res3 == {"chunks": [], "error": "missing sub or query_embedding"}


def test_caller_not_provisioned_returns_error_not_raise(wired):
    res = rag.lambda_handler(make_event(sub="sub-ghost"), None)
    assert res == {"chunks": [], "error": "caller not provisioned"}


def test_admin_uses_company_sites(wired):
    seen = {}
    wired.setattr(rag.sites, "list_company_sites",
                  lambda conn, cid: (seen.update(cid=cid) or [{"id": "s-1"}, {"id": "s-2"}]))
    captured = {}
    wired.setattr(rag.chunks, "search_chunks",
                  lambda conn, qv, site_ids, k=5, date_from=None, date_to=None:
                      (captured.update(site_ids=site_ids) or []))

    res = rag.lambda_handler(make_event(), None)

    assert seen["cid"] == "c-uuid-1"
    assert captured["site_ids"] == ["s-1", "s-2"]
    assert res["site_count"] == 2
    assert res["chunks"] == []


def test_worker_uses_memberships(wired):
    wired.setattr(rag.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    seen = {}
    wired.setattr(rag.memberships, "accessible_site_ids",
                  lambda conn, uid, role: (seen.update(uid=uid, role=role) or ["s-3"]))
    captured = {}
    wired.setattr(rag.chunks, "search_chunks",
                  lambda conn, qv, site_ids, k=5, date_from=None, date_to=None:
                      (captured.update(site_ids=site_ids) or []))

    res = rag.lambda_handler(make_event(), None)

    assert seen == {"uid": "u-uuid-1", "role": "worker"}
    assert captured["site_ids"] == ["s-3"]
    assert res["site_count"] == 1


def test_empty_site_ids_empty_chunks(wired):
    # deny-by-default: a worker with no memberships short-circuits to an
    # empty result WITHOUT calling search_chunks (saves the DB round-trip —
    # WHERE site_id = ANY('{}') would just match no rows anyway).
    wired.setattr(rag.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(rag.memberships, "accessible_site_ids", lambda conn, uid, role: [])

    def boom(*a, **k):
        raise AssertionError("search_chunks must not be called when site_ids is empty")
    wired.setattr(rag.chunks, "search_chunks", boom)

    res = rag.lambda_handler(make_event(), None)

    assert res["chunks"] == []
    assert res["site_count"] == 0


def test_search_chunks_receives_vector_and_k(wired):
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    captured = {}

    def fake_search(conn, qv, site_ids, k=5, date_from=None, date_to=None):
        captured.update(qv=qv, site_ids=site_ids, k=k)
        return [{"chunk_text": "hello"}]

    wired.setattr(rag.chunks, "search_chunks", fake_search)
    vec = [0.5] * 1024

    res = rag.lambda_handler(make_event(query_embedding=vec, k=3), None)

    assert captured["qv"] == vec
    assert captured["k"] == 3
    assert res["chunks"] == [{"chunk_text": "hello"}]


def test_default_k_is_8(wired):
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    captured = {}

    def fake_search(conn, qv, site_ids, k=5, date_from=None, date_to=None):
        captured["k"] = k
        return []

    wired.setattr(rag.chunks, "search_chunks", fake_search)

    rag.lambda_handler(make_event(), None)  # no "k" key in event

    assert captured["k"] == 8


def test_k_is_clamped_to_1_32(wired):
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    captured = {}

    def fake_search(conn, qv, site_ids, k=5, date_from=None, date_to=None):
        captured["k"] = k
        return []

    wired.setattr(rag.chunks, "search_chunks", fake_search)

    rag.lambda_handler(make_event(k=999), None)
    assert captured["k"] == 32

    rag.lambda_handler(make_event(k=-5), None)
    assert captured["k"] == 1


def test_garbage_k_falls_back_to_default(wired):
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    captured = {}

    def fake_search(conn, qv, site_ids, k=5, date_from=None, date_to=None):
        captured["k"] = k
        return []

    wired.setattr(rag.chunks, "search_chunks", fake_search)

    rag.lambda_handler(make_event(k="not-a-number"), None)

    assert captured["k"] == 8


def test_json_safe_return_coerces_uuid_and_date(wired):
    # C1: search_chunks returns raw psycopg rows containing uuid.UUID and
    # datetime.date values. Lambda's JSON marshaller cannot serialize these
    # (Runtime.MarshalError) — the handler MUST coerce them before returning
    # or every real (non-empty) RAG hit silently fails in prod.
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    row = {
        "id": uuid.uuid4(),
        "site_id": uuid.uuid4(),
        "topic_id": uuid.uuid4(),
        "report_date": datetime.date(2026, 2, 9),
        "chunk_text": "hello",
    }
    wired.setattr(rag.chunks, "search_chunks",
                  lambda conn, qv, site_ids, k=5, date_from=None, date_to=None: [row])

    res = rag.lambda_handler(make_event(), None)

    # This is exactly what the real Lambda JSON marshaller does to the
    # handler's return value -- it must not raise.
    json.dumps(res)

    out_row = res["chunks"][0]
    assert isinstance(out_row["id"], str)
    assert isinstance(out_row["site_id"], str)
    assert isinstance(out_row["topic_id"], str)
    assert out_row["report_date"] == "2026-02-09"


def test_date_bounds_forwarded_to_search_chunks(wired):
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    captured = {}

    def fake_search(conn, qv, site_ids, k=5, date_from=None, date_to=None):
        captured.update(date_from=date_from, date_to=date_to)
        return []

    wired.setattr(rag.chunks, "search_chunks", fake_search)
    ev = make_event()
    ev["date_from"] = "2026-02-01"
    ev["date_to"] = "2026-03-31"

    rag.lambda_handler(ev, None)

    assert captured["date_from"] == "2026-02-01"
    assert captured["date_to"] == "2026-03-31"


def test_date_bounds_default_none(wired):
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    captured = {}

    def fake_search(conn, qv, site_ids, k=5, date_from=None, date_to=None):
        captured.update(date_from=date_from, date_to=date_to)
        return []

    wired.setattr(rag.chunks, "search_chunks", fake_search)

    rag.lambda_handler(make_event(), None)  # no date keys

    assert captured["date_from"] is None
    assert captured["date_to"] is None
