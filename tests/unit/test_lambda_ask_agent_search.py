"""Tests for lambda_ask_agent mode=search (retrieve-only topic list).
Mirrors test_lambda_ask_agent_rag.py's wiring (FakeLambdaClient stand-in for
rag-search; dashscope_utils.embed / claude_utils.call_claude monkeypatched as
shared-module attributes)."""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3")
import claude_utils  # noqa: E402
import dashscope_utils  # noqa: E402


def chunk(topic_id, date, dist, title, folder="Jarley_Trainor", site="Ellesmere",
          chunk_type="topic", text="door hardware defect", src=None):
    # ingest stamps every chunk's source_s3_key with the report key
    # (reports/<date>/<folder>/daily_report.json); the search route parses the
    # folder out of it (seed-independent). folder=None => no folder segment.
    if src is None:
        src = ("reports/" + date + "/" + folder + "/daily_report.json") if folder \
            else "reports/" + date + "/daily_report.json"
    return {"id": "c-" + str(dist), "chunk_text": text, "chunk_type": chunk_type,
            "topic_id": topic_id, "source_s3_key": src, "metadata": {},
            "report_date": date, "site_id": "s-1", "site_name": site,
            "topic_title": title, "topic_summary": "", "distance": dist}


class FakeLambdaClient:
    def __init__(self, chunks):
        self.payload = {"chunks": chunks}
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({"FunctionName": FunctionName, "Payload": json.loads(Payload)})
        return {"Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8"))}


def wire(monkeypatch, chunks):
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    fc = FakeLambdaClient(chunks)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: fc)

    def no_claude(*a, **k):
        raise AssertionError("call_claude must not run in search mode")
    monkeypatch.setattr(claude_utils, "call_claude", no_claude)
    return fc


def run(event):
    resp = laa.lambda_handler(event, None)
    return json.loads(resp["body"])


def ev(**kw):
    base = {"question": "door damage", "caller_sub": "sub-1", "mode": "search"}
    base.update(kw)
    return base


def test_search_mode_returns_topics_no_claude(monkeypatch):
    wire(monkeypatch, [chunk("t-1", "2026-02-09", 0.1, "Door Hardware Issues")])
    out = run(ev())
    assert out["count"] == 1
    r = out["results"][0]
    assert r["title"] == "Door Hardware Issues"
    assert r["report_date"] == "2026-02-09"
    assert r["route"] == "/timeline?date=2026-02-09&user=Jarley_Trainor&topic=t-1"


def test_search_mode_dedupes_topic_keeps_best_distance(monkeypatch):
    wire(monkeypatch, [
        chunk("t-1", "2026-02-09", 0.4, "Door Hardware Issues"),
        chunk("t-1", "2026-02-09", 0.1, "Door Hardware Issues"),  # same topic, closer
    ])
    out = run(ev())
    assert out["count"] == 1
    assert out["results"][0]["score"] == 0.1


def test_search_mode_orders_by_distance(monkeypatch):
    wire(monkeypatch, [
        chunk("t-2", "2026-03-02", 0.5, "Far"),
        chunk("t-1", "2026-02-09", 0.1, "Near"),
    ])
    out = run(ev())
    assert [r["title"] for r in out["results"]] == ["Near", "Far"]


def test_search_mode_topicless_chunk_grouped_no_topic_in_route(monkeypatch):
    wire(monkeypatch, [chunk(None, "2026-02-09", 0.2, "", chunk_type="transcript_window",
                             text="sliding door came off runner")])
    out = run(ev())
    r = out["results"][0]
    assert r["topic_id"] is None
    assert "&topic=" not in r["route"]
    assert r["route"].startswith("/timeline?date=2026-02-09&user=Jarley_Trainor")
    assert r["title"] == "sliding door came off runner"[:60]


def test_search_mode_route_omits_user_when_folder_missing(monkeypatch):
    wire(monkeypatch, [chunk("t-9", "2026-02-09", 0.2, "T", folder=None)])
    out = run(ev())
    assert out["results"][0]["route"] == "/timeline?date=2026-02-09&topic=t-9"


def test_search_mode_forwards_date_range_and_k(monkeypatch):
    fc = wire(monkeypatch, [])
    run(ev(date_from="2026-02-01", date_to="2026-03-31", k=25))
    p = fc.calls[0]["Payload"]
    assert p["date_from"] == "2026-02-01"
    assert p["date_to"] == "2026-03-31"
    assert p["k"] == 25


def test_search_mode_default_k_is_30(monkeypatch):
    fc = wire(monkeypatch, [])
    run(ev())
    assert fc.calls[0]["Payload"]["k"] == 30


def test_ask_mode_unaffected_still_calls_claude(monkeypatch):
    # mode absent => Ask path; call_claude IS used (no AssertionError wiring here)
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    fc = FakeLambdaClient([chunk("t-1", "2026-02-09", 0.1, "Door")])
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: fc)
    seen = {}
    monkeypatch.setattr(claude_utils, "call_claude",
                        lambda prompt, max_tokens=4096: (seen.setdefault("called", True), "ans [1]")[1] if False else ("ans [1]", None))
    resp = laa.lambda_handler({"question": "q", "caller_sub": "sub-1"}, None)
    body = json.loads(resp["body"])
    assert "citations" in body  # Ask envelope, not the search {results} envelope
