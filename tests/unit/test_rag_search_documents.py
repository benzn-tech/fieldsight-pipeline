"""rag-search `documents` mode: same ACL primitive as every other mode (scope.visible_scope),
site narrowing only inside it, nothing at all when the flag is off."""
import uuid

import pytest

rag = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")

S1, S2 = "51000000-0000-0000-0000-000000000001", "51000000-0000-0000-0000-000000000002"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PROJECT_DOCUMENTS", "true")
    monkeypatch.setattr(rag, "get_cached_connection", lambda: "conn")
    monkeypatch.setattr(rag, "close_cached_connection", lambda: None)
    monkeypatch.setattr(rag.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u1", "company_id": "c1"} if sub == "s" else None)
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {S1, S2}, "author_ids": {"u1"}})
    monkeypatch.setattr(rag.sites, "get_company_site_by_slug",
                        lambda conn, company_id, slug: {"id": S2} if (company_id, slug) == (
                            "c1", "tower-b") else None)
    calls = []

    def search(conn, site_ids, qv, q_text, k=8):
        calls.append({"site_ids": sorted(site_ids), "q_text": q_text, "k": k})
        return [{"id": uuid.UUID(int=1), "document_id": uuid.UUID(int=2), "title": "Spec",
                 "chunk_text": "Cover 40 mm", "distance": 0.2, "page_from": 3}]
    monkeypatch.setattr(rag.project_docs, "search", search)
    return calls


def _ev(**kw):
    ev = {"mode": "documents", "sub": "s", "query_embedding": [0.1] * 4,
          "question": "what is the cover for the slab", "k": 5}
    ev.update(kw)
    return ev


def test_documents_are_searched_over_the_callers_whole_reach_with_no_author_filter(env):
    out = rag.lambda_handler(_ev(), None)
    assert out["site_count"] == 2 and env[0]["site_ids"] == sorted([S1, S2])
    assert env[0]["k"] == 5 and "cover" in env[0]["q_text"]
    row = out["documents"][0]
    assert row["document_id"] == str(uuid.UUID(int=2)) and row["page_from"] == 3


def test_a_site_narrows_inside_the_reach_and_a_foreign_site_yields_nothing(env):
    assert rag.lambda_handler(_ev(site=S1), None)["site_count"] == 1
    assert env[-1]["site_ids"] == [S1]
    out = rag.lambda_handler(_ev(site="59000000-0000-0000-0000-000000000009"), None)
    assert out == {"documents": [], "site_count": 0}


def test_the_site_pin_resolves_like_records_mode_any_case_uuid_or_slug(env):
    """Same resolver as records mode (_pin_site): an upper-case UUID is the same project,
    and the legacy slug still pins -- never outside the reach."""
    assert rag.lambda_handler(_ev(site=S1.upper()), None)["site_count"] == 1
    assert env[-1]["site_ids"] == [S1]
    assert rag.lambda_handler(_ev(site="tower-b"), None)["site_count"] == 1
    assert env[-1]["site_ids"] == [S2]
    out = rag.lambda_handler(_ev(site="59000000-0000-0000-0000-00000000000A"), None)
    assert out == {"documents": [], "site_count": 0}
    out = rag.lambda_handler(_ev(site="no-such-slug"), None)
    assert out == {"documents": [], "site_count": 0}


def test_off_means_no_query_at_all(env, monkeypatch):
    monkeypatch.setenv("PROJECT_DOCUMENTS", "false")
    assert rag.lambda_handler(_ev(), None) == {"documents": [], "off": True}
    assert env == []


def test_an_unknown_caller_or_missing_embedding_is_an_error_not_a_crash(env):
    assert rag.lambda_handler(_ev(sub="nobody"), None)["error"] == "caller not provisioned"
    assert "error" in rag.lambda_handler(_ev(query_embedding=None), None)
    assert env == []


def test_k_is_clamped(env):
    rag.lambda_handler(_ev(k=500), None)
    assert env[-1]["k"] == 20
