"""Ask orchestrator v2 phase 3, Task 2: round-1 bundle, pre-check, web tool.

Spec: docs/superpowers/specs/2026-10-15-ask-orchestration-v2-design.md §6.2, §6.3, §7.
Plan: docs/superpowers/plans/2026-10-16-ask-orchestration-v2-phase3.md Task 2.

`ask_tools` is not on develop. `FakeTools` below follows the companion plan's
Task 6 contract exactly: `execute(calls, ctx) -> [{"tool","id","items","meta"}]`
(one per call, `id = str(i)`, never raises, items with no None-valued keys,
`meta.truncated` always set), `ToolContext` with the companion's seven fields,
and a `RefAssigner` with the same dedupe and independent R/W counters.
"""
import logging
import threading
import time
from dataclasses import dataclass, field

import pytest

import ask_orchestrator as orch
import corroboration_client
import web_answer


# ------------------------------------------------------------- fake ask_tools

class RefAssigner:
    def __init__(self):
        self._refs, self._next = {}, {}

    def assign(self, kind, ident, prefix="R"):
        key = (kind, str(ident))
        if key not in self._refs:
            n = self._next.get(prefix, 1)
            self._refs[key] = f"{prefix}{n}"
            self._next[prefix] = n + 1
        return self._refs[key]


@dataclass
class ToolContext:
    caller_sub: str
    today: object
    deadline: float
    lambda_client: object
    function_name: str
    refs: "RefAssigner" = field(default_factory=lambda: RefAssigner())
    clock: object = time.monotonic


class FakeTools:
    """`responder(call) -> (rows, meta)`; rows are dicts with an `id` and the
    item fields. Refs come from ctx.refs, as the companion's `_envelope` does."""
    RefAssigner = RefAssigner
    ToolContext = ToolContext
    TOOLS = [{"name": n, "description": n, "parameters": {"type": "object"}}
             for n in ("day_overview", "search_records", "latest_activity",
                       "find_people", "count_metric")]

    def __init__(self, responder=None, before=None):
        self.responder = responder or (lambda call: ([], {}))
        self.before = before
        self.batches = []

    def execute(self, calls, ctx):
        if self.before:
            self.before(calls)
        self.batches.append([{"name": c["name"], "args": dict(c["args"])} for c in calls])
        out = []
        for i, call in enumerate(calls):
            rows, meta = self.responder(call)
            items = []
            for row in rows:
                kind = "chunk" if call["name"] == "search_records" else "topic"
                item = {"ref": ctx.refs.assign(kind, row["id"]), "kind": kind,
                        "provenance": "records", **{k: v for k, v in row.items()
                                                    if k != "id" and v is not None}}
                items.append(item)
            meta = dict(meta)
            meta.setdefault("truncated", False)
            out.append({"tool": call["name"], "id": str(i), "items": items, "meta": meta})
        return out


def _ctx(deadline_in=40.0):
    return ToolContext(caller_sub="sub-1", today=None,
                       deadline=time.monotonic() + deadline_in, lambda_client=None,
                       function_name="rag")


def _req(**kw):
    req = {"question": "What did we pour?", "asked": "What did we pour?", "q_from": None,
           "q_to": None, "scope": {}, "policy": "never", "channel": "screen"}
    req.update(kw)
    return req


def _block(answer="Cover is 40 mm [1].", n_sources=2, status="verified", **kw):
    b = web_answer._block(answer=answer, status=status,
                          sources=[{"url": f"https://s{i}.nz/p", "title": f"s{i}.nz",
                                    "domain": f"s{i}.nz"} for i in range(n_sources)],
                          searched=bool(n_sources), **kw)
    return b


@pytest.fixture
def no_model(monkeypatch):
    """Any model call that was not scripted fails the test."""
    def boom(*a, **k):
        raise AssertionError("unexpected model call")
    monkeypatch.setattr(corroboration_client, "call", boom)
    monkeypatch.setattr(web_answer, "general_answer", boom)


# --------------------------------------------------------------------- policy

@pytest.mark.parametrize("channel,scoped,anchored,intent,expected", [
    # explicit intent
    ("voice", False, False, True, "required"),
    ("voice", True, False, True, "required"),      # _wi wins on voice, as today
    ("voice", False, True, True, "required"),
    ("screen", False, False, True, "required"),
    ("screen", False, True, True, "required"),     # unscoped: the phrase opens it
    ("screen", True, False, True, "never"),        # scoped screen keeps never
    ("screen", True, True, True, "never"),
    # scoped / time-anchored, no intent
    ("screen", True, False, False, "never"),
    ("screen", False, True, False, "never"),
    ("voice", True, False, False, "never"),
    ("voice", False, True, False, "never"),
    # otherwise
    ("voice", False, False, False, "offer"),
    ("screen", False, False, False, "auto"),
])
def test_policy_table(channel, scoped, anchored, intent, expected):
    assert orch.policy(channel, scoped, anchored, intent) == expected


