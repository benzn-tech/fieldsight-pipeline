"""Ask orchestrator v2 phase 3, Task 3: the judge loop and its fallbacks.

Spec: docs/superpowers/specs/2026-10-15-ask-orchestration-v2-design.md §6.1-§6.9.
Plan: docs/superpowers/plans/2026-10-16-ask-orchestration-v2-phase3.md Task 3.

`chat` is a scripted fake returning `llm_tools_client.ChatTurn`s; tools are the
companion-contract fake from test_ask_orchestrator_round1. No network.
"""
import json
import logging

import pytest

import ask_orchestrator as orch
import corroboration_client
import lambda_ask_agent as laa
import llm_utils
import voice_web
import web_answer
from llm_tools_client import ChatTurn
from tests.unit.test_ask_orchestrator_round1 import FakeTools, _block

NOW = "2026-10-15T09:00:00+13:00"           # "yesterday" is 2026-10-14 in Auckland
SUB = "sub-secret-123"


class ScriptChat:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []

    def __call__(self, messages, tools, *, tool_choice="required", deadline=None, **kw):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "tools": tools,
                           "tool_choice": tool_choice, "deadline": deadline})
        nxt = self.turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def final(answer, basis="records", offer_web=False):
    return ChatTurn(tool_calls=[{"id": "f", "name": "final_answer",
                                 "arguments": {"answer": answer, "basis": basis,
                                               "offer_web": offer_web}}])


def calls(*named):
    return ChatTurn(tool_calls=[{"id": f"c{i}", "name": n, "arguments": a}
                                for i, (n, a) in enumerate(named)])


def _rows(call):
    name = call["name"]
    if name == "search_records":
        return [{"id": "c1", "title": "Pour B2", "text": "Poured B2 slab.",
                 "date": "2026-10-14", "site": "Riverside", "chunk_type": "topic",
                 "source": "extractions/a.json"}], {}
    if name == "day_overview":
        return [{"id": "t1", "title": "Inspection", "text": "Level 2 checked.",
                 "date": call["args"].get("date_from"), "site": "Riverside"}], {}
    if name == "latest_activity":
        return [{"id": "t2", "title": "Pour", "text": "Last pour.", "date": "2026-10-10"}], {}
    return [], {"empty_reason": "no_match"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "true")
    monkeypatch.delenv("ASK_CONVERSATION_MEMORY", raising=False)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: object())

    def no_web(*a, **k):
        raise AssertionError("unexpected web call")

    def no_model(*a, **k):
        raise AssertionError("unexpected model call")
    monkeypatch.setattr(web_answer, "general_answer", no_web)
    monkeypatch.setattr(corroboration_client, "call", no_model)
    monkeypatch.setattr(llm_utils, "call_llm", no_model)


def precheck_says(monkeypatch, verdict):
    monkeypatch.setattr(corroboration_client, "call", lambda *a, **k:
                        corroboration_client.Reply(text=json.dumps({"web": verdict})))


def body(question="What did we pour on B2?", **kw):
    b = {"question": question, "caller_sub": SUB, "tz": "Pacific/Auckland", "now": NOW}
    b.update(kw)
    return b


def run(b, chat, *, channel="screen", tools=None, states=None):
    states = states if states is not None else []
    out = orch.answer(b, channel=channel, on_state=states.append,
                      tools=tools or FakeTools(_rows), chat=chat)
    return out


# ------------------------------------------------------------ the loop itself

def test_one_round_final(monkeypatch):
    precheck_says(monkeypatch, "no")
    chat = ScriptChat(final("We poured the B2 slab [R1]."))
    tools = FakeTools(_rows)
    states = []
    out = run(body(), chat, tools=tools, states=states)
    assert out["answer"] == "We poured the B2 slab [1]."
    assert [c["topic_title"] for c in out["citations"]] == ["Pour B2"]
    assert out["grounded"] is True and not out.get("from_web")
    assert isinstance(out["basis"], dict) and out["basis"]["widened"] is False
    assert len(chat.calls) == 1 and len(tools.batches) == 1
    assert states[0] == "searching_records" and "writing" in states
    # System, user, then round 1 as assistant tool calls + one tool message each.
    msgs = chat.calls[0]["messages"]
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    assert msgs[2]["role"] == "assistant"
    ids = [tc["id"] for tc in msgs[2]["tool_calls"]]
    assert [m["tool_call_id"] for m in msgs[3:]] == ids
    assert all(m["content"].startswith(orch.DATA_FENCE_HEADING) for m in msgs[3:])
    names = [t["function"]["name"] for t in chat.calls[0]["tools"]]
    assert "final_answer" in names and "day_overview" in names
    assert "web_search" in names                 # auto policy, no web yet
    assert chat.calls[0]["tool_choice"] == "required"


