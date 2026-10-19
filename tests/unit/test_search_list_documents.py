"""R7: the palette's documents group rides the existing /api/search -> ask-agent
mode=search path; ask-agent makes a second rag-search call in `documents` mode and returns
it beside the records, and the proxy forwards the opt-in and passes the answer through."""
import io
import json

import pytest

laa = pytest.importorskip("lambda_ask_agent")
fa = pytest.importorskip("lambda_fieldsight_api")


class FakeLambda:
    def __init__(self, documents=None, fail_documents=False, off=False, raw_documents=None):
        self.events, self.documents = [], documents or []
        self.fail_documents, self.off = fail_documents, off
        self.raw_documents = raw_documents          # a documents-mode body sent as is

    def invoke(self, FunctionName, InvocationType, Payload):
        ev = json.loads(Payload)
        self.events.append(ev)
        if ev.get("mode") == "documents":
            if self.fail_documents:
                return {"FunctionError": "Unhandled", "Payload": io.BytesIO(b"{}")}
            if self.raw_documents is not None:
                return {"Payload": io.BytesIO(json.dumps(self.raw_documents).encode())}
            body = {"documents": [], "off": True} if self.off else {"documents": self.documents}
        else:
            body = {"chunks": [], "site_count": 1}
        return {"Payload": io.BytesIO(json.dumps(body).encode())}


ROW = {"id": "c1", "document_id": "d1", "series_id": "s1", "revision": 2, "site_id": "p1",
       "site_name": "Tower", "title": "Structural spec", "heading": "3.2 Formwork",
       "page_from": 4, "page_to": 5, "content_type": "application/pdf",
       "chunk_text": "x" * 500, "lexical_hit": True, "distance": 0.4}


@pytest.fixture
def env(monkeypatch):
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts: [[0.1] * 4 for _ in texts])
    monkeypatch.setenv("PROJECT_DOCUMENTS", "true")

    def use(fake):
        monkeypatch.setattr(laa, "_get_lambda_client", lambda: fake)
        return fake
    return use


def _body(**kw):
    b = {"question": "formwork tolerance", "caller_sub": "sub-1", "include_documents": True}
    b.update(kw)
    return b


def test_documents_come_back_beside_the_records(env):
    fake = env(FakeLambda(documents=[ROW]))
    out = laa._rag_search_list(_body(site="p1"))
    assert [e.get("mode") for e in fake.events] == [None, "documents"]
    assert fake.events[1]["site"] == "p1" and fake.events[1]["question"] == "formwork tolerance"
    assert out["documents"] == [{
        "document_id": "d1", "series_id": "s1", "revision": 2, "site_id": "p1",
        "site_name": "Tower", "title": "Structural spec", "heading": "3.2 Formwork",
        "page_from": 4, "page_to": 5, "content_type": "application/pdf",
        "snippet": "x" * 240, "lexical": True}]
    assert out["results"] == []


def test_no_opt_in_no_second_call(env):
    fake = env(FakeLambda(documents=[ROW]))
    out = laa._rag_search_list(_body(include_documents=False))
    assert len(fake.events) == 1 and "documents" not in out


def test_flag_off_no_documents_key(env, monkeypatch):
    monkeypatch.setenv("PROJECT_DOCUMENTS", "false")
    fake = env(FakeLambda(documents=[ROW]))
    out = laa._rag_search_list(_body())
    assert len(fake.events) == 1 and "documents" not in out


def test_rag_search_reporting_off_means_no_documents_key(env):
    env(FakeLambda(off=True))
    assert "documents" not in laa._rag_search_list(_body())


def test_a_failed_documents_call_never_costs_the_records(env):
    env(FakeLambda(fail_documents=True))
    out = laa._rag_search_list(_body())
    assert out["documents"] == [] and "error" not in out


@pytest.mark.parametrize("raw", [["not", "a", "dict"], "a string", 7, {"documents": ["x"]}])
def test_a_malformed_documents_payload_never_costs_the_records(env, raw):
    """Shaping sits inside the try too: a non-dict body or a non-dict hit is [] documents,
    and the records result comes back as it would have."""
    env(FakeLambda(raw_documents=raw))
    out = laa._rag_search_list(_body())
    assert out["documents"] == [] and out["results"] == [] and "error" not in out


def test_the_proxy_forwards_only_an_explicit_true_and_passes_documents_through(monkeypatch):
    sent = []

    class _L:
        def invoke(self, FunctionName, InvocationType, Payload):
            sent.append(json.loads(Payload))
            return {"Payload": io.BytesIO(json.dumps(
                {"results": [], "count": 0, "documents": [{"title": "Spec"}]}).encode())}
    monkeypatch.setattr(fa, "lambda_client", _L())
    monkeypatch.setattr(fa, "resolve_user_display_name", lambda caller: "x")
    resp = fa.search_topics({"question": "formwork", "include_documents": True}, {"sub": "s"})
    assert sent[-1]["include_documents"] is True
    assert json.loads(resp["body"])["documents"] == [{"title": "Spec"}]
    fa.search_topics({"question": "formwork", "include_documents": "yes"}, {"sub": "s"})
    assert "include_documents" not in sent[-1]


def test_the_opt_in_survives_the_real_ask_agent_handler(env, monkeypatch):
    """Through lambda_handler's mode=search dispatch, not just the helper: the opt-in in
    the body reaches the second call and `documents` comes back in the response body."""
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")
    fake = env(FakeLambda(documents=[ROW]))
    resp = laa.lambda_handler({"mode": "search", **_body()}, None)
    out = json.loads(resp["body"])
    assert [e.get("mode") for e in fake.events] == [None, "documents"]
    assert out["documents"][0]["document_id"] == "d1"


def test_a_content_free_browse_makes_no_documents_call(env, monkeypatch):
    import query_slots
    import search_browse
    monkeypatch.setattr(query_slots, "time_range", lambda q, today: ("2026-10-15", "2026-10-15"))
    monkeypatch.setattr(search_browse, "is_content_free", lambda q, *today: True)
    fake = env(FakeLambda(documents=[ROW]))
    out = laa._rag_search_list(_body(question="what happened yesterday"))
    assert len(fake.events) == 1 and "documents" not in out and out.get("browse") is True