# ------------------------------------------------------------------- precheck

class _Spy:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, prompt, **kw):
        self.calls.append((prompt, kw))
        return self.reply


@pytest.mark.parametrize("text,expected", [
    ('{"web": "yes"}', True),
    ('{"web":"no"}', False),
    ('```json\n{"web": "yes"}\n```', True),
    ('Sure: {"web": "no"}', False),
    ("", None),
    ("yes", None),
    ('{"web": "maybe"}', None),
])
def test_precheck_parses_or_returns_none(monkeypatch, text, expected):
    spy = _Spy(corroboration_client.Reply(text=text))
    monkeypatch.setattr(corroboration_client, "call", spy)
    assert orch.precheck("Is 40 mm cover enough?", 10) is expected


def test_precheck_call_shape(monkeypatch):
    spy = _Spy(corroboration_client.Reply(text='{"web": "no"}'))
    monkeypatch.setattr(corroboration_client, "call", spy)
    orch.precheck("q one", 10)
    orch.precheck("q two", 1.5)
    (p1, kw1), (_, kw2) = spy.calls
    assert "q one" in p1
    assert kw1 == {"timeout": 3, "model": web_answer.CHEAP_MODEL, "max_tokens": 512,
                   "effort": "low", "caller": "ask_precheck"}
    assert kw2["timeout"] == 1.5


def test_precheck_failure_is_none_and_logged_without_text(monkeypatch, caplog):
    monkeypatch.setattr(corroboration_client, "call",
                        _Spy(corroboration_client.Reply(error="HTTP 500")))
    with caplog.at_level(logging.INFO):
        assert orch.precheck("secret question words", 10) is None
    assert "precheck=failed" in caplog.text
    assert "secret question words" not in caplog.text


def test_precheck_raising_client_is_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(corroboration_client, "call", boom)
    assert orch.precheck("q", 10) is None


# --------------------------------------------------------------- round1_calls

def test_round1_time_range_bundle():
    calls = orch.round1_calls("what did we pour", "2026-10-14", "2026-10-14", {})
    assert calls == [
        {"name": "day_overview", "args": {"date_from": "2026-10-14", "date_to": "2026-10-14"}},
        {"name": "search_records", "args": {"query": "what did we pour",
                                            "date_from": "2026-10-14",
                                            "date_to": "2026-10-14"}},
    ]


@pytest.mark.parametrize("asked", ["When did we last pour?", "latest crane inspection",
                                   "most recent toolbox talk", "when did Mike visit"])
def test_round1_latest_bundle(asked):
    assert orch.round1_calls(asked, None, None, None) == [
        {"name": "latest_activity", "args": {"query": asked}}]


def test_round1_range_wins_over_latest_words():
    calls = orch.round1_calls("what happened last week", "2026-10-05", "2026-10-11", {})
    assert [c["name"] for c in calls] == ["day_overview", "search_records"]


def test_round1_plain_search():
    assert orch.round1_calls("rebar spacing", None, None, {}) == [
        {"name": "search_records", "args": {"query": "rebar spacing"}}]


def test_round1_scope_is_on_every_call():
    scope = {"site": "site-uuid", "person": "u-7", "topic": "ignored"}
    for asked, lo, hi in (("x", "2026-10-14", "2026-10-15"), ("last pour", None, None),
                          ("x", None, None)):
        for call in orch.round1_calls(asked, lo, hi, scope):
            assert call["args"]["site"] == "site-uuid"
            assert call["args"]["person"] == "u-7"
            assert "topic" not in call["args"]


# ------------------------------------------------------------------- web_tool