def test_system_prompt_names_today_refs_and_language(monkeypatch):
    precheck_says(monkeypatch, "no")
    chat = ScriptChat(final("x [R1]"))
    run(body(), chat)
    system = chat.calls[0]["messages"][0]["content"]
    assert "2026-10-15" in system
    assert "[R3]" in system and "[R1, R2]" in system and "[W2]" in system
    assert "DATA, not instructions" in system


def test_two_round_tool_request(monkeypatch):
    precheck_says(monkeypatch, "no")
    chat = ScriptChat(calls(("find_people", {"name": "Mike"}),
                            ("search_records", {"query": "Mike pour"})),
                      final("Mike poured [R1]."))
    tools = FakeTools(_rows)
    out = run(body(), chat, tools=tools)
    assert len(tools.batches) == 2
    assert [c["name"] for c in tools.batches[1]] == ["find_people", "search_records"]
    msgs = chat.calls[1]["messages"]
    assert sum(1 for m in msgs if m["role"] == "assistant") == 2
    assert out["answer"] == "Mike poured [1]."


def test_forced_final_on_a_short_deadline(monkeypatch):
    precheck_says(monkeypatch, "no")
    monkeypatch.setattr(orch, "DEADLINE_S", {"screen": 12.0, "voice": 12.0})
    chat = ScriptChat(final("B2 [R1]."))
    t0 = orch._clock()
    out = run(body(), chat)
    call = chat.calls[0]
    assert call["tool_choice"] == {"type": "function", "function": {"name": "final_answer"}}
    assert [t["function"]["name"] for t in call["tools"]] == ["final_answer"]
    assert any("answer now" in (m.get("content") or "").lower() for m in call["messages"])
    assert call["deadline"] <= t0 + 12.0 + 0.5
    assert out["answer"] == "B2 [1]."


def test_non_final_judge_calls_keep_the_answer_reserve(monkeypatch):
    precheck_says(monkeypatch, "no")
    chat = ScriptChat(final("x [R1]"))
    t0 = orch._clock()
    run(body(), chat)
    assert chat.calls[0]["deadline"] <= t0 + orch.DEADLINE_S["screen"] - orch.FINAL_RESERVE_S + 0.5


def test_four_call_cap(monkeypatch):
    precheck_says(monkeypatch, "no")
    six = [("search_records", {"query": f"q{i}"}) for i in range(6)]
    chat = ScriptChat(calls(*six), final("ok [R1]"))
    tools = FakeTools(_rows)
    run(body(), chat, tools=tools)
    assert len(tools.batches[1]) == 4
    tool_msgs = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"]
    assert sum("dropped" in m["content"] for m in tool_msgs) == 2


def test_bad_args_twice_forces_final(monkeypatch):
    precheck_says(monkeypatch, "no")
    bad = ChatTurn(tool_calls=[{"id": "b", "name": "search_records", "arguments": {}}],
                   bad_args=["b"])
    chat = ScriptChat(bad, bad, final("done [R1]"))
    out = run(body(), chat)
    assert chat.calls[2]["tool_choice"] == {"type": "function",
                                            "function": {"name": "final_answer"}}
    assert out["answer"] == "done [1]"


def test_bad_args_round_is_fed_back_as_an_error(monkeypatch):
    precheck_says(monkeypatch, "no")
    bad = ChatTurn(tool_calls=[{"id": "b", "name": "search_records", "arguments": {}}],
                   bad_args=["b"])
    chat = ScriptChat(bad, final("x [R1]"))
    tools = FakeTools(_rows)
    run(body(), chat, tools=tools)
    assert len(tools.batches) == 1              # nothing executed for a bad call
    assert any("error: bad_args" in (m.get("content") or "")
               for m in chat.calls[1]["messages"] if m["role"] == "tool")


