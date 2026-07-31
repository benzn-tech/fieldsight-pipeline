# tests/unit/test_lambda_rag_search_normalize.py
import pytest

mod = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")


class Conn:
    pass


def test_retrieved_chunk_text_is_alias_normalized(monkeypatch):
    monkeypatch.setattr(mod, "get_cached_connection", lambda: Conn())
    monkeypatch.setattr(mod.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u-1", "company_id": "co-1",
                                           "global_role": "admin"})
    monkeypatch.setattr(mod.sites, "list_company_sites",
                        lambda conn, cid: [{"id": "s-1"}])
    monkeypatch.setattr(mod.chunks, "search_chunks",
                        lambda conn, qv, ids, k=8, date_from=None, date_to=None, author_ids=None:
                        [{"id": "c-1", "chunk_text": "Mackon poured the slab",
                          "site_id": "s-1", "topic_id": "t-1", "report_date": "2026-07-16"}])
    monkeypatch.setattr(mod.aliases, "list_active",
                        lambda conn, cid, site_ids=None: [
                            {"wrong_term": "Mackon", "right_term": "McCahon"}])

    out = mod.lambda_handler({"sub": "sub-1", "query_embedding": [0.1] * 1024}, None)
    assert out["chunks"][0]["chunk_text"] == "McCahon poured the slab"