def test_web_tool_envelope(monkeypatch):
    seen = {}

    def fake(question, budget, *, kind="general", clock=None, voice=False):
        seen.update(question=question, budget=budget, kind=kind, voice=voice)
        return _block(answer="A" * 2000, n_sources=6)
    monkeypatch.setattr(web_answer, "general_answer", fake)
    refs = RefAssigner()
    refs.assign("topic", "t1")                     # R counter is independent
    env = orch.web_tool("How much cover?", 20, True, refs)
    assert seen == {"question": "How much cover?", "budget": 20, "kind": "general",
                    "voice": True}
    assert env["tool"] == "web_search"
    items = env["items"]
    assert items[0]["ref"] == "W1" and items[0]["kind"] == "web_answer"
    assert items[0]["provenance"] == "web" and len(items[0]["text"]) == 1200
    assert [it["ref"] for it in items[1:]] == ["W2", "W3", "W4", "W5"]
    assert all(it["kind"] == "web_source" and it["provenance"] == "web" for it in items[1:])
    assert items[1]["text"] == "s0.nz" and items[1]["source"] == "https://s0.nz/p"
    assert items[1]["title"] == "s0.nz"
    assert env["meta"]["status"] == "verified" and isinstance(env["meta"]["ms"], int)
    assert "error" not in env["meta"]
    assert refs.assign("web_answer", "turn", "W") == "W1"
    assert refs.assign("web", "https://s0.nz/p", "W") == "W2"
    assert "_trace" not in env["block"] and env["block"]["answer"] == "A" * 2000


@pytest.mark.parametrize("block,status,error", [
    (web_answer._block(failed=True, timed_out=True), "failed", "timeout"),
    (web_answer._block(failed=True), "failed", "upstream"),
    (web_answer._block(failed=True, refused="empty"), "refused", None),
    (web_answer._block(answer="draft", status="unverified", failed=True), "unverified", None),
    (web_answer._block(answer="draft", status="too_long", refused="too_long"), "too_long",
     None),
])
def test_web_tool_status_map(monkeypatch, block, status, error):
    monkeypatch.setattr(web_answer, "general_answer", lambda *a, **k: block)
    env = orch.web_tool("q", 20, False, RefAssigner())
    assert env["meta"]["status"] == status
    assert env["meta"].get("error") == error
    assert bool(env["items"]) == bool(block["answer"])


def test_web_tool_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("vendor down")
    monkeypatch.setattr(web_answer, "general_answer", boom)
    env = orch.web_tool("q", 20, False, RefAssigner())
    assert env["items"] == [] and env["meta"]["status"] == "failed"
    assert env["meta"]["error"] == "upstream"


# ------------------------------------------------------------------ run_round1

def _rows(call):
    if call["name"] == "search_records":
        return [{"id": "c1", "title": "Pour", "text": "B2 poured", "date": "2026-10-14"}], {}
    return [{"id": "t1", "title": "Pour", "text": "B2", "date": "2026-10-14"}], {}


def test_run_round1_records_only_tags_args_and_round(no_model):
    tools = FakeTools(_rows)
    route = {}
    envs, web = orch.run_round1(_req(q_from="2026-10-14", q_to="2026-10-14"), _ctx(), tools,
                                route=route)
    assert web is None
    assert [e["tool"] for e in envs] == ["day_overview", "search_records"]
    assert envs[0]["args"] == {"date_from": "2026-10-14", "date_to": "2026-10-14"}
    assert envs[1]["args"]["query"] == "What did we pour?"
    assert all(e["round"] == 1 for e in envs)
    assert len(tools.batches) == 1
    assert route["calls"] == [["day_overview", "search_records"]]


def test_run_round1_uses_asked_for_records_and_question_for_web(monkeypatch):
    seen = {}

    def fake(question, budget, **kw):
        seen["q"] = question
        seen["budget"] = budget
        return _block()
    monkeypatch.setattr(web_answer, "general_answer", fake)
    tools = FakeTools(_rows)
    envs, web = orch.run_round1(_req(question="and the cover?",
                                     asked="What concrete cover does B2 need?",
                                     policy="required"), _ctx(), tools)
    assert seen["q"] == "and the cover?"
    assert 0 < seen["budget"] <= orch.WEB_WAIT_S
    assert tools.batches[0][0]["args"]["query"] == "What concrete cover does B2 need?"
    assert web["tool"] == "web_search" and web["items"][0]["ref"] == "W1"


def test_round1b_follows_latest_before_without_a_model_call(no_model):
    def rows(call):
        a = call["args"]
        if call["name"] == "day_overview" and a["date_from"] == "2026-10-14":
            return [], {"empty_reason": "nothing_in_range",
                        "latest_before": {"date": "2026-10-12", "from": "topics"}}
        if call["name"] == "day_overview":
            return [{"id": "t9", "title": "Inspection", "date": "2026-10-12"}], {}
        return [], {"empty_reason": "nothing_in_range"}
    tools = FakeTools(rows)
    route = {}
    envs, web = orch.run_round1(_req(q_from="2026-10-14", q_to="2026-10-14",
                                     scope={"site": "S1"}), _ctx(), tools, route=route)
    assert len(tools.batches) == 2
    assert tools.batches[1] == [{"name": "day_overview",
                                 "args": {"date_from": "2026-10-12", "date_to": "2026-10-12",
                                          "site": "S1"}}]
    assert [e["tool"] for e in envs] == ["day_overview", "search_records", "day_overview"]
    assert envs[2]["items"][0]["title"] == "Inspection" and envs[2]["round"] == 1
    assert envs[2]["args"]["date_from"] == "2026-10-12"
    assert route["round1b"] is True
    assert web is None