def test_text_only_reply_is_the_final_answer(monkeypatch):
    precheck_says(monkeypatch, "no")
    chat = ScriptChat(ChatTurn(text="Poured B2 [R1]."))
    out = run(body(), chat)
    assert out["answer"] == "Poured B2 [1]." and len(chat.calls) == 1


def test_judge_web_search_runs_once_with_the_original_question(monkeypatch):
    precheck_says(monkeypatch, "no")
    seen = []

    def fake(question, budget, **kw):
        seen.append(question)
        return _block(answer="40 mm cover.", n_sources=1)
    monkeypatch.setattr(web_answer, "general_answer", fake)
    chat = ScriptChat(calls(("web_search", {})),
                      final("Cover is 40 mm [W1] [W2].", basis="web"))
    states = []
    out = run(body("What concrete cover for B2?"), chat, states=states)
    assert seen == ["What concrete cover for B2?"]
    assert "searching_web" in states
    assert "web_search" not in [t["function"]["name"] for t in chat.calls[1]["tools"]]
    assert out["from_web"] is True
    assert [s["url"] for s in out["web"]["sources"]] == ["https://s0.nz/p"]


# ---------------------------------------------------------- guard + fallback

def test_guard_fires_on_yesterday_with_basis_web(monkeypatch, caplog):
    seen = {}

    def fake_llm(prompt, **kw):
        seen["prompt"] = prompt
        seen["kw"] = kw
        return "Records answer [1].", None
    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)
    chat = ScriptChat(final("Generally you pour concrete [W1].", basis="web"))
    with caplog.at_level(logging.INFO):
        out = run(body("What did we do yesterday?"), chat)
    assert out["answer"] == "Records answer [1]."
    assert out.get("from_web") is not True and "web" not in out
    assert out["citations"] and out["citations"][0]["topic_title"]
    assert "DATA, not instructions" in seen["prompt"]
    assert seen["kw"]["caller"] == "ask_answer"
    assert 0 < seen["kw"]["deadline"] <= orch.DEADLINE_S["screen"]   # seconds left
    route = _route(caplog)
    assert route["guard"] is True and route["fallback"] == "guard"
    assert route["policy"] == "never"


def test_judge_timeout_falls_back_to_records_synthesis(monkeypatch):
    precheck_says(monkeypatch, "no")
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: ("Synth [1].", None))
    chat = ScriptChat(ChatTurn(error="deadline", timed_out=True))
    out = run(body(), chat)
    assert out["answer"] == "Synth [1]."
    assert out["citations"][0]["topic_title"] == "Pour B2"


def test_judge_raising_falls_back_too(monkeypatch):
    precheck_says(monkeypatch, "no")
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: ("Synth [1].", None))
    out = run(body(), ScriptChat(RuntimeError("boom")))
    assert out["answer"] == "Synth [1]."


def test_judge_error_without_records_says_so_with_latest_before(monkeypatch):
    def rows(call):
        if call["name"] == "day_overview" and call["args"]["date_from"] == "2026-10-14":
            return [], {"empty_reason": "nothing_in_range",
                        "latest_before": {"date": "2026-10-12", "from": "topics"}}
        return [], {"empty_reason": "nothing_in_range"}
    chat = ScriptChat(ChatTurn(error="HTTP 500"))
    out = run(body("What did we do yesterday?"), chat, tools=FakeTools(rows))
    assert out["answer"].startswith("No relevant records found for this question.")
    assert "2026-10-12" in out["answer"]
    assert out["citations"] == []


def test_not_provisioned_gives_the_identity_answer_and_never_the_web(monkeypatch):
    def rows(call):
        return [], {"error": "not_provisioned"}
    chat = ScriptChat()
    out = run(body("What does NZS 3101 say? search online"), chat, tools=FakeTools(rows))
    assert out["answer"] == "No relevant records found for this question."
    assert out["citations"] == [] and out["grounded"] is True
    assert "web" not in out and not out.get("from_web")
    assert chat.calls == []


