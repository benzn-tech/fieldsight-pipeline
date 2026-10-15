"""Ask orchestrator v2 against the REAL ask_tools (#1128), not the plan-shaped
fake. Only the network edges are faked: the rag-search Lambda (its `mode:
"tools"` response shape), the DashScope embed, the judge `chat`. Everything
between -- validation, refs, envelopes, render, refs -> citations -- is real.
"""
import io
import json

import pytest

import ask_orchestrator as orch
import ask_tools
import corroboration_client
import dashscope_utils
import lambda_ask_agent as laa
import llm_utils
import web_answer
from llm_tools_client import ChatTurn

NOW = "2026-10-15T09:00:00+13:00"           # "yesterday" = 2026-10-14 in Auckland
SUB = "sub-contract-1"

CHUNK_ROW = {"id": "ch-1", "chunk_text": "Poured the B2 slab, 32 MPa.",
             "chunk_type": "topic", "topic_id": "tp-1", "topic_title": "Pour B2",
             "report_date": "2026-10-12", "site_name": "Riverside",
             "site_slug": "riverside", "source_s3_key": "extractions/a.json",
             "metadata": {}, "distance": 0.2}
TOPIC_ROW = {"id": "tp-2", "title": "Level 2 inspection", "summary": "Checked L2 formwork.",
             "report_date": "2026-10-12", "site_name": "Riverside", "user_name": "Ben Lin",
             "n_tasks": 1, "n_findings": 0}


class RagSearch:
    """rag-search `mode: "tools"`: {"results": [{"id", "rows", "meta"}]}."""

    def __init__(self, by_name):
        self.by_name, self.events = by_name, []

    def invoke(self, FunctionName, InvocationType, Payload):
        ev = json.loads(Payload)
        self.events.append(ev)
        assert ev["mode"] == "tools" and ev["sub"] == SUB
        out = []
        for c in ev["calls"]:
            rows, meta = self.by_name(c)
            out.append({"id": c["id"], "rows": rows, "meta": meta})
        return {"Payload": io.BytesIO(json.dumps({"results": out}).encode())}


class Chat:
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def __call__(self, messages, tools, *, tool_choice="required", deadline=None, **kw):
        self.seen.append({"messages": json.loads(json.dumps(messages)), "tools": tools})
        return self.turns.pop(0)


def final(answer, basis="records"):
    return ChatTurn(tool_calls=[{"id": "f", "name": "final_answer",
                                 "arguments": {"answer": answer, "basis": basis}}])


@pytest.fixture(autouse=True)
def edges(monkeypatch):
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "true")
    monkeypatch.setenv("ENABLE_RERANK", "false")
    monkeypatch.delenv("ASK_CONVERSATION_MEMORY", raising=False)
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, **k: [[0.1] * 1024 for _ in texts])

    def boom(*a, **k):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(web_answer, "general_answer", boom)
    monkeypatch.setattr(llm_utils, "call_llm", boom)
    monkeypatch.setattr(corroboration_client, "call", lambda *a, **k:
                        corroboration_client.Reply(text=json.dumps({"web": "no"})))


def _body(question):
    return {"question": question, "caller_sub": SUB, "tz": "Pacific/Auckland", "now": NOW}


def test_yesterday_empty_day_round1b_and_citation_cards(monkeypatch):
    def by_name(call):
        if call["name"] == "day_overview" and call["args"]["date_from"] == "2026-10-14":
            return [], {"empty_reason": "nothing_in_range",
                        "latest_before": {"date": "2026-10-12", "from": "topics"}}
        if call["name"] == "day_overview":
            return [TOPIC_ROW], {}
        if call["name"] == "search_records":
            return [], {"empty_reason": "nothing_in_range",
                        "latest_before": {"date": "2026-10-12", "from": "chunks"}}
        raise AssertionError(call["name"])
    rag = RagSearch(by_name)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: rag)
    chat = Chat(final("Nothing on the 14th; on the 12th Ben checked L2 formwork [R1]."))

    out = orch.answer(_body("What did we do yesterday?"), channel="screen",
                      on_state=lambda s: None, tools=ask_tools, chat=chat)

    # Round 1 + round 1b reached the real dispatcher with real validated args.
    sent = [c for ev in rag.events for c in ev["calls"]]
    assert [c["name"] for c in sent] == ["day_overview", "search_records", "day_overview"]
    assert sent[2]["args"]["date_from"] == "2026-10-12"
    assert "query_embedding" in sent[1]                  # search_records was embedded
    # The judge saw the real render, with the empty day's latest_before.
    rendered = " ".join(m.get("content") or "" for m in chat.seen[0]["messages"]
                        if m["role"] == "tool")
    assert "latest_before 2026-10-12" in rendered and "R1 |" in rendered
    # R1 -> [1] and a card with the keys _build_citations emits.
    assert out["answer"].endswith("[1].")
    assert out["grounded"] is True and not out.get("from_web")
    card = out["citations"][0]
    assert set(card) == {"source_s3_key", "report_date", "site_name", "site_slug",
                         "topic_title", "chunk_type", "snippet", "time_start"}
    assert card["topic_title"] == "Level 2 inspection" and card["report_date"] == "2026-10-12"


def test_judge_round_two_uses_real_tools_and_real_refs(monkeypatch):
    def by_name(call):
        if call["name"] == "search_records":
            return [CHUNK_ROW], {}
        if call["name"] == "latest_activity":
            assert "date_from" not in call["args"]        # contract note 2
            return [dict(CHUNK_ROW, id="ch-2", report_date="2026-10-10",
                         chunk_text="Earlier pour, 25 MPa.")], {"matched_date": "2026-10-10"}
        raise AssertionError(call["name"])
    rag = RagSearch(by_name)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: rag)
    chat = Chat(ChatTurn(tool_calls=[{"id": "c0", "name": "latest_activity",
                                      "arguments": {"query": "pour"}}]),
                final("B2 was poured at 32 MPa [R1]; the one before at 25 MPa [R2]."))

    out = orch.answer(_body("What concrete did we pour on B2?"), channel="screen",
                      on_state=lambda s: None, tools=ask_tools, chat=chat)

    assert [c["snippet"][:12] for c in out["citations"]] == ["Poured the B", "Earlier pour"]
    assert out["answer"] == "B2 was poured at 32 MPa [1]; the one before at 25 MPa [2]."
    # The offered tool list is the real ask_tools.TOOLS plus the orchestrator's own.
    names = {t["function"]["name"] for t in chat.seen[0]["tools"]}
    assert {t["name"] for t in ask_tools.TOOLS} <= names and "final_answer" in names


def test_id_less_rows_use_fresh_refs_through_the_lock(monkeypatch):
    rows = [dict(TOPIC_ROW, id=None, title="A"), dict(TOPIC_ROW, id=None, title="B")]
    rag = RagSearch(lambda call: (rows, {}) if call["name"] == "day_overview"
                    else ([], {"empty_reason": "no_match"}))
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: rag)
    chat = Chat(final("A [R1] and B [R2]."))
    out = orch.answer(_body("What did we do yesterday?"), channel="screen",
                      on_state=lambda s: None, tools=ask_tools, chat=chat)
    assert [c["topic_title"] for c in out["citations"]] == ["A", "B"]