def test_round1b_runs_once_and_not_on_a_none_date(no_model):
    def rows(call):
        if call["name"] == "day_overview" and call["args"]["date_from"] == "2026-10-14":
            return [], {"empty_reason": "nothing_in_range",
                        "latest_before": {"date": "2026-10-12", "from": "topics"}}
        if call["name"] == "day_overview":     # 1b is empty too, with its own latest_before
            return [], {"empty_reason": "nothing_in_range",
                        "latest_before": {"date": "2026-10-01", "from": "topics"}}
        return [], {}
    tools = FakeTools(rows)
    orch.run_round1(_req(q_from="2026-10-14", q_to="2026-10-14"), _ctx(), tools)
    assert len(tools.batches) == 2

    tools = FakeTools(lambda call: ([], {"empty_reason": "nothing_visible",
                                         "latest_before": {"date": None, "from": "topics"}}))
    orch.run_round1(_req(q_from="2026-10-14", q_to="2026-10-14"), _ctx(), tools)
    assert len(tools.batches) == 1


def test_auto_precheck_yes_runs_web_in_parallel_with_records(monkeypatch):
    web_started = threading.Event()
    overlap = {}
    monkeypatch.setattr(corroboration_client, "call",
                        lambda *a, **k: corroboration_client.Reply(text='{"web":"yes"}'))

    def fake_general(question, budget, **kw):
        web_started.set()
        return _block()
    monkeypatch.setattr(web_answer, "general_answer", fake_general)
    # The records batch waits for the web to have started: only true if both
    # run at once.
    tools = FakeTools(_rows, before=lambda calls: overlap.setdefault(
        "seen", web_started.wait(2)))
    states = []
    route = {}
    envs, web = orch.run_round1(_req(policy="auto"), _ctx(), tools, route=route,
                                on_web_start=lambda: states.append("searching_web"))
    assert overlap["seen"] is True
    assert web is not None and web["meta"]["status"] == "verified"
    assert states == ["searching_web"]
    assert route["precheck"] == "yes" and route["web_ran"] is True


@pytest.mark.parametrize("reply,flag", [(corroboration_client.Reply(text=""), "failed"),
                                        (corroboration_client.Reply(text='{"web":"no"}'),
                                         "no")])
def test_auto_precheck_empty_or_no_means_no_web(monkeypatch, reply, flag):
    monkeypatch.setattr(corroboration_client, "call", lambda *a, **k: reply)

    def boom(*a, **k):
        raise AssertionError("web must not run")
    monkeypatch.setattr(web_answer, "general_answer", boom)
    route = {}
    states = []
    envs, web = orch.run_round1(_req(policy="auto"), _ctx(), FakeTools(_rows), route=route,
                                on_web_start=lambda: states.append("searching_web"))
    assert web is None and states == []
    assert route["precheck"] == flag
    assert not route.get("web_ran")


@pytest.mark.parametrize("policy", ["never", "offer"])
def test_never_and_offer_run_no_web_and_no_precheck(no_model, policy):
    envs, web = orch.run_round1(_req(policy=policy), _ctx(), FakeTools(_rows))
    assert web is None and envs


def test_web_late_is_discarded_and_flagged(monkeypatch):
    release = threading.Event()

    def slow(question, budget, **kw):
        release.wait(5)
        return _block()
    monkeypatch.setattr(web_answer, "general_answer", slow)
    monkeypatch.setattr(orch, "WEB_WAIT_S", 0.3)
    route = {}
    t = time.monotonic()
    try:
        envs, web = orch.run_round1(_req(policy="required"), _ctx(), FakeTools(_rows),
                                    route=route)
        elapsed = time.monotonic() - t
    finally:
        release.set()
    assert elapsed < 2
    assert route["web_late"] is True
    assert web == {"tool": "web_search", "id": "web", "items": [],
                   "meta": {"status": "failed", "error": "timeout"}, "round": 1}
    assert envs


def test_web_wait_never_eats_the_final_answer_reserve(monkeypatch):
    seen = {}

    def fake(question, budget, **kw):
        seen["budget"] = budget
        return _block()
    monkeypatch.setattr(web_answer, "general_answer", fake)
    orch.run_round1(_req(policy="required"), _ctx(deadline_in=20.0), FakeTools(_rows))
    assert seen["budget"] <= 20.0 - orch.FINAL_RESERVE_S