def test_round1b_answer_reports_the_widened_day(monkeypatch):
    def rows(call):
        a = call["args"]
        if call["name"] == "day_overview" and a["date_from"] == "2026-10-14":
            return [], {"empty_reason": "nothing_in_range",
                        "latest_before": {"date": "2026-10-12", "from": "topics"}}
        if call["name"] == "day_overview":
            return [{"id": "t9", "title": "Inspection", "date": "2026-10-12"}], {}
        return [], {"empty_reason": "nothing_in_range"}
    chat = ScriptChat(final("Nothing on the 14th; on the 12th, inspection [R1]."))
    out = run(body("What did we do yesterday?"), chat, tools=FakeTools(rows))
    assert out["basis"]["widened"] is True
    assert out["basis"]["from"] == out["basis"]["to"] == "2026-10-12"
    assert out["basis"]["dates"] == ["2026-10-12"]


# ------------------------------------------------------------------- voice

def voice_body(question="What did we pour on B2?", **kw):
    return body(question, mode="voice", voice_web=True, offer_web=True, **kw)


def test_voice_offer_appends_when_records_answer(monkeypatch):
    chat = ScriptChat(final("We poured B2 [R1].", basis="records", offer_web=True))
    out = run(voice_body(), chat, channel="voice")
    assert out["answer"] == "We poured B2. " + voice_web.OFFER_ALSO
    assert out["web_offer"] is True and out["citations"] == []


def test_voice_offer_replaces_when_basis_none(monkeypatch):
    chat = ScriptChat(final("Nothing recorded.", basis="none", offer_web=True))
    out = run(voice_body("What is the best rebar brand?"), chat, channel="voice")
    assert out["answer"] == voice_web.OFFER_NO_RECORDS and out["web_offer"] is True


def test_offer_web_is_ignored_on_the_screen_and_without_the_offer_flag(monkeypatch):
    precheck_says(monkeypatch, "no")
    out = run(body(), ScriptChat(final("B2 [R1].", offer_web=True)))
    assert "web_offer" not in out
    b = voice_body()
    b.pop("offer_web")
    out = run(b, ScriptChat(final("B2 [R1].", offer_web=True)), channel="voice")
    assert "web_offer" not in out


def test_voice_strips_refs_and_offers_no_web_tool(monkeypatch):
    chat = ScriptChat(final("We poured B2 [R1] on the 14th [R9]."))
    out = run(voice_body(), chat, channel="voice")
    assert "[" not in out["answer"] and out["citations"] == []
    assert "web_search" not in [t["function"]["name"] for t in chat.calls[0]["tools"]]


def test_voice_web_answer_goes_through_speakable(monkeypatch):
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda q, b, **kw: _block(answer="Use 40 mm.", n_sources=1))
    seen = {}

    def speak(answer, records_answer):
        seen["in"] = answer
        return "Spoken."
    monkeypatch.setattr(laa, "_speakable_web_answer", speak)
    chat = ScriptChat(final("Use 40 mm [W1].", basis="web"))
    out = run(voice_body("How much cover for a slab?", web_intent=True), chat,
              channel="voice")
    assert seen["in"] == "Use 40 mm." and out["answer"] == "Spoken."
    assert out["from_web"] is True


# ------------------------------------------------------------- pre-pass reuse

def test_metric_fast_path_is_untouched(monkeypatch):
    import metric_slots
    monkeypatch.setattr(metric_slots, "detect", lambda q: "duration")
    sentinel = {"answer": "You recorded 2 h.", "citations": [], "computed": True}
    seen = {}

    def fake_metric(caller_sub, question, metric, date_from, date_to, applied_scope=None):
        seen.update(metric=metric, date_from=date_from, date_to=date_to)
        return dict(sentinel)
    monkeypatch.setattr(laa, "_metric_answer", fake_metric)
    tools = FakeTools(_rows)
    chat = ScriptChat()
    out = run(body("How long did I record yesterday?"), chat, tools=tools)
    assert out == {**sentinel, "asked": None}
    assert seen == {"metric": "duration", "date_from": "2026-10-14", "date_to": "2026-10-14"}
    assert tools.batches == [] and chat.calls == []


def test_topic_or_author_scope_is_handed_to_the_legacy_path(monkeypatch):
    seen = []

    def legacy(b, on_web_start=None):
        seen.append(b)
        return {"answer": "legacy", "citations": []}
    monkeypatch.setattr(laa, "_rag_answer", legacy)
    tools = FakeTools(_rows)
    for extra in ({"topic_row_id": "0f8f5c55-1111-4222-8333-944444444444"},
                  {"author_folder": "Ben_Lin"}):
        out = run(body(**extra), ScriptChat(), tools=tools)
        assert out == {"answer": "legacy", "citations": []}
    assert len(seen) == 2 and tools.batches == []


def test_site_scope_reaches_every_tool_call(monkeypatch):
    site = "0f8f5c55-1111-4222-8333-944444444444"
    tools = FakeTools(_rows)
    chat = ScriptChat(final("x [R1]"))
    out = run(body(site_id=site), chat, tools=tools)
    assert all(c["args"]["site"] == site for c in tools.batches[0])
    assert out["applied_scope"]["site_id"] == site
    assert "web_search" not in [t["function"]["name"] for t in chat.calls[0]["tools"]]


def test_rewritten_question_is_asked_and_the_web_gets_the_original(monkeypatch):
    import ask_rewrite
    monkeypatch.setenv("ASK_CONVERSATION_MEMORY", "true")
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("What cover does the B2 slab need?", True))
    seen = []
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda q, b, **kw: seen.append(q) or _block())
    tools = FakeTools(_rows)
    chat = ScriptChat(final("x [R1]"))
    out = run(body("and the cover? search online",
                   history=[{"question": "what about B2", "answer": "poured"}]), chat, tools=tools)
    assert tools.batches[0][0]["args"]["query"] == "What cover does the B2 slab need?"
    assert seen == ["and the cover? search online"]
    assert out["asked"] == "What cover does the B2 slab need?"
    user = chat.calls[0]["messages"][1]["content"]
    assert "What cover does the B2 slab need?" in user and "and the cover?" in user


# ------------------------------------------------------------------ logging

def _route(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ASK_ROUTE ")]
    assert len(lines) == 1
    return json.loads(lines[0][len("ASK_ROUTE "):])


def test_ask_route_fields_and_no_text(monkeypatch, caplog):
    precheck_says(monkeypatch, "no")
    chat = ScriptChat(calls(("search_records", {"query": "secret follow up"})),
                      final("Hidden answer words [R1] [R77]."))
    with caplog.at_level(logging.INFO):
        run(body("Unique question words here?"), chat)
    route = _route(caplog)
    for key in ("orch", "channel", "policy", "precheck", "rounds", "calls", "refs_r",
                "refs_w", "bad_refs", "basis", "web_late", "fallback", "ms", "sub_hash"):
        assert key in route, key
    assert route["orch"] == "v2" and route["channel"] == "screen"
    assert route["policy"] == "auto" and route["precheck"] == "no"
    assert route["rounds"] == 2
    assert route["calls"] == [["search_records"], ["search_records"]]
    assert route["refs_r"] == 1 and route["bad_refs"] == 1 and route["basis"] == "records"
    assert len(route["ms"]["judge"]) == 2
    text = caplog.text
    for secret in ("Unique question words", "Hidden answer words", "secret follow up",
                   SUB, "Poured B2 slab"):
        assert secret not in text


def test_precheck_is_na_when_no_precheck_ran(monkeypatch, caplog):
    monkeypatch.setattr(web_answer, "general_answer", lambda q, b, **kw: _block())
    with caplog.at_level(logging.INFO):
        run(body("What does NZS 3101 say? search online"), ScriptChat(final("x [W1]",
                                                                            basis="web")))
    route = _route(caplog)
    assert route["policy"] == "required" and route["precheck"] == "n/a"


def test_on_state_failure_never_breaks_the_turn(monkeypatch):
    precheck_says(monkeypatch, "no")

    def bad_state(s):
        raise RuntimeError("s3 down")
    out = orch.answer(body(), channel="screen", on_state=bad_state,
                      tools=FakeTools(_rows), chat=ScriptChat(final("x [R1]")))
    assert out["answer"] == "x [1]"
