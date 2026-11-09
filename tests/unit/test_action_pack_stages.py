"""The action agent's stages (Task 11): plan, gather, write, snapshots, verify seam -> the
store payload. Every seam is faked: models (an injected call_model), tools (execute / items
run), the web (general_answer), the fetcher, S3 and the verifier. No network, no AWS."""
import dataclasses
import datetime as dt
import json

import pytest

import action_pack_stages as st
import action_pack_view
import ask_items
import ask_tools
import lambda_action_agent as agent

PACK = "11111111-2222-4333-8444-555555555555"
SITE = "22222222-3333-4444-8555-666666666666"
CO = "33333333-4444-4555-8666-777777777777"
SID1 = "44444444-5555-4666-8777-888888888881"
SID2 = "44444444-5555-4666-8777-888888888882"
TOPIC = "55555555-6666-4777-8888-999999999991"
DOC = "66666666-7777-4888-8999-aaaaaaaaaaa1"
SECRET = "SECRET-RECORD-TEXT pour at grid C4 cracked"
TODAY = dt.date(2026, 11, 1)
WRITER = "deepseek/deepseek-v4.1-flash"
VERIFIER = "google/gemini-3.8-flash"


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)


def make_ctx(clock=None, checkpoint=None, instruction="Review the 10-10 pour against NZS 3109"):
    clock = clock or Clock()
    cps = []

    def cp(reading=None, title=None):
        cps.append({"reading": reading, "title": title, "t": clock()})
        if checkpoint:
            checkpoint(len(cps))
    ctx = agent.RunContext(
        pack_id=PACK, pack={"id": PACK, "instruction": instruction, "site_id": SITE,
                            "company_id": CO, "site_name": "Harbour St"},
        caller_sub="sub-1", site_id=SITE, company_id=CO, s3=FakeS3(), lambda_client=None,
        data_bucket="data", lake_bucket="lake", org_api_function="org",
        rag_search_function="rag", budget=agent.Budget(start=0.0, end=600.0, clock=clock),
        checkpoint=cp, clock=clock)
    ctx.cps = cps
    return ctx


def plan_obj(**over):
    obj = {"reading": "Review the 10-10 pour.", "title": "10-10 pour review",
           "language": "en", "site_matches": True, "date_from": "2026-10-10",
           "date_to": "2026-10-10", "record_queries": ["concrete pour"],
           "document_queries": ["NZS 3109 curing"],
           "web_queries": [{"id": "q1", "text": "NZS 3109 concrete curing requirements"}]}
    obj.update(over)
    return obj


def write_obj(**over):
    obj = {"conclusion": [{"text": "The pour was done.", "refs": ["R1"],
                           "confidence": "supported"}],
           "actions": [{"type": "close_item", "target_stable_id": SID1,
                        "proposed": {"status": "done"}, "rationale": "Records say done.",
                        "refs": ["R1", "R2"], "field_sources": {"status": src("R1")}}]}
    obj.update(over)
    return obj


def src(*refs, said_on=None, source="evidence"):
    """A field_sources entry (spec 2026-11-05 §3.2.3). R1 is the run's topic line."""
    return {"source": source, "refs": list(refs), "said_on": said_on}


class Model:
    """call_model fake: answers by caller; records every call (prompt, deadline, caller)."""

    def __init__(self, plan=None, rounds=None, write=None, used=None, usage=None,
                 finish=None):
        # The default plan tidies (spec 2026-11-05 §3.2.4): the scope gate then lets the
        # default close on SID1 through without a named target and its round-1 hop.
        self.answers = {"action_plan": list(plan or [plan_obj(change_scope="tidy")]),
                        "action_round": list(rounds or []),
                        "action_write": list(write or [write_obj()])}
        self.used = used or {}
        self.usage = usage
        self.finish = {k: list(v) for k, v in (finish or {}).items()}   # caller -> reasons
        self.calls = []

    def __call__(self, prompt, *, max_tokens, force_json, model, deadline, caller, info):
        self.calls.append({"prompt": prompt, "deadline": deadline, "caller": caller,
                           "model": model})
        queue = self.answers[caller]
        ans = queue.pop(0) if len(queue) > 1 else (queue[0] if queue
                                                     else {"calls": [], "done": True})
        info["model"] = self.used.get(caller, model)
        if self.finish.get(caller):
            info["finish_reason"] = self.finish[caller].pop(0)
        if self.usage is not None:
            info["usage"] = {"prompt_tokens": self.usage, "completion_tokens": 0}
        if isinstance(ans, tuple):          # (None, error)
            return ans
        return (ans if isinstance(ans, str) else json.dumps(ans)), None


class Tools:
    """execute + items_run fakes; refs from the run's shared RefAssigner."""

    def __init__(self, clock=None, advance=0.0, fail=False):
        self.calls, self.items_calls = [], []
        self.clock, self.advance, self.fail = clock, advance, fail

    def execute(self, calls, tctx):
        assert len(calls) <= ask_tools.MAX_BATCH
        self.calls.append(calls)
        if self.clock is not None:
            self.clock.t += self.advance
        out = []
        for i, c in enumerate(calls):
            if self.fail:
                out.append({"tool": c["name"], "id": str(i), "items": [],
                            "meta": {"error": "upstream"}})
            elif c["name"] == ask_tools.DOC_TOOL:
                out.append({"tool": c["name"], "id": str(i), "meta": {}, "items": [
                    {"ref": tctx.refs.assign("document", "dc1", "D"), "kind": "document",
                     "provenance": "documents", "title": "Spec", "heading": "Curing",
                     "page_from": 4, "page_to": 4, "document_id": DOC, "revision": 2,
                     "site_id": SITE, "text": "Cure for 7 days."}]})
            else:
                out.append({"tool": c["name"], "id": str(i), "meta": {}, "items": [
                    {"ref": tctx.refs.assign("topic", TOPIC), "kind": "topic",
                     "provenance": "records", "date": "2026-10-10", "site": "Harbour St",
                     "who": "Ann", "title": "Pour", "text": SECRET, "topic_id": TOPIC}]})
        return out

    def items_run(self, args, tctx, call_id="items"):
        self.items_calls.append(args)
        if self.fail:
            return {"tool": "list_items", "id": call_id, "items": [],
                    "meta": {"error": "upstream"}}
        rows = []
        for sid, no in ((SID1, 1), (SID2, 2)):
            row = {f: None for f in ask_items.ITEM_FIELDS}
            row.update(stable_id=sid, task_no=no, text=f"Item {no}", status="open",
                       deadline="2026-10-20", updated_at="2026-10-09T01:00:00+00:00",
                       site_id=SITE, report_date="2026-10-09")
            rows.append({"ref": tctx.refs.assign("item", sid, "R"), "kind": "item",
                         "provenance": "records", **row})
        return {"tool": "list_items", "id": call_id, "items": rows, "meta": {}}


class Web:
    def __init__(self, status="verified"):
        self.queries, self.status = [], status

    def __call__(self, text, budget, kind="general", voice=False, clock=None):
        self.queries.append(text)
        return {"status": self.status, "answer": "Cure 7 days [1].",
                "sources": [{"url": "https://www.branz.co.nz/curing", "title": "Curing",
                             "domain": "branz.co.nz"}]}


class Fetcher:
    def __init__(self, ok=True):
        self.ok, self.urls = ok, []

    def fetch(self, url):
        self.urls.append(url)
        if not self.ok:
            return {"ok": False, "reason": "robots", "text": None}
        return {"ok": True, "text": "page text", "fetched_at": "2026-11-01T00:00:00+00:00"}


class Verify:
    def __init__(self, label="supported", model=VERIFIER):
        self.label, self.model, self.calls = label, model, []

    def __call__(self, draft, *, registry, items, sources, ctx, writer_model, deadline,
                 writer_served=None, user_values=None):
        self.calls.append({"draft": draft, "sources": sources, "writer_model": writer_model,
                           "deadline": deadline, "registry": registry,
                           "user_values": user_values})
        return {"conclusion": [{"index": i, "confidence": c["confidence"]}
                               for i, c in enumerate(draft["conclusion"])],
                "actions": [{"index": i, "verifier": self.label}
                            for i in range(len(draft["actions"]))],
                "model": self.model, "counts": {"checked": len(draft["actions"])}}


def deps(model=None, tools=None, web=None, fetcher=None, verify=None, docs=True,
         web_on=True):
    tools = tools or Tools()
    return st.Deps(call_model=model or Model(), execute=tools.execute,
                   items_run=tools.items_run, general_answer=web or Web(),
                   fetcher=fetcher or Fetcher(), verify=verify or Verify(),
                   docs_on=lambda: docs, web_on=lambda: web_on,
                   now=lambda: dt.datetime(2026, 11, 1, 1, 0, tzinfo=dt.timezone.utc),
                   today=lambda: TODAY)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACTION_WRITER_MODEL", WRITER)
    monkeypatch.delenv("ACTION_TOKEN_CAP", raising=False)
    monkeypatch.delenv("ACTION_MAX_ROUNDS", raising=False)


# ---- happy path / payload ------------------------------------------------------------

def test_payload_passes_the_store_checks_and_cites_only_what_survives():
    ctx = make_ctx()
    payload = st.run(ctx, deps())
    assert action_pack_view.payload_shape_error(payload) is None
    assert action_pack_view.snapshot_key_error(payload, CO, PACK) is None
    assert payload["title"] == "10-10 pour review"
    assert payload["as_of"] == "2026-11-01T01:00:00+00:00"
    assert payload["models"] == {"planner": WRITER, "writer": WRITER, "verifier": VERIFIER}
    [a] = payload["actions"]
    assert a["seq"] == 1 and a["target_stable_id"] == SID1 and a["verifier"] == "supported"
    assert a["target_snapshot"] == {"status": "open", "deadline": "2026-10-20",
                                    "responsible": None, "priority": None, "text": "Item 1",
                                    "updated_at": "2026-10-09T01:00:00+00:00"}
    refs = {s["ref"] for s in payload["sources"]}
    assert refs == {"R1", "R2"}                       # only the cited refs
    by_ref = {s["ref"]: s for s in payload["sources"]}
    assert by_ref["R1"]["kind"] == "topic" and by_ref["R1"]["excerpt"] == SECRET
    assert by_ref["R2"]["kind"] == "item" and by_ref["R2"]["stable_id"] == SID1
    # the plan's reading/title went out at the first checkpoint
    assert ctx.cps[0]["reading"] == "Review the 10-10 pour." and ctx.cps[0]["title"]
    trace = payload["run_trace"]
    assert trace["truncated"] is False and trace["rounds"] >= 1
    # Counts and codes only (plan_scope is a code since PR-3).
    assert all(isinstance(v, (int, bool, dict, list, str)) for v in trace.values())
    assert action_pack_view.fail_trace_error(trace) is None


def test_round_one_is_deterministic_and_pinned_to_the_site():
    tools = Tools()
    model = Model(plan=[plan_obj(date_from="2026-10-01", date_to="2026-10-20")])
    st.run(make_ctx(), deps(model=model, tools=tools))
    first = tools.calls[0]
    assert [c["name"] for c in first] == ["day_overview"] * 3
    assert [(c["args"]["date_from"], c["args"]["date_to"]) for c in first] == [
        ("2026-10-14", "2026-10-20"), ("2026-10-07", "2026-10-13"),
        ("2026-10-01", "2026-10-06")]
    assert all(c["args"]["site"] == SITE for batch in tools.calls for c in batch)
    assert tools.items_calls[0] == {"limit": st.ROUND1_ITEMS_LIMIT, "site_id": SITE}
    assert tools.calls[1] == [{"name": ask_tools.DOC_TOOL,
                               "args": {"query": "NZS 3109 curing", "site": SITE}}]


def test_round_one_failing_everywhere_is_reason_tools():
    with pytest.raises(agent.ToolsFailure):
        st.run(make_ctx(), deps(tools=Tools(fail=True)))


# ---- plan --------------------------------------------------------------------------------

def test_plan_schema_violation_gets_one_repair():
    model = Model(plan=[{"reading": "x"}, plan_obj()])
    payload = st.run(make_ctx(), deps(model=model))
    assert payload["run_trace"]["plan_repairs"] == 1
    plans = [c for c in model.calls if c["caller"] == "action_plan"]
    assert len(plans) == 2 and "rejected (keys)" in plans[1]["prompt"]


@pytest.mark.parametrize("bad", [
    {"reading": "x"},
    plan_obj(extra=1),
    plan_obj(title="t" * 121),
    plan_obj(site_matches="yes"),
    plan_obj(date_from="2026-10-12", date_to="2026-10-10"),
    plan_obj(date_from="2026-12-01", date_to="2026-12-02"),
    plan_obj(web_queries=[{"id": "q1", "text": "a"}] * 2),
    plan_obj(web_queries=[{"id": f"q{i}", "text": "a"} for i in range(4)]),
    plan_obj(web_queries=[{"id": "q1", "text": "x" * 301}]),
    plan_obj(record_queries="concrete"),
    "not json at all",
])
def test_plan_schema_violations_are_rejected(bad):
    model = Model(plan=[bad, bad])
    with pytest.raises(agent.ModelFailure):
        st.run(make_ctx(), deps(model=model))
    assert [c["caller"] for c in model.calls] == ["action_plan", "action_plan"]


def test_plan_range_is_bounded():
    plan, err = st.validate_plan(plan_obj(date_from="2026-08-01", date_to="2026-11-30"),
                                 TODAY)
    assert err is None and plan.clamped
    assert plan.date_to == TODAY
    assert (plan.date_to - plan.date_from).days + 1 == st.MAX_PLAN_DAYS


def test_a_different_site_gets_the_fixed_not_found_sentence():
    model = Model(plan=[plan_obj(site_matches=False)])
    ctx = make_ctx()
    payload = st.run(ctx, deps(model=model))
    assert payload["reading"].startswith(
        "The site named in the instruction was not found in your projects; this pack "
        "covers Harbour St.")
    assert ctx.cps[0]["reading"] == payload["reading"]
    model = Model(plan=[plan_obj(site_matches=False, language="zh")])
    payload = st.run(make_ctx(), deps(model=model))
    assert payload["reading"].startswith("指令中提到的工地不在您的项目中")


def test_the_planner_sees_no_record_evidence():
    model = Model()
    st.run(make_ctx(), deps(model=model))
    [plan] = [c for c in model.calls if c["caller"] == "action_plan"]
    assert SECRET not in plan["prompt"] and "Item 1" not in plan["prompt"]
    assert plan is model.calls[0]                     # before any tool ran


# ---- web lane ----------------------------------------------------------------------------

def test_web_search_only_by_planned_id_and_no_record_text_reaches_it():
    web = Web()
    rounds = [{"calls": [{"tool": "web_search", "args": {"query": SECRET}},
                         {"tool": "web_search", "args": {"query_id": "zz"}},
                         {"tool": "web_search", "args": {"query_id": "q1", "text": SECRET}},
                         {"tool": "web_search", "args": {"query_id": "q1"}}],
               "done": True}]
    model = Model(rounds=rounds)
    payload = st.run(make_ctx(), deps(model=model, web=web))
    assert web.queries == ["NZS 3109 concrete curing requirements"]   # the planned text only
    trace = payload["run_trace"]
    assert trace["web_free_text_refused"] == 2
    assert trace["web"]["web_refused"] == 1 and trace["web"]["web_searches"] == 1


def test_web_off_means_no_planned_query_is_searched():
    """ENABLE_WEB_ANSWER off (the Ask web switch): the round model is not offered
    web_search, a web call it makes anyway is refused and counted, and nothing reaches
    general_answer. The planned queries are counted as web_disabled."""
    web = Web()
    rounds = [{"calls": [{"tool": "web_search", "args": {"query_id": "q1"}}], "done": True}]
    model = Model(rounds=rounds)
    payload = st.run(make_ctx(), deps(model=model, web=web, web_on=False))
    assert web.queries == []
    trace = payload["run_trace"]
    assert trace["web_disabled"] == 1                   # the plan's one query
    assert trace["web_disabled_refused"] == 1
    round_prompts = [c["prompt"] for c in model.calls if c["caller"] == "action_round"]
    assert round_prompts and all("- web_search:" not in p for p in round_prompts)


def test_web_on_is_the_ask_switch_by_default(monkeypatch):
    import web_answer
    d = st.Deps(execute=1, items_run=1, general_answer=1, fetcher=1, verify=1,
                docs_on=1).resolve()
    assert d.web_on is web_answer.enabled
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "false")
    assert d.web_on() is False


def test_an_unverified_web_answer_never_becomes_evidence():
    rounds = [{"calls": [{"tool": "web_search", "args": {"query_id": "q1"}}], "done": True}]
    verify = Verify()
    payload = st.run(make_ctx(), deps(model=Model(rounds=rounds), web=Web("unverified"),
                                      verify=verify))
    assert not any(r.startswith("W") for r in verify.calls[0]["registry"])
    assert payload["run_trace"]["web"]["web_unverified_dropped"] == 1


def test_cited_web_sources_are_snapshotted_and_a_failed_fetch_keeps_the_url():
    rounds = [{"calls": [{"tool": "web_search", "args": {"query_id": "q1"}}], "done": True}]
    write = write_obj(conclusion=[{"text": "Cure 7 days.", "refs": ["D1", "W1", "W2"],
                                   "confidence": "supported"}])
    ctx = make_ctx()
    payload = st.run(ctx, deps(model=Model(rounds=rounds, write=[write])))
    w2 = {s["ref"]: s for s in payload["sources"]}["W2"]
    assert w2["url"] == "https://www.branz.co.nz/curing"
    assert w2["snapshot_s3_key"] == action_pack_view.snapshot_key(CO, PACK, "W2")
    assert ctx.s3.puts[0]["Key"] == w2["snapshot_s3_key"]
    assert action_pack_view.snapshot_key_error(payload, CO, PACK) is None

    ctx = make_ctx()
    payload = st.run(ctx, deps(model=Model(rounds=rounds, write=[write]),
                               fetcher=Fetcher(ok=False)))
    w2 = {s["ref"]: s for s in payload["sources"]}["W2"]
    assert w2["url"] and w2["snapshot_s3_key"] is None and ctx.s3.puts == []


# ---- gather limits -----------------------------------------------------------------------

def test_gather_respects_eight_rounds_of_four_calls():
    n = iter(range(1000))

    def many():
        return {"calls": [{"tool": "search_records", "args": {"query": f"q{next(n)}"}}
                          for _ in range(6)], "done": False}
    model = Model(rounds=[many() for _ in range(20)])
    tools = Tools()
    payload = st.run(make_ctx(), deps(model=model, tools=tools))
    trace = payload["run_trace"]
    assert trace["rounds"] == st.MAX_ROUNDS and trace["rounds_exhausted"] is True
    rounds = [c for c in model.calls if c["caller"] == "action_round"]
    assert len(rounds) == st.MAX_ROUNDS - 1
    model_batches = [b for b in tools.calls if b[0]["name"] == "search_records"]
    assert len(model_batches) == st.MAX_ROUNDS - 1
    assert all(len(b) == st.MAX_CALLS_PER_ROUND for b in model_batches)
    assert trace["calls_refused"] == 2 * (st.MAX_ROUNDS - 1)


def test_unknown_and_unscoped_tools_are_refused_and_site_is_pinned():
    rounds = [{"calls": [{"tool": "count_metric", "args": {"metric": "duration"}},
                         {"tool": "rm_rf", "args": {}},
                         {"tool": "search_records", "args": {"query": "x", "site": "Other"}},
                         {"tool": "list_items", "args": {"site_id": "elsewhere"}}],
               "done": True}]
    tools = Tools()
    payload = st.run(make_ctx(), deps(model=Model(rounds=rounds), tools=tools))
    assert payload["run_trace"]["calls_refused"] == 2
    assert tools.calls[-1] == [{"name": "search_records", "args": {"query": "x",
                                                                    "site": SITE}}]
    assert tools.items_calls[-1] == {"site_id": SITE}


def test_a_repeated_call_is_not_run_again():
    call = {"tool": "search_records", "args": {"query": "pour"}}
    model = Model(rounds=[{"calls": [call], "done": False}, {"calls": [call], "done": False}])
    tools = Tools()
    payload = st.run(make_ctx(), deps(model=model, tools=tools))
    assert sum(1 for b in tools.calls if b[0]["name"] == "search_records") == 1
    assert payload["run_trace"]["calls_repeated"] == 1


# ---- budgets ------------------------------------------------------------------------------

def test_token_cap_stops_gathering_and_marks_the_pack_truncated(monkeypatch):
    monkeypatch.setenv("ACTION_TOKEN_CAP", "1000")
    model = Model(rounds=[{"calls": [{"tool": "search_records", "args": {"query": "x"}}],
                           "done": False}], usage=5000)
    payload = st.run(make_ctx(), deps(model=model))
    assert [c["caller"] for c in model.calls] == ["action_plan", "action_write"]
    trace = payload["run_trace"]
    assert trace["truncated"] is True and "tokens" in trace["truncated_reasons"]
    assert trace["tokens"] >= 10000 and trace["token_cap"] == 1000
    assert action_pack_view.payload_shape_error(payload) is None


def test_gather_deadline_gives_a_partial_pack():
    clock = Clock()
    # round 1 (two rag-search batches) takes nearly the whole gather window (420 s)
    tools = Tools(clock=clock, advance=200.0)
    model = Model(rounds=[{"calls": [{"tool": "search_records", "args": {"query": "x"}}],
                           "done": False}])
    payload = st.run(make_ctx(clock=clock), deps(model=model, tools=tools))
    assert "action_round" not in [c["caller"] for c in model.calls]
    trace = payload["run_trace"]
    assert trace["truncated"] is True and trace["truncated_reasons"] == ["time"]
    assert payload["actions"]                             # the write still ran


def test_no_time_to_write_fails_budget():
    clock = Clock()
    tools = Tools(clock=clock, advance=300.0)            # past the write deadline too
    with pytest.raises(agent.BudgetExceeded):
        st.run(make_ctx(clock=clock), deps(tools=tools))


def test_every_model_call_gets_the_stage_deadline():
    clock = Clock()
    tools = Tools(clock=clock, advance=7.0)
    model = Model(rounds=[{"calls": [{"tool": "search_records", "args": {"query": "x"}}],
                           "done": True}])
    ctx = make_ctx(clock=clock)
    seen = []
    real = model.__call__

    def spy(prompt, **kw):
        stage = "write" if kw["caller"] == "action_write" else "gather"
        hop = st.TOOL_HOP_S if kw["caller"] == "action_round" else 0.0   # fix round 1
        seen.append((kw["deadline"], ctx.left(stage) - hop))
        return real(prompt, **kw)
    st.run(ctx, deps(model=spy, tools=tools))
    assert len(seen) == 3
    assert all(d is not None and 0 < d == left for d, left in seen)
    assert seen[-1][0] <= ctx.left("write")


def test_write_render_drops_web_first_then_documents_and_truncates(monkeypatch):
    monkeypatch.setattr(st, "WRITE_RENDER_MAX_CHARS", 430)  # dated lines (PR-2): was 380
    rounds = [{"calls": [{"tool": "web_search", "args": {"query_id": "q1"}}], "done": True}]
    model = Model(rounds=rounds)
    payload = st.run(make_ctx(), deps(model=model))
    [write] = [c for c in model.calls if c["caller"] == "action_write"]
    assert "W1 |" not in write["prompt"] and "W2 |" not in write["prompt"]
    assert "D1 |" not in write["prompt"]                 # documents go before records
    assert "R1 |" in write["prompt"] and f"stable_id {SID1}" in write["prompt"]
    trace = payload["run_trace"]
    assert trace["truncated"] is True and "write_render" in trace["truncated_reasons"]
    assert trace["write_refs_dropped"] == 3


def test_write_render_shows_the_full_excerpt():
    long = "x" * 2500
    tools = Tools()
    real = tools.execute

    def execute(calls, tctx):
        envs = real(calls, tctx)
        for e in envs:
            for it in e["items"]:
                if it["kind"] == "topic":
                    it["text"] = long
        return envs
    model = Model()
    st.run(make_ctx(), st.Deps(**{**deps(model=model).__dict__, "execute": execute}))
    [write] = [c for c in model.calls if c["caller"] == "action_write"]
    assert long in write["prompt"]
    rounds_or_plan = [c for c in model.calls if c["caller"] != "action_write"]
    assert all(long not in c["prompt"] for c in rounds_or_plan)


# ---- write schema -------------------------------------------------------------------------

def test_write_schema_violations_are_dropped_and_counted():
    write = {"conclusion": [
        {"text": "ok", "refs": ["R1"], "confidence": "supported"},
        {"text": "no refs", "refs": [], "confidence": "supported"},     # -> uncertain
        {"text": "bad conf", "refs": ["R1"], "confidence": "sure"},
        {"text": "made-up ref", "refs": ["R99"], "confidence": "uncertain"}],
        "actions": [
        {"type": "close_item", "target_stable_id": SID1, "proposed": {}, "rationale": "r",
         "refs": ["R1"], "field_sources": {"status": src("R1")}},
        {"type": "close_item", "target_stable_id": "77777777-0000-4000-8000-000000000000",
         "proposed": {"status": "done"}, "rationale": "r", "refs": ["R1"]},
        {"type": "close_item", "target_stable_id": "the pour item",
         "proposed": {"status": "done"}, "rationale": "r", "refs": ["R1"]},
        {"type": "update_item", "target_stable_id": SID2, "proposed": {"text": "new"},
         "rationale": "r", "refs": ["R1"]},
        {"type": "update_item", "target_stable_id": SID2,
         "proposed": {"deadline": "next week"}, "rationale": "r", "refs": ["R1"]},
        {"type": "close_item", "target_stable_id": SID2, "proposed": {"status": "open"},
         "rationale": "r", "refs": ["R1"]},
        {"type": "delete_item", "target_stable_id": SID2, "proposed": {},
         "rationale": "r", "refs": ["R1"]},
        {"type": "create_item", "proposed": {"text": "Book the pump"}, "rationale": "r",
         "refs": []},
        {"type": "create_item", "proposed": {"text": "Book the pump"}, "rationale": "r",
         "refs": ["R99"]},
        {"type": "draft_email", "proposed": {"subject": "s"}, "rationale": "r",
         "refs": ["R1"]},
        {"type": "note", "proposed": {"text": "Check curing"}, "rationale": "r",
         "refs": ["D1", "R77"]},
        {"type": "update_item", "target_stable_id": SID2,
         "proposed": {"deadline": "2026-11-10", "status": "in_progress"},
         "rationale": "r", "refs": ["R1"],
         "field_sources": {"deadline": src("R1", said_on="2026-10-10"),
                           "status": src("R1")}}]}
    verify = Verify()
    payload = st.run(make_ctx(), deps(model=Model(write=[write]), verify=verify))
    draft = verify.calls[0]["draft"]
    assert [c["text"] for c in draft["conclusion"]] == ["ok", "no refs", "made-up ref"]
    assert [c["confidence"] for c in draft["conclusion"]] == ["supported", "uncertain",
                                                              "uncertain"]
    assert draft["conclusion"][2]["refs"] == []
    assert [(a["type"], a["target_stable_id"]) for a in draft["actions"]] == [
        ("close_item", SID1), ("note", None), ("update_item", SID2)]
    assert draft["actions"][0]["proposed"] == {"status": "done"}        # close defaults done
    assert draft["actions"][1]["refs"] == ["D1"]
    dropped = payload["run_trace"]["write_dropped"]
    # Typed keys (spec 2026-11-05 §5.2): <type>__<reason>, conclusion__<reason>.
    assert dropped == {
        "conclusion__downgraded": 1, "conclusion__confidence": 1, "conclusion__refs": 1,
        "close_item__targets": 2, "close_item__proposed": 1,
        "update_item__proposed": 2, "unknown__type": 1,
        "create_item__no_refs": 2, "create_item__refs": 1, "draft_email__proposed": 1,
        "note__refs": 1}
    assert action_pack_view.payload_shape_error(payload) is None
    assert [a["seq"] for a in payload["actions"]] == [1, 2, 3]


def test_write_gets_one_repair_and_nothing_parsing_is_reason_model():
    model = Model(write=["{not json", write_obj()])
    payload = st.run(make_ctx(), deps(model=model))
    assert payload["run_trace"]["write_repairs"] == 1
    with pytest.raises(agent.ModelFailure):
        st.run(make_ctx(), deps(model=Model(write=["nope", {"conclusion": "x"}])))


def test_a_writer_error_is_reason_model():
    with pytest.raises(agent.ModelFailure):
        st.run(make_ctx(), deps(model=Model(write=[(None, "all models failed")])))


# ---- models used / verify seam ---------------------------------------------------------

def test_the_models_actually_used_are_recorded_after_a_fallback():
    luna = "openai/gpt-6-luna-pro"
    verify = Verify()
    model = Model(used={"action_write": luna, "action_plan": "xiaomi/mimo-v2.6-pro"})
    payload = st.run(make_ctx(), deps(model=model, verify=verify))
    assert payload["models"] == {"planner": "xiaomi/mimo-v2.6-pro", "writer": luna,
                                 "verifier": VERIFIER}
    assert verify.calls[0]["writer_model"] == luna
    assert all(c["model"] == WRITER for c in model.calls)            # what was asked for


def test_verify_gets_its_deadline_and_the_snapshotted_sources():
    verify = Verify()
    ctx = make_ctx()
    st.run(ctx, deps(verify=verify))
    call = verify.calls[0]
    assert call["deadline"] == ctx.deadline_for("verify")
    assert set(call["sources"]) == {"R1", "R2"}


def test_unsupported_actions_are_stored_with_their_label():
    payload = st.run(make_ctx(), deps(verify=Verify(label="unsupported")))
    assert payload["actions"][0]["verifier"] == "unsupported"
    assert {s["ref"] for s in payload["sources"]} == {"R1", "R2"}


def test_a_verifier_that_drops_everything_leaves_only_cited_sources():
    class DropAll(Verify):
        def __call__(self, draft, **kw):
            return {"conclusion": [], "actions": [], "model": VERIFIER}
    payload = st.run(make_ctx(), deps(verify=DropAll()))
    assert payload["sources"] == [] and payload["actions"] == []


# ---- cancellation -------------------------------------------------------------------------

def test_cancel_at_a_checkpoint_stops_every_further_call():
    def cp(n):
        if n == 2:                                    # the checkpoint after round 1
            raise agent.Cancelled()
    model = Model(rounds=[{"calls": [{"tool": "search_records", "args": {"query": "x"}}],
                           "done": False}])
    tools = Tools()
    verify = Verify()
    with pytest.raises(agent.Cancelled):
        st.run(make_ctx(checkpoint=cp), deps(model=model, tools=tools, verify=verify))
    assert [c["caller"] for c in model.calls] == ["action_plan"]
    assert len(tools.calls) == 2 and len(tools.items_calls) == 1    # round 1 only
    assert verify.calls == []


# ---- S8: the rag-search hop retries once, on throttle or 5xx only -----------------------

class ClientError(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class Payload:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode()


class FlakyLambda:
    def __init__(self, errors, body=None):
        self.errors, self.body, self.n = list(errors), body or {}, 0

    def invoke(self, **kw):
        self.n += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"StatusCode": 200, "Payload": Payload(self.body)}


@pytest.mark.parametrize("exc,retried", [
    (ClientError("TooManyRequestsException", 429), True),
    (ClientError("ThrottlingException", 400), True),
    (ClientError("ServiceException", 500), True),
    (ClientError("EC2ThrottledException", 502), True),
    (ClientError("ResourceNotFoundException", 404), False),
    (ClientError("InvalidRequestContentException", 400), False),
    (TimeoutError("read timeout"), False),
])
def test_retry_once_only_on_throttle_or_5xx(exc, retried):
    client, trace = FlakyLambda([exc]), {}
    wrapper = st.RetryOnceLambda(client, deadline=100.0, clock=Clock(), trace=trace)
    if retried:
        assert wrapper.invoke(FunctionName="rag")["StatusCode"] == 200
        assert client.n == 2 and trace["rag_retries"] == 1
    else:
        with pytest.raises(type(exc)):
            wrapper.invoke(FunctionName="rag")
        assert client.n == 1 and "rag_retries" not in trace


def test_retry_happens_once_and_not_without_time():
    client = FlakyLambda([ClientError("TooManyRequestsException", 429)] * 2)
    wrapper = st.RetryOnceLambda(client, deadline=100.0, clock=Clock(), trace={})
    with pytest.raises(ClientError):
        wrapper.invoke(FunctionName="rag")
    assert client.n == 2
    client = FlakyLambda([ClientError("TooManyRequestsException", 429)])
    wrapper = st.RetryOnceLambda(client, deadline=100.0, clock=Clock(98.0), trace={})
    with pytest.raises(ClientError):
        wrapper.invoke(FunctionName="rag")
    assert client.n == 1


def test_the_retry_sits_below_the_items_tool_catch():
    row = {f: None for f in ask_items.ITEM_FIELDS}
    row.update(stable_id=SID1, status="open")
    client = FlakyLambda([ClientError("TooManyRequestsException", 429)],
                         body={"rows": [row], "meta": {}})
    trace = {}
    tctx = ask_tools.ToolContext(
        caller_sub="sub-1", today=TODAY, deadline=100.0,
        lambda_client=st.RetryOnceLambda(client, deadline=100.0, clock=Clock(), trace=trace),
        function_name="rag", clock=Clock())
    env = ask_items.run({"site_id": SITE}, tctx)
    assert [it["stable_id"] for it in env["items"]] == [SID1]
    assert client.n == 2 and trace["rag_retries"] == 1


# ---- dated evidence (spec 2026-11-05 §6) ---------------------------------------------------

TOPIC_LINE = {"ref": "R3", "kind": "topic", "date": "2026-10-28", "time": "09:15",
              "site": "Alpha", "who": "Ann", "title": "Pour | review",
              "text": "Pour ```next Friday```\nok"}


def test_day_renders_date_and_weekday():
    assert st._day("2026-11-06") == "2026-11-06 (Fri)"
    assert st._day(dt.date(2026, 11, 1)) == "2026-11-01 (Sun)"
    assert st._day("2026-11-06T09:00:00+13:00") == "2026-11-06 (Fri)"
    for bad in (None, "", "next Friday", "2026-13-01", 7):
        assert st._day(bad) == "-"


def test_record_line_has_said_on_weekday():
    assert st._record_line(TOPIC_LINE) == (
        "R3 | said_on 2026-10-28 (Wed) 09:15 | Alpha | Ann | Pour / review — "
        "Pour '''next Friday''' ok")
    assert st._line(TOPIC_LINE, full=True) == st._record_line(TOPIC_LINE)
    chunk = {"ref": "R4", "kind": "chunk", "date": None, "text": "x"}
    assert st._line(chunk, full=True) == "R4 | said_on - | - | - | - — x"


def test_item_line_has_deadline_text_and_raised_on():
    item = {"ref": "R2", "kind": "item", "stable_id": SID1, "task_no": 4, "status": "open",
            "deadline": None, "responsible": "Ann", "priority": None,
            "deadline_text": "end of\nnext week | ```x```", "raised_on": "2026-10-20",
            "report_date": "2026-10-30", "text": "Fix the door"}
    line = st._line(item, full=True)
    assert line == (f"R2 | item | stable_id {SID1} | #4 | status open | deadline - | "
                    "responsible Ann | priority - | deadline_text \"end of next week / "
                    "'''x'''\" | raised_on 2026-10-20 (Tue) — Fix the door")
    bare = st._line(dict(item, deadline_text=None, raised_on=None), full=True)
    assert "deadline_text" not in bare and "| raised_on - —" in bare


def test_write_prompt_states_today_with_weekday():
    model = Model()
    st.run(make_ctx(), deps(model=model))
    header = ("Today (New Zealand): 2026-11-01 (Sun) — today is not the date the "
              "records were said.")
    by = {}
    for c in model.calls:
        by.setdefault(c["caller"], c["prompt"])
    for caller in ("action_plan", "action_round", "action_write"):
        assert header in by[caller], caller
    write = by["action_write"]
    assert "said_on" in write and "raised_on" in write and "deadline_text" in write
    assert "said_on 2026-" in write                    # the evidence lines are dated


def test_ask_item_line_unchanged():
    """Ask shares ask_orchestrator._item_line: the agent's dated render must not touch it."""
    import ask_orchestrator
    assert ask_orchestrator._item_line(TOPIC_LINE) == (
        "R3 | 2026-10-28 09:15 | Alpha | Ann | Pour / review — Pour '''next Friday''' ok")
    assert ask_orchestrator._item_line({"ref": "R4", "kind": "chunk", "date": None,
                                        "text": "x"}) == "R4 | - | - | - | - — x"


# ---- PR-1 Task 1.2: judge_call routes the writer by ACTION_WRITER_* ------------------

def _spy_complete_json(monkeypatch):
    import llm_tools_client as tc
    seen = []

    def complete_json(prompt, **kw):
        seen.append(kw)
        return tc.ChatTurn(text="{}", model="served", requested_model=kw.get("model") or "m")
    monkeypatch.setattr(tc, "complete_json", complete_json)
    monkeypatch.setattr(tc, "_now", lambda: 1000.0)
    return seen


def test_judge_call_passes_explicit_chain(monkeypatch):
    muse = "meta/muse-spark-1.3-contributor"
    monkeypatch.setenv("ACTION_WRITER_MODEL", muse)
    monkeypatch.setenv("ACTION_WRITER_REASONING", "high")
    monkeypatch.setenv("ACTION_WRITER_FALLBACKS", "a/b")
    monkeypatch.delenv("ACTION_WRITER_PROVIDER", raising=False)
    monkeypatch.setenv("ASK_JUDGE_MODEL", muse)
    monkeypatch.setenv("ASK_JUDGE_PROVIDER", "coreweave/fp8")
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", "judge/fb1,judge/fb2,judge/fb3")
    seen = _spy_complete_json(monkeypatch)
    st.judge_call("p", max_tokens=10, model=muse, deadline=100.0, caller="action_write",
                  info={})
    kw = seen[0]
    assert kw["chain"] == [muse, "a/b"]
    assert kw["primary_extras"] == {"reasoning": {"effort": "high"}}
    assert kw["deadline"] == 1100.0
    # Two entries in the writer's own chain -> the fallback share (not the judge's list).
    assert kw["attempt_timeout"] == pytest.approx(100.0 * st.FALLBACK_ATTEMPT_SHARE)

    monkeypatch.delenv("ACTION_WRITER_FALLBACKS")
    st.judge_call("p", max_tokens=10, model=muse, deadline=100.0, caller="action_write",
                  info={})
    assert seen[1]["chain"] == [muse] and seen[1]["attempt_timeout"] == 100.0


def test_judge_call_without_writer_model_is_unchanged(monkeypatch):
    monkeypatch.delenv("ACTION_WRITER_MODEL", raising=False)
    monkeypatch.setenv("ASK_JUDGE_MODEL", WRITER)
    monkeypatch.delenv("ASK_JUDGE_FALLBACKS", raising=False)
    seen = _spy_complete_json(monkeypatch)
    st.judge_call("p", max_tokens=10, model=WRITER, deadline=100.0, caller="c", info={})
    assert seen[0] == {"model": WRITER, "max_tokens": 10, "deadline": 1100.0, "caller": "c",
                       "attempt_timeout": 100.0}


# ---- PR-3 Task 3.1: plan v2 keys, tolerant (spec 2026-11-05 §3.2.1, §4.2) ----------------

V2 = {"change_scope": "named",
      "targets": [{"id": "t1", "task_no": 4, "phrase": None},
                  {"id": "t2", "task_no": None, "phrase": "door hardware"}],
      "stated_values": [{"target": "t1", "field": "deadline", "value": "2026-11-13"},
                        {"target": None, "field": "priority", "value": "high"}],
      "delegations": [{"target": "t2", "field": "deadline"}],
      "sub_tasks": [{"id": "s1", "ask": "close #4", "kind": "close", "targets": ["t1"]},
                    {"id": "s2", "ask": "set a due date", "kind": "update",
                     "targets": ["t2"]}]}
V2_UNNAMED = dict(
    V2, stated_values=[{"target": None, "field": "priority", "value": "high"}],
    delegations=[{"target": None, "field": "deadline"}],
    sub_tasks=[{"id": "s1", "ask": "tidy up", "kind": "other", "targets": []}])


def test_plan_v1_answer_still_valid():
    plan, err = st.validate_plan(plan_obj(), TODAY)
    assert err is None
    assert plan.change_scope == "named" and plan.defaulted == []
    assert (plan.targets, plan.stated_values, plan.delegations, plan.sub_tasks) == (
        [], [], [], [])


def test_plan_v2_answer_is_kept():
    plan, err = st.validate_plan(plan_obj(**dict(V2, change_scope="tidy")), TODAY)
    assert err is None and plan.defaulted == []
    assert plan.change_scope == "tidy"
    assert plan.targets == V2["targets"]
    assert plan.stated_values == V2["stated_values"]
    assert plan.delegations == V2["delegations"]
    assert plan.sub_tasks == V2["sub_tasks"]


@pytest.mark.parametrize("key,bad", [
    ("targets", "t1"),
    ("targets", [{"id": "t1", "task_no": None, "phrase": None}]),       # names nothing
    ("targets", [{"id": "t 1", "task_no": 3, "phrase": None}]),         # id not a code
    ("targets", [{"id": "t1", "task_no": 3, "phrase": None}] * 2),      # duplicate id
    ("targets", [{"id": "t1", "task_no": True, "phrase": None}]),
    ("targets", [{"id": "t1", "task_no": None, "phrase": "x" * 201}]),
    ("targets", [{"id": "t1", "task_no": 3, "phrase": None, "extra": 1}]),
    ("stated_values", [{"target": None, "field": "deadline", "value": "next Friday"}]),
    ("stated_values", [{"target": None, "field": "colour", "value": "red"}]),
    ("stated_values", [{"target": None, "field": "priority", "value": "urgent"}]),
    ("stated_values", [{"target": None, "field": "status", "value": "reopened"}]),
    ("stated_values", [{"target": "t9", "field": "priority", "value": "high"}]),
    ("delegations", [{"target": None, "field": "priority"}]),
    ("sub_tasks", [{"id": "s1", "ask": "x", "kind": "dance", "targets": []}]),
    ("sub_tasks", [{"id": "s1", "ask": "", "kind": "close", "targets": []}]),
    ("sub_tasks", [{"id": "s1", "ask": "x", "kind": "close", "targets": ["t9"]}]),
])
def test_plan_v2_malformed_key_defaulted(key, bad):
    # A bad `targets` would also default every key naming t1/t2 (pinned below): here the
    # others name no target, so each case shows a bad key costing only itself.
    base = V2 if key != "targets" else V2_UNNAMED
    plan, err = st.validate_plan(plan_obj(**dict(base, **{key: bad})), TODAY)
    assert err is None
    assert getattr(plan, key) == [] and plan.defaulted == [key]
    for other in ("targets", "stated_values", "delegations", "sub_tasks"):
        if other != key:
            assert getattr(plan, other) == base[other], other


def test_plan_v2_target_refs_follow_a_defaulted_targets_key():
    """stated_values / delegations / sub_tasks naming a target that is no longer there are
    defaulted too: a reference to a dropped target is a reference to nothing."""
    plan, err = st.validate_plan(plan_obj(**dict(V2, targets="t1")), TODAY)
    assert err is None
    assert plan.defaulted == ["targets", "stated_values", "delegations", "sub_tasks"]


@pytest.mark.parametrize("scope", [None, "", "everything", 3])
def test_plan_invalid_scope_is_named(scope):
    plan, err = st.validate_plan(plan_obj(**dict(V2, change_scope=scope)), TODAY)
    assert err is None and plan.change_scope == "named"
    assert plan.defaulted == ["change_scope"]


def test_plan_v2_caps():
    many = [{"id": f"t{i}", "task_no": i, "phrase": None} for i in range(1, 12)]
    plan, err = st.validate_plan(plan_obj(targets=many), TODAY)
    assert err is None and plan.targets == [] and plan.defaulted == ["targets"]
    ten = many[:10]
    plan, _ = st.validate_plan(plan_obj(targets=ten), TODAY)
    assert len(plan.targets) == 10 and plan.defaulted == []
    subs = [{"id": f"s{i}", "ask": "x", "kind": "other", "targets": []} for i in range(9)]
    plan, _ = st.validate_plan(plan_obj(sub_tasks=subs), TODAY)
    assert plan.sub_tasks == [] and plan.defaulted == ["sub_tasks"]
    sv = [{"target": None, "field": "priority", "value": "high"}] * 11
    plan, _ = st.validate_plan(plan_obj(stated_values=sv), TODAY)
    assert plan.stated_values == [] and plan.defaulted == ["stated_values"]


def test_plan_unknown_extra_key_rejected():
    plan, err = st.validate_plan(plan_obj(**V2, extra=1), TODAY)
    assert plan is None and err == "keys"
    core = plan_obj(**V2)
    del core["reading"]
    assert st.validate_plan(core, TODAY) == (None, "keys")


def test_plan_v2_trace_is_counts_shaped():
    model = Model(plan=[plan_obj(**dict(V2, delegations="bad"))])
    payload = st.run(make_ctx(), deps(model=model))
    trace = payload["run_trace"]
    assert trace["plan_scope"] == "named"
    assert trace["plan_key_defaulted"] == ["delegations"]
    assert trace["plan_targets"] == 2 and trace["plan_sub_tasks"] == 2
    # Counts only, so Task 4.6 can tell a planner miss from a writer or gate miss (R3).
    assert trace["plan_stated_values"] == 2 and trace["plan_delegations"] == 0
    assert action_pack_view.fail_trace_error(trace) is None


def test_plan_prompt_states_the_v2_keys():
    model = Model()
    st.run(make_ctx(), deps(model=model))
    prompt = next(c["prompt"] for c in model.calls if c["caller"] == "action_plan")
    for key in ("change_scope", "targets", "stated_values", "delegations", "sub_tasks"):
        assert f'"{key}"' in prompt, key


def test_plan_prompt_a_quoted_item_value_is_not_stated():
    # v2_r1 U3: "#56 says '21st'. Make that a real date." was stored as a stated deadline
    # resolved against today (2026-11-21); the date belongs to the records (2026-09-21).
    model = Model()
    st.run(make_ctx(), deps(model=model))
    prompt = next(c["prompt"] for c in model.calls if c["caller"] == "action_plan")
    assert "values the user gave in their own words" in prompt
    assert "repeats from an item or a record and asks to be worked out" in prompt
    assert "is not a stated value: leave it out" in prompt


# ---- PR-3 Task 3.3: target lookups in round 1 (spec 2026-11-05 §3.2.2, §11) --------------

SID_DONE = "44444444-5555-4666-8777-888888888883"
TARGETS2 = [{"id": "t1", "task_no": 2, "phrase": None},
            {"id": "t2", "task_no": None, "phrase": "level 3 windows"}]


class TargetTools(Tools):
    """Tools whose target hops (call_id target_*) return one row per target: the done item
    for the phrase, and a delay to show the hops overlap."""

    def __init__(self, delay=0.0, **kw):
        super().__init__(**kw)
        self.delay, self.target_calls, self.threads = delay, [], set()

    def items_run(self, args, tctx, call_id="items"):
        import threading
        import time
        if not call_id.startswith("target_"):
            return super().items_run(args, tctx, call_id)
        self.target_calls.append((call_id, args, tctx))
        self.threads.add(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        row = {f: None for f in ask_items.ITEM_FIELDS}
        if args.get("task_no") == 2:
            row.update(stable_id=SID2, task_no=2, text="Item 2", status="open")
        else:
            row.update(stable_id=SID_DONE, task_no=3, text="Seal the level 3 windows",
                       status="done")
        return {"tool": "list_items", "id": call_id, "meta": {}, "items": [
            {"ref": tctx.refs.assign("item", row["stable_id"], "R"), "kind": "item",
             "provenance": "records", **row}]}


def test_round1_issues_target_calls_all_statuses():
    tools = TargetTools()
    model = Model(plan=[plan_obj(targets=TARGETS2)])
    payload = st.run(make_ctx(), deps(model=model, tools=tools))
    calls = {cid: args for cid, args, _ in tools.target_calls}
    assert set(calls) == {"target_t1", "target_t2"}
    for args in calls.values():
        assert args["status"] == ["open", "in_progress", "blocked", "done", "closed"]
        assert args["limit"] == 50 and args["site_id"] == SITE
    assert calls["target_t1"]["task_no"] == 2 and "query" not in calls["target_t1"]
    assert calls["target_t2"]["query"] == "level 3 windows" and "task_no" not in calls[
        "target_t2"]
    trace = payload["run_trace"]
    assert trace["target_resolution"] == {"t1": SID2, "t2": SID_DONE}
    assert trace["target_calls"] == 2
    # The hops' items entered the registry with refs from the run's shared assigner: SID2
    # keeps the ref round 1's items call gave it, the done item gets the next one.
    run_refs = {s["stable_id"]: s["ref"] for s in payload["sources"] if s.get("stable_id")}
    assert all(r.startswith("R") for r in run_refs.values())


def test_target_refs_are_reassigned_on_the_shared_assigner():
    tools = TargetTools()
    ctx = make_ctx()
    run = st.Run(ctx=ctx, deps=deps(model=Model(), tools=tools))
    run.plan, _ = st.validate_plan(plan_obj(targets=TARGETS2), TODAY)
    st.gather_stage(run)
    by_sid = {e["item"]["stable_id"]: ref for ref, e in run.registry.items()
              if e["kind"] == "item"}
    assert by_sid[SID2] == run.refs.assign("item", SID2, "R")          # same as items1
    assert by_sid[SID_DONE] == run.refs.assign("item", SID_DONE, "R")
    assert len(set(run.registry)) == len(run.registry)
    # each hop had a private assigner and its own deadline
    for _, _, tctx in tools.target_calls:
        assert tctx.refs is not run.refs
        assert tctx.deadline == pytest.approx(ctx.clock() + ask_items.MAX_DEADLINE_S)
    assert run.resolution["t2"].stable_id == SID_DONE


def test_target_calls_run_concurrently():
    import time
    tools = TargetTools(delay=0.2)
    targets = [{"id": f"t{i}", "task_no": None, "phrase": f"windows {i}"}
               for i in range(1, 5)]
    run = st.Run(ctx=make_ctx(), deps=deps(model=Model(), tools=tools))
    run.plan, _ = st.validate_plan(plan_obj(targets=targets), TODAY)
    t0 = time.perf_counter()
    st.gather_stage(run)
    assert time.perf_counter() - t0 < 0.5
    assert len(tools.target_calls) == 4 and len(tools.threads) == 4


def test_at_most_four_target_lookups():
    tools = TargetTools()
    targets = [{"id": f"t{i}", "task_no": i, "phrase": None} for i in range(1, 7)]
    run = st.Run(ctx=make_ctx(), deps=deps(model=Model(), tools=tools))
    run.plan, _ = st.validate_plan(plan_obj(targets=targets), TODAY)
    st.gather_stage(run)
    assert len(tools.target_calls) == 4
    assert run.trace["target_lookups_capped"] == 2
    # t5/t6 still resolve over what the run read (task_no 1 and 2 are round 1's items).
    assert set(run.trace["target_resolution"]) == {f"t{i}" for i in range(1, 7)}


def test_target_hop_skipped_without_time():
    clock = Clock()
    ctx = make_ctx(clock=clock)
    tools = TargetTools()
    run = st.Run(ctx=ctx, deps=deps(model=Model(), tools=tools))
    run.plan, _ = st.validate_plan(plan_obj(targets=TARGETS2), TODAY)
    # 4 s of hop budget left: under MIN_TARGET_HOP_S.
    clock.t = ctx.deadline_for("gather") - st.ROUND_FLOOR_S - 4.0
    st.gather_stage(run)
    assert tools.target_calls == []
    assert run.trace["target_resolve_time"] == 2
    # t1 (task_no 2) is among round 1's open items, but a skipped lookup is `none`.
    assert run.trace["target_resolution"] == {"t1": "none", "t2": "none"}


def test_target_hop_budget_is_capped_by_gather_time():
    clock = Clock()
    ctx = make_ctx(clock=clock)
    tools = TargetTools()
    run = st.Run(ctx=ctx, deps=deps(model=Model(), tools=tools))
    run.plan, _ = st.validate_plan(plan_obj(targets=TARGETS2[:1]), TODAY)
    clock.t = ctx.deadline_for("gather") - st.ROUND_FLOOR_S - 12.0
    st.gather_stage(run)
    [(_, _, tctx)] = tools.target_calls
    assert tctx.deadline == pytest.approx(clock.t + 12.0)


def test_no_targets_no_hops_and_an_empty_resolution():
    tools = TargetTools()
    payload = st.run(make_ctx(), deps(model=Model(plan=[plan_obj()]), tools=tools))
    assert tools.target_calls == []
    assert payload["run_trace"]["target_resolution"] == {}
    assert payload["run_trace"]["plan_scope"] == "named"


def test_target_resolution_in_trace_is_counts_shaped():
    tools = TargetTools()
    targets = TARGETS2 + [{"id": "t3", "task_no": 404, "phrase": None},
                          {"id": "t4", "task_no": None, "phrase": "item"}]
    model = Model(plan=[plan_obj(targets=targets)])
    payload = st.run(make_ctx(), deps(model=model, tools=tools))
    trace = payload["run_trace"]
    assert trace["target_resolution"]["t3"] == "none"
    assert action_pack_view.fail_trace_error(trace) is None
    assert action_pack_view.fail_trace_error(
        {"target_resolution": trace["target_resolution"]}) is None


def test_a_failing_target_hop_does_not_fail_round_1():
    class Boom(TargetTools):
        def items_run(self, args, tctx, call_id="items"):
            if call_id.startswith("target_"):
                raise RuntimeError("boom")
            return super().items_run(args, tctx, call_id)
    payload = st.run(make_ctx(), deps(model=Model(plan=[plan_obj(targets=TARGETS2)]),
                                      tools=Boom()))
    trace = payload["run_trace"]
    assert trace["tool_errors"] >= 2
    assert trace["target_resolution"]["t1"] == SID2       # round 1's own items


# ---- PR-3 Task 3.4: the named targets in the write render --------------------------------

def _write_prompt(model):
    return next(c["prompt"] for c in model.calls if c["caller"] == "action_write")


def test_write_prompt_lists_named_targets():
    class Pool(Tools):
        """Round 1's items plus a done item and two scaffold look-alikes."""

        def items_run(self, args, tctx, call_id="items"):
            env = super().items_run(args, tctx, call_id)
            extra = [("44444444-5555-4666-8777-88888888888a", 7, "Seal the level 3 windows",
                      "done"),
                     ("44444444-5555-4666-8777-88888888888b", 8, "Check north scaffold",
                      "open"),
                     ("44444444-5555-4666-8777-88888888888c", 9,
                      "Fix north scaffold tie", "blocked")]
            for sid, no, text, status in extra:
                row = {f: None for f in ask_items.ITEM_FIELDS}
                row.update(stable_id=sid, task_no=no, text=text, status=status)
                env["items"].append({"ref": tctx.refs.assign("item", sid, "R"),
                                     "kind": "item", "provenance": "records", **row})
            return env

    targets = [{"id": "t1", "task_no": 2, "phrase": None},
               {"id": "t2", "task_no": None, "phrase": "north scaffold"},
               {"id": "t3", "task_no": None, "phrase": "seal level 3 windows"},
               {"id": "t4", "task_no": 404, "phrase": None}]
    model = Model(plan=[plan_obj(targets=targets)])
    ctx = make_ctx()
    st.run(ctx, deps(model=model, tools=Pool()))
    prompt = _write_prompt(model)
    refs = {}
    for line in prompt.splitlines():
        if " | item | stable_id " in line:
            ref, _, sid = line.split(" | ")[:3]
            refs[sid.split()[-1]] = ref
    block = ("Named targets (the items the instruction names, matched in code):\n"
             f"t1: {refs[SID2]} (open)\n"
             f"t2: AMBIGUOUS — {refs['44444444-5555-4666-8777-88888888888b']}, "
             f"{refs['44444444-5555-4666-8777-88888888888c']}: write a note, change neither\n"
             f"t3: {refs['44444444-5555-4666-8777-88888888888a']} (done) — already done\n"
             "t4: NOT FOUND — write a note\n")
    assert block in prompt
    assert "north scaffold" not in prompt.split("Named targets")[1].split("Instruction")[0]


def test_write_prompt_without_targets_is_unchanged():
    model = Model()
    st.run(make_ctx(), deps(model=model))
    prompt = _write_prompt(model)
    assert "Named targets" not in prompt
    assert "Write in this language: en\n\nInstruction from the user" in prompt


# ---- PR-3 Task 3.5: ACTION_MAX_ROUNDS --------------------------------------------------

@pytest.mark.parametrize("knob,model_rounds", [("2", 1), ("1", 0), ("4", 3), ("99", 7)])
def test_model_rounds_honours_knob(monkeypatch, knob, model_rounds):
    monkeypatch.setenv("ACTION_MAX_ROUNDS", knob)
    # Every round asks for a fresh call and is never done, so only the cap stops it.
    rounds = [{"calls": [{"tool": "search_records", "args": {"query": f"q{i}"}}],
               "done": False} for i in range(20)]
    model = Model(rounds=rounds)
    payload = st.run(make_ctx(), deps(model=model))
    made = [c for c in model.calls if c["caller"] == "action_round"]
    assert len(made) == model_rounds
    assert payload["run_trace"]["rounds"] == model_rounds + 1
    for i, call in enumerate(made):                 # the prompt counts down to the knob
        assert f"{model_rounds - i - 1} rounds remain after this one" in call["prompt"]


# ---- PR-4 Task 4.1: writer keys target_ref and field_sources (spec 2026-11-05 §3.2.3) ----

def _vrun(items=None, targets=None):
    run = st.Run(ctx=make_ctx(), deps=deps())
    run.items = items or {SID1: {"stable_id": SID1, "status": "open"}}
    run.plan, _ = st.validate_plan(
        plan_obj(targets=targets or [{"id": "t1", "task_no": 1}]), TODAY)
    return run


def test_validate_draft_keeps_field_sources():
    run = _vrun()
    obj = {"conclusion": [], "actions": [
        {"type": "update_item", "target_stable_id": SID1, "target_ref": "t1",
         "proposed": {"deadline": "2026-11-10", "priority": "high"},
         "field_sources": {
             "deadline": {"source": "evidence", "refs": ["R2", "R99"],
                          "said_on": "2026-11-01"},
             "priority": {"source": "user", "refs": []},
             "status": {"source": "user", "refs": []}},          # not proposed: ignored
         "rationale": "r", "refs": ["R1"]},
        {"type": "create_item", "target_ref": "t9",              # unknown target id
         "proposed": {"text": "Book the pump"}, "rationale": "r", "refs": ["R1"]}]}
    draft = st.validate_draft(obj, run, {"R1", "R2"})
    upd, create = draft["actions"]
    assert upd["target_ref"] == "t1"
    assert upd["field_sources"] == {
        "deadline": {"source": "evidence", "refs": ["R2"], "said_on": "2026-11-01"},
        "priority": {"source": "user", "refs": [], "said_on": None}}
    assert upd["refs"] == ["R1"]          # a field's refs join only if it passes the gate
    assert create["target_ref"] is None and create["field_sources"] is None


def test_field_sources_bad_source_is_none():
    proposed = {"deadline": "2026-11-10", "priority": "high", "responsible": "Sam",
                "status": "blocked"}
    raw = {"deadline": {"source": "guess", "refs": ["R1"], "said_on": None},
           "priority": "high",
           "responsible": {"source": "evidence", "refs": "R1"},
           "status": {"source": "evidence", "refs": ["R1"], "said_on": "yesterday"}}
    out = st._field_sources(raw, proposed, citable={"R1"})
    none = {"source": None, "refs": [], "said_on": None}
    assert out["deadline"] == none and out["priority"] == none
    assert out["responsible"] == none
    assert out["status"] == {"source": "evidence", "refs": ["R1"], "said_on": None}
    assert st._field_sources(None, proposed, citable={"R1"}) is None
    assert st._field_sources("x", proposed, citable={"R1"}) is None


def test_open_statuses_match_the_verifier():
    import action_pack_verify
    assert st.OPEN_STATUSES == action_pack_verify.OPEN_STATUSES



# ---- PR-4 Task 4.5: the scope gate in the run; the write budget (spec 2026-11-05 §3.2.4, §11)

class NamedTools(Tools):
    """Round 1's items (SID1 #1, SID2 #2) with per-item field overrides, on every read
    (round 1 and the target hops alike)."""

    def __init__(self, over=None, **kw):
        super().__init__(**kw)
        self.over = over or {}

    def items_run(self, args, tctx, call_id="items"):
        env = super().items_run(args, tctx, call_id)
        for it in env["items"]:
            it.update(self.over.get(it["stable_id"], {}))
        return env


NAMED_T1 = [{"id": "t1", "task_no": 1, "phrase": None}]


def test_write_stage_applies_scope_gate():
    write = write_obj(actions=[
        dict(write_obj()["actions"][0], target_ref="t1"),                # named: kept
        {"type": "update_item", "target_stable_id": SID2, "proposed": {"priority": "high"},
         "field_sources": {"priority": src("R1")}, "rationale": "r", "refs": ["R1"]}])
    verify = Verify()
    model = Model(plan=[plan_obj(targets=NAMED_T1)], write=[write])
    payload = st.run(make_ctx(), deps(model=model, verify=verify))
    trace = payload["run_trace"]
    assert trace["target_resolution"] == {"t1": SID1}
    assert [(a["type"], a["target_stable_id"]) for a in verify.calls[0]["draft"]["actions"]] \
        == [("close_item", SID1)]
    assert trace["scope_gate"] == {"drop_unasked_target": 1}
    assert [a["target_stable_id"] for a in payload["actions"]] == [SID1]


def test_a_closed_item_named_by_number_becomes_an_already_done_note():
    # Review Focus 5, end to end: "#1" is already done. The writer's close must reach the
    # gate (validate_draft no longer drops it as a transition) and become a note.
    write = write_obj(actions=[dict(write_obj()["actions"][0], target_ref="t1",
                                    proposed={"status": "closed"})])
    verify = Verify()
    model = Model(plan=[plan_obj(targets=NAMED_T1)], write=[write])
    payload = st.run(make_ctx(), deps(model=model, verify=verify,
                                      tools=NamedTools({SID1: {"status": "done"}})))
    [note] = verify.calls[0]["draft"]["actions"]
    assert note["type"] == "note" and note["target_stable_id"] is None
    assert note["proposed"] == {"text": "Records say done. (already done)"}
    assert note["refs"] == ["R1", "R2"]
    assert payload["run_trace"]["scope_gate"] == {"demoted_target_closed": 1}
    assert "write_dropped" not in payload["run_trace"]
    assert payload["actions"][0]["type"] == "note"


def test_validate_draft_leaves_a_closed_target_to_the_gate():
    # Review Focus 5: a close on a done item must reach the scope gate (which makes the
    # named one an "already done" note); legal_change would drop it here first.
    run = _vrun(items={SID1: {"stable_id": SID1, "status": "done"},
                       SID2: {"stable_id": SID2, "status": "open"}})
    obj = {"conclusion": [], "actions": [
        {"type": "close_item", "target_stable_id": SID1, "target_ref": "t1",
         "proposed": {"status": "closed"}, "rationale": "r", "refs": ["R1"]},
        {"type": "update_item", "target_stable_id": SID2, "proposed": {"status": "open"},
         "rationale": "r", "refs": ["R1"]}]}
    draft = st.validate_draft(obj, run, {"R1"})
    assert [(a["type"], a["target_stable_id"]) for a in draft["actions"]] == [
        ("close_item", SID1)]
    assert run.trace["write_dropped"] == {"update_item__transitions": 1}


def test_r3_delegated_deadline_is_the_raised_on_date():
    # R3: "Nobody's said when #1 is due. Just pick a date." -> the item's raised_on,
    # whatever the writer proposed, marked assumed.
    plan = plan_obj(targets=NAMED_T1, delegations=[{"target": "t1", "field": "deadline"}])
    write = write_obj(actions=[
        {"type": "update_item", "target_stable_id": SID1, "target_ref": "t1",
         "proposed": {"deadline": "2026-11-08"},
         "field_sources": {"deadline": src(source="assumed_default")},
         "rationale": "No due date was set; the user asked for one.", "refs": ["R2"]}])
    verify = Verify()
    model = Model(plan=[plan], write=[write])
    tools = NamedTools({SID1: {"deadline": None, "raised_on": "2026-10-09"}})
    payload = st.run(make_ctx(), deps(model=model, verify=verify, tools=tools))
    [a] = verify.calls[0]["draft"]["actions"]
    assert a["proposed"] == {"deadline": "2026-10-09"} and a["assumed"] == ["deadline"]
    assert payload["actions"][0]["proposed"] == {"deadline": "2026-10-09"}
    assert payload["run_trace"]["scope_gate"] == {"date_assumed": 1}
    prompt = _write_prompt(model)
    assert ("Delegated missing values (only for an item with neither a deadline nor a "
            "deadline_text; source assumed_default):\nt1: deadline") in prompt


def test_write_prompt_states_scope_and_field_sources():
    model = Model()
    st.run(make_ctx(), deps(model=model))
    prompt = _write_prompt(model)
    assert ("- Change only items the instruction names, unless it asks you to tidy open "
            "items. For anything else worth changing, write a note.") in prompt
    for key in ('"target_ref"', '"field_sources"', '"assumed_default"', '"said_on"'):
        assert key in prompt, key
    assert "Delegated missing values" not in prompt


def test_write_length_stop_counted_and_repaired():
    # Review Focus 3: the larger answer stops on the token limit -> counted, repaired, built.
    model = Model(write=['{"conclusion": [{"text": "The pour was', write_obj()],
                  finish={"action_write": ["length", "stop"]})
    payload = st.run(make_ctx(), deps(model=model))
    trace = payload["run_trace"]
    assert trace["write_finish_length"] == 1 and trace["write_repairs"] == 1
    assert [a["type"] for a in payload["actions"]] == ["close_item"]
    assert st.WRITE_MAX_TOKENS == 12000


def test_a_length_stop_outside_the_writer_is_not_counted():
    model = Model(finish={"action_plan": ["length"], "action_round": ["length"]})
    payload = st.run(make_ctx(), deps(model=model))
    assert "write_finish_length" not in payload["run_trace"]


def test_judge_call_reports_the_finish_reason(monkeypatch):
    import llm_tools_client as tc
    monkeypatch.setattr(tc, "complete_json", lambda prompt, **kw: tc.ChatTurn(
        text="{", model="m", requested_model="m", finish_reason="length"))
    monkeypatch.setattr(tc, "_now", lambda: 1000.0)
    info = {}
    st.judge_call("p", max_tokens=10, model=WRITER, deadline=100.0, caller="action_write",
                  info=info)
    assert info["finish_reason"] == "length"


def test_scope_gate_trace_counts_shaped():
    stated = [{"target": "t1", "field": "deadline", "value": "2026-11-14"}]
    plan = plan_obj(targets=NAMED_T1 + [{"id": "t2", "task_no": 404, "phrase": None}],
                    stated_values=stated)
    write = write_obj(actions=[
        {"type": "update_item", "target_stable_id": SID1, "target_ref": "t1",
         "proposed": {"deadline": "2026-11-13", "priority": "high"},
         "field_sources": {"deadline": src(source="user"), "priority": src("R2")},
         "rationale": "r", "refs": ["R1"]},
        {"type": "update_item", "target_stable_id": SID2, "target_ref": "t2",
         "proposed": {"status": "blocked"}, "field_sources": {"status": src("R1")},
         "rationale": "r", "refs": ["R1"]}])
    verify = Verify()
    payload = st.run(make_ctx(), deps(model=Model(plan=[plan], write=[write]),
                                      verify=verify))
    trace = payload["run_trace"]
    assert trace["scope_gate"] == {"user_value_overridden__deadline": 1,
                                   "field_unsourced__priority": 1,
                                   "demoted_target_none": 1}
    assert action_pack_view.fail_trace_error({"scope_gate": trace["scope_gate"]}) is None
    assert action_pack_view.fail_trace_error(trace) is None
    assert [a["proposed"] for a in verify.calls[0]["draft"]["actions"]] == [
        {"deadline": "2026-11-14"}, {"text": "r (named item not found)"}]
    # A run the gate leaves alone still records it, empty.
    assert st.run(make_ctx(), deps())["run_trace"]["scope_gate"] == {}


# ---- PR-4 review fixes --------------------------------------------------------------------

def test_a_non_string_target_ref_is_absent_and_counted():
    run = _vrun()
    obj = {"conclusion": [], "actions": [
        {"type": "close_item", "target_stable_id": SID1, "target_ref": bad,
         "proposed": {"status": "done"}, "rationale": "r", "refs": ["R1"]}
        for bad in (["t1"], {"id": "t1"}, 1)]}
    draft = st.validate_draft(obj, run, {"R1"})
    assert [a["target_ref"] for a in draft["actions"]] == [None, None, None]
    assert run.trace["write_dropped"] == {"close_item__target_ref": 3}


def test_write_prompt_shows_stated_values():
    stated = [{"target": "t1", "field": "status", "value": "done"},
              {"target": None, "field": "priority", "value": "high"}]
    model = Model(plan=[plan_obj(targets=NAMED_T1, stated_values=stated)])
    st.run(make_ctx(), deps(model=model))
    prompt = _write_prompt(model)
    assert ("Values the user stated (source user; the code applies these values):\n"
            "t1: status = done\nany item you change: priority = high") in prompt


def test_named_close_with_user_source_and_no_stated_value_is_kept():
    # "Close #1": the planner recorded no stated status; the writer says source user and
    # cites the record that says it is finished. The close must still be shown.
    write = write_obj(actions=[
        {"type": "close_item", "target_stable_id": SID1, "target_ref": "t1",
         "proposed": {"status": "done"},
         "field_sources": {"status": src(source="user")},
         "rationale": "The record says it is finished.", "refs": ["R1", "R2"]}])
    verify = Verify()
    model = Model(plan=[plan_obj(targets=NAMED_T1)], write=[write])
    payload = st.run(make_ctx(), deps(model=model, verify=verify))
    assert [(a["type"], a["target_stable_id"]) for a in payload["actions"]] == [
        ("close_item", SID1)]
    assert payload["run_trace"]["scope_gate"] == {"user_value_unstated__status": 1}


# ---- PR-6: notes survive the schema; typed write_dropped (spec 2026-11-05 §5.2) ----------

def _note(text, **extra):
    return {"type": "note", "proposed": {"text": text}, "rationale": "r", "refs": ["D1"],
            **extra}


def test_long_note_kept():
    text = "Clause 4.2 requires " + "x" * 2980
    write = {"conclusion": [], "actions": [_note(text)]}
    verify = Verify()
    payload = st.run(make_ctx(), deps(model=Model(write=[write]), verify=verify))
    [note] = verify.calls[0]["draft"]["actions"]
    assert note["type"] == "note" and note["proposed"] == {"text": text}
    assert action_pack_view.payload_shape_error(payload) is None
    assert "write_dropped" not in payload["run_trace"]


def test_stray_key_removed_and_counted():
    run = _vrun()
    long = "y" * 3000
    obj = {"conclusion": [], "actions": [
        {"type": "note", "proposed": {"text": long, "clause": "4.2"}, "rationale": "r",
         "refs": ["R1"], "confidence": "supported"},
        {"type": "update_item", "target_stable_id": SID1, "target_ref": "t1",
         "proposed": {"deadline": "2026-11-10", "note": "per R1"}, "rationale": "r",
         "refs": ["R1"]}]}
    draft = st.validate_draft(obj, run, {"R1"})
    note, update = draft["actions"]
    assert note["proposed"] == {"text": long} and "confidence" not in note
    assert update["proposed"] == {"deadline": "2026-11-10"}
    assert run.trace["write_dropped"] == {"extra_key__note": 2, "extra_key__update_item": 1}


def test_note_over_4000_dropped():
    run = _vrun()
    obj = {"conclusion": [], "actions": [
        {**_note("z" * (st.BODY_MAX_CHARS + 1)), "refs": ["R1"]},
        {**_note("z" * st.BODY_MAX_CHARS), "refs": ["R1"]}]}
    draft = st.validate_draft(obj, run, {"R1"})
    assert [len(a["proposed"]["text"]) for a in draft["actions"]] == [st.BODY_MAX_CHARS]
    assert run.trace["write_dropped"] == {"note__text_too_long": 1}


def test_create_item_text_keeps_the_apply_cap():
    # A create is applied, so its text keeps action_pack_view's cap; only notes grow.
    run = _vrun()
    obj = {"conclusion": [], "actions": [
        {"type": "create_item", "proposed": {"text": "t" * 2001}, "rationale": "r",
         "refs": ["R1"]}]}
    assert st.validate_draft(obj, run, {"R1"})["actions"] == []
    assert run.trace["write_dropped"] == {"create_item__text_too_long": 1}


def test_write_dropped_keys_carry_type():
    run = _vrun()
    obj = {"conclusion": [{"text": "", "refs": [], "confidence": "supported"}],
           "actions": [
        "not an object",
        {"type": "merge_items", "proposed": {}, "rationale": "r", "refs": ["R1"]},
        {"type": "note", "proposed": {"text": "n"}, "rationale": "", "refs": ["R1"]},
        {"type": "draft_rfi", "proposed": {"subject": "s"}, "rationale": "r",
         "refs": ["R1"]},
        {"type": "close_item", "target_stable_id": None, "proposed": {},
         "rationale": "r", "refs": ["R1"]},
        {"type": "draft_email", "proposed": {"subject": "s", "body": "b"},
         "rationale": "r", "refs": ["R9"]}]}
    st.validate_draft(obj, run, {"R1"})
    assert run.trace["write_dropped"] == {
        "conclusion__text": 1, "unknown__schema": 1, "unknown__type": 1,
        "note__rationale": 1, "draft_rfi__proposed": 1, "close_item__targets": 1,
        "draft_email__refs": 1, "draft_email__no_refs": 1}


def test_write_dropped_cap_is_typed():
    run = _vrun()
    obj = {"conclusion": [{"text": "c", "refs": ["R1"], "confidence": "supported"}]
           * (st.MAX_CONCLUSION + 1),
           "actions": [_note("n", refs=["R1"])] * (st.MAX_ACTIONS + 2)}
    st.validate_draft(obj, run, {"R1"})
    assert run.trace["write_dropped"] == {"conclusion__cap": 1, "note__cap": 2}


def test_write_dropped_counts_shaped():
    run = _vrun()
    every_type = [{"type": t, "proposed": {"bogus": 1}, "rationale": "r", "refs": ["R9"],
                   "stray": True} for t in st.ACTION_TYPES]
    obj = {"conclusion": [{"text": "c", "refs": ["R9"], "confidence": "supported"}],
           "actions": every_type + [{**_note("w" * 5000), "refs": ["R1"]},
                                    {"type": "x" * 300, "refs": ["R1"]}]}
    st.validate_draft(obj, run, {"R1"})
    wd = run.trace["write_dropped"]
    assert wd["note__text_too_long"] == 1 and wd["unknown__type"] == 1
    assert sum(wd[f"{t}__no_refs"] for t in st.ACTION_TYPES) == len(st.ACTION_TYPES)
    assert all(k.split("__")[0] in st.ACTION_TYPES + ("unknown", "conclusion", "extra_key")
               for k in wd)
    assert action_pack_view.fail_trace_error({"write_dropped": wd}) is None


def test_removed_keys_count_only_on_kept_actions_and_foreign_fields_apart():
    run = _vrun()
    obj = {"conclusion": [{"text": "c", "refs": "R1", "confidence": "uncertain"}],
           "actions": [
        {"type": "update_item", "target_stable_id": SID1, "target_ref": "t1",
         "proposed": {"text": "reword", "deadline": "2026-11-20", "junk": 1},
         "rationale": "r", "refs": ["R1"]},
        {**_note("n", stray=1, refs=["R9"])}]}           # dropped: its stray key is not counted
    draft = st.validate_draft(obj, run, {"R1"})
    assert [a["proposed"] for a in draft["actions"]] == [{"deadline": "2026-11-20"}]
    assert run.trace["write_dropped"] == {
        "conclusion__schema": 1, "update_item__foreign_field": 1,
        "extra_key__update_item": 1, "note__refs": 1, "note__no_refs": 1}


def test_write_prompt_has_standards_note_rule():
    p = st.WRITE_PROMPT
    assert "the answer is a note action" in p
    assert ("Use create_item for it only when the instruction asks for a task or the "
            "evidence shows work still to do") in p
    assert ("use draft_rfi only when the instruction asks for an RFI or the answer needs a "
            "party outside the team") in p
    # It is general wording, and the prompt still formats.
    st.WRITE_PROMPT.format(site="s", today="t", as_of="a", reading="r", language="en",
                           targets="", instruction="i", evidence="e")


# ---- PR-5 Task 5.2: the writer's sub_tasks and their accounting (spec 2026-11-05 §4.2) ---

PLAN_SUBS = [{"id": "s1", "ask": "Close #1", "kind": "close", "targets": ["t1"]},
             {"id": "s2", "ask": "Email the client about the pour", "kind": "draft",
              "targets": []}]


def _sub(sid, status, actions=(), conclusion=(), reason_code=None, reason=None):
    return {"id": sid, "status": status, "actions": list(actions),
            "conclusion": list(conclusion), "reason_code": reason_code, "reason": reason}


def _stored(sid, ask, kind, status, actions=(), conclusion=(), reason_code=None,
            reason=None):
    return {"id": sid, "ask": ask, "kind": kind, "status": status,
            "actions": list(actions), "conclusion": list(conclusion),
            "reason_code": reason_code, "reason": reason}


class DropVerify(Verify):
    """Labels every element but leaves out the action indices in `drop`."""

    def __init__(self, drop=()):
        super().__init__()
        self.drop = set(drop)

    def __call__(self, draft, **kw):
        out = super().__call__(draft, **kw)
        out["actions"] = [a for a in out["actions"] if a["index"] not in self.drop]
        return out


def _close1(**over):
    return dict({"type": "close_item", "target_stable_id": SID1, "target_ref": "t1",
                 "proposed": {"status": "done"}, "field_sources": {"status": src("R1")},
                 "rationale": "Records say done.", "refs": ["R1", "R2"]}, **over)


def _email():
    return {"type": "draft_email", "proposed": {"subject": "Pour", "body": "Hi"},
            "rationale": "Tell the client.", "refs": ["R1"]}


def test_missing_subtask_not_answered():
    write = write_obj(actions=[_close1()],
                      sub_tasks=[_sub("s1", "done", actions=[0], conclusion=[0]),
                                 _sub("s9", "done", actions=[0])])      # unknown id
    model = Model(plan=[plan_obj(targets=NAMED_T1, sub_tasks=PLAN_SUBS)], write=[write])
    payload = st.run(make_ctx(), deps(model=model))
    assert payload["sub_tasks"] == [
        _stored("s1", "Close #1", "close", "done", actions=[1], conclusion=[0]),
        _stored("s2", "Email the client about the pour", "draft", "not_done",
                reason_code="not_answered")]
    assert payload["run_trace"]["subtask_unknown"] == 1
    assert action_pack_view.payload_shape_error(payload) is None
    assert action_pack_view.fail_trace_error(payload["run_trace"]) is None


def test_done_subtask_all_dropped_becomes_not_done():
    # s1's only action is an unasked update (the scope gate drops it); s2's email has no
    # refs (validate_draft drops it). Both said done/partly: both become not_done.
    write = write_obj(
        actions=[{"type": "update_item", "target_stable_id": SID2,
                  "proposed": {"priority": "high"}, "field_sources": {"priority": src("R1")},
                  "rationale": "r", "refs": ["R1"]},
                 dict(_email(), refs=[])],
        sub_tasks=[_sub("s1", "done", actions=[0]),
                   _sub("s2", "partly", actions=[1], conclusion=[0])])
    model = Model(plan=[plan_obj(targets=NAMED_T1, sub_tasks=PLAN_SUBS)], write=[write])
    payload = st.run(make_ctx(), deps(model=model))
    assert payload["actions"] == []
    assert [(s["id"], s["status"], s["reason_code"], s["actions"], s["conclusion"])
            for s in payload["sub_tasks"]] == [
        ("s1", "not_done", "dropped_by_checks", [], []),
        ("s2", "not_done", "dropped_by_checks", [], [0])]


def test_done_subtask_dropped_by_the_verifier_becomes_not_done():
    write = write_obj(actions=[_close1(), _email()],
                      sub_tasks=[_sub("s1", "done", actions=[0]),
                                 _sub("s2", "done", actions=[1])])
    model = Model(plan=[plan_obj(targets=NAMED_T1, sub_tasks=PLAN_SUBS)], write=[write])
    payload = st.run(make_ctx(), deps(model=model, verify=DropVerify(drop={0})))
    assert [a["type"] for a in payload["actions"]] == ["draft_email"]
    assert [(s["status"], s["reason_code"], s["actions"]) for s in payload["sub_tasks"]] == [
        ("not_done", "dropped_by_checks", []), ("done", None, [1])]


def test_plan_without_subtasks_stores_null():
    write = write_obj(sub_tasks=[_sub("s1", "done", actions=[0])])
    payload = st.run(make_ctx(), deps(model=Model(write=[write])))
    assert "sub_tasks" not in payload
    assert action_pack_view.payload_shape_error(payload) is None
    # ... and so does a plan with sub-tasks whose writer sent none: every one not_answered.
    model = Model(plan=[plan_obj(targets=NAMED_T1, sub_tasks=PLAN_SUBS)])
    payload = st.run(make_ctx(), deps(model=model))
    assert [(s["status"], s["reason_code"]) for s in payload["sub_tasks"]] == [
        ("not_done", "not_answered"), ("not_done", "not_answered")]


def test_subtask_indices_follow_demotion():
    # #1 is already done: its close becomes an "already done" note and keeps its place
    # among the survivors; the unasked update before it is dropped.
    write = write_obj(
        actions=[_email(),
                 {"type": "update_item", "target_stable_id": SID2,
                  "proposed": {"priority": "high"}, "field_sources": {"priority": src("R1")},
                  "rationale": "r", "refs": ["R1"]},
                 _close1(proposed={"status": "closed"})],
        sub_tasks=[_sub("s1", "done", actions=[2]),
                   _sub("s2", "partly", actions=[0, 1], reason_code="other",
                        reason="The client's address is not in the records.")])
    model = Model(plan=[plan_obj(targets=NAMED_T1, sub_tasks=PLAN_SUBS)], write=[write])
    payload = st.run(make_ctx(), deps(model=model,
                                      tools=NamedTools({SID1: {"status": "done"}})))
    assert [(a["seq"], a["type"]) for a in payload["actions"]] == [(1, "draft_email"),
                                                                   (2, "note")]
    assert payload["sub_tasks"] == [
        _stored("s1", "Close #1", "close", "done", actions=[2]),
        _stored("s2", "Email the client about the pour", "draft", "partly", actions=[1],
                reason_code="other", reason="The client's address is not in the records.")]


def test_validate_draft_parses_writer_sub_tasks():
    run = _vrun()
    run.plan.sub_tasks = [{"id": f"s{i}", "ask": "a", "kind": "other", "targets": []}
                          for i in range(1, 8)]
    obj = {"conclusion": [{"text": "", "refs": [], "confidence": "supported"},   # dropped
                          {"text": "c", "refs": ["R1"], "confidence": "supported"}],
           "actions": [_note("bad", refs=[]),                                     # dropped
                       _note("n", refs=["R1"])],
           "sub_tasks": [
               _sub("s1", "done", actions=[1, 1, 7, "0", True], conclusion=[1]),
               _sub("s2", "not_done", reason_code="dropped_by_checks",            # code's
                    reason="x" * 400),
               _sub("s3", "finished", actions=[1]),                               # status
               _sub("s4", "done"),                                                # no index
               _sub("s1", "partly", actions=[1]),                                 # repeat
               _sub("s5", "not_done"),
               _sub("s6", "partly", actions=[9]),                                 # no index
               "not an object", _sub("s99", "done", actions=[1])]}
    draft = st.validate_draft(obj, run, {"R1", "D1"})
    by_id = {e["id"]: e for e in draft["sub_tasks"]}
    assert set(by_id) == {"s1", "s2", "s5"}
    assert (by_id["s1"]["status"], by_id["s1"]["actions"], by_id["s1"]["conclusion"]) == (
        "done", [0], [0])
    assert by_id["s2"]["reason_code"] == "other"
    assert by_id["s2"]["reason"] == "x" * st.SUB_TASK_REASON_MAX_CHARS
    assert by_id["s5"]["reason_code"] == "other" and by_id["s5"]["reason"] is None
    assert run.trace["subtask_unknown"] == 1
    assert run.trace["subtask_malformed"] == 5        # s3, s4, s1 again, s6, a non-object
    assert action_pack_view.fail_trace_error(run.trace) is None


def test_validate_draft_without_sub_tasks_key_is_empty():
    run = _vrun()
    draft = st.validate_draft({"conclusion": [], "actions": []}, run, {"R1"})
    assert draft["sub_tasks"] == []
    draft = st.validate_draft({"conclusion": [], "actions": [], "sub_tasks": "s1"}, run,
                              {"R1"})
    assert draft["sub_tasks"] == [] and run.trace["subtask_malformed"] == 1


def test_account_sub_tasks_rules():
    plan = st.Plan(reading="r", title="t", language="en", site_matches=True,
                   date_from=TODAY, date_to=TODAY, record_queries=[], document_queries=[],
                   web=None, sub_tasks=PLAN_SUBS)
    draft = {"conclusion": [], "actions": [],
             "sub_tasks": [dict(_sub("s1", "done", actions=[0, 1]), listed=True)]}
    out = st.account_sub_tasks(plan, draft, {0: None, 1: 0})
    assert [(s["id"], s["status"], s["actions"], s["reason_code"]) for s in out] == [
        ("s1", "done", [0], None), ("s2", "not_done", [], "not_answered")]
    assert st.account_sub_tasks(dataclasses.replace(plan, sub_tasks=[]), draft, {}) is None


def test_the_verifier_sees_no_sub_tasks():
    write = write_obj(sub_tasks=[_sub("s1", "done", actions=[0])])
    verify = Verify()
    model = Model(plan=[plan_obj(targets=NAMED_T1, sub_tasks=PLAN_SUBS)],
                  write=[dict(write, actions=[_close1()])])
    st.run(make_ctx(), deps(model=model, verify=verify))
    assert set(verify.calls[0]["draft"]) == {"conclusion", "actions"}


def test_write_prompt_lists_sub_tasks_and_the_rule():
    model = Model(plan=[plan_obj(targets=NAMED_T1, sub_tasks=PLAN_SUBS)])
    st.run(make_ctx(), deps(model=model))
    prompt = _write_prompt(model)
    assert ("Sub-tasks (the separate asks in the instruction; answer every one):\n"
            "s1 (close): Close #1 [t1]\n"
            "s2 (draft): Email the client about the pour") in prompt
    p = st.WRITE_PROMPT
    assert "one entry for every listed sub-task" in p
    for code in st.SUB_TASK_REASON_CODES:
        assert code in p
    assert "dropped_by_checks" not in p and "not_answered" not in p
    # Without sub-tasks the block is absent.
    model = Model()
    st.run(make_ctx(), deps(model=model))
    assert "Sub-tasks (" not in _write_prompt(model)


# ---- the verifier's user values (spec 2026-11-05 §7.2, Task 7.2) ----------------------------

def test_run_passes_the_instruction_and_stated_values_to_the_verifier():
    stated = [{"target": "t1", "field": "deadline", "value": "2026-11-13"},
              {"target": "t2", "field": "priority", "value": "high"},
              {"target": None, "field": "responsible", "value": "Mike"}]
    verify = Verify()
    model = Model(plan=[plan_obj(targets=TARGETS2, stated_values=stated)])
    st.run(make_ctx(instruction="Push task 2 to the 13th"),
           deps(model=model, tools=TargetTools(), verify=verify))
    uv = verify.calls[0]["user_values"]
    assert uv["instruction"] == "Push task 2 to the 13th"
    # a resolved target carries its item's stable id; the verifier renders that item
    assert uv["stated_values"] == [
        {"target": "t1", "field": "deadline", "value": "2026-11-13",
         "target_stable_id": SID2},
        {"target": "t2", "field": "priority", "value": "high",
         "target_stable_id": SID_DONE},
        {"target": None, "field": "responsible", "value": "Mike",
         "target_stable_id": None}]



class HideVerify(Verify):
    """Labels every element; conclusion lines in `hide` come back hidden (an unsupported
    line, spec 2026-11-05 §7.2), with a detail."""

    def __init__(self, hide=()):
        super().__init__()
        self.hide = set(hide)

    def __call__(self, draft, **kw):
        out = super().__call__(draft, **kw)
        for c in out["conclusion"]:
            if c["index"] in self.hide:
                c.update(confidence="uncertain", hidden=True,
                         detail={"model": VERIFIER, "served": None, "attempt": 1,
                                 "reason_code": "not_in_evidence", "reason": "No."})
        return out


def test_a_hidden_conclusion_line_is_stored_but_answers_no_sub_task():
    write = write_obj(conclusion=[{"text": "Hidden one.", "refs": ["R1"],
                                   "confidence": "supported"},
                                  {"text": "The pour was done.", "refs": ["R1"],
                                   "confidence": "supported"}],
                      actions=[_close1()],
                      sub_tasks=[_sub("s1", "done", conclusion=[0]),
                                 _sub("s2", "done", actions=[0], conclusion=[0, 1])])
    model = Model(plan=[plan_obj(targets=NAMED_T1, sub_tasks=PLAN_SUBS)], write=[write])
    payload = st.run(make_ctx(), deps(model=model, verify=HideVerify(hide={0})))
    assert [(c["text"], c.get("hidden", False)) for c in payload["conclusion"]] == [
        ("Hidden one.", True), ("The pour was done.", False)]
    assert payload["conclusion"][0]["verifier_detail"]["reason_code"] == "not_in_evidence"
    # s1 rested only on the hidden line: not done, as when the line was dropped
    assert [(s["id"], s["status"], s["reason_code"], s["conclusion"])
            for s in payload["sub_tasks"]] == [
        ("s1", "not_done", "dropped_by_checks", []), ("s2", "done", None, [1])]
    assert payload["run_trace"]["conclusion"] == 1
    assert action_pack_view.payload_shape_error(payload) is None


def test_apply_verdicts_carries_hidden_and_rejects_a_non_bool():
    draft = {"conclusion": [{"text": "c", "refs": ["R1"], "confidence": "supported"}],
             "actions": []}
    conclusion, *_rest, ignored = st.apply_verdicts(draft, {
        "conclusion": [{"index": 0, "confidence": "uncertain", "hidden": True}],
        "actions": [], "model": VERIFIER})
    assert conclusion[0]["hidden"] is True and ignored["fields"] == 0
    with pytest.raises(agent.StageError):
        st.apply_verdicts(draft, {"conclusion": [{"index": 0, "confidence": "uncertain",
                                                  "hidden": "yes"}],
                                  "actions": [], "model": VERIFIER})


def test_sources_cited_only_by_a_hidden_line_are_not_in_the_pack():
    # review of #1254: GET sources and refs_cited follow the shown lines only
    write = write_obj(conclusion=[{"text": "Hidden one.", "refs": ["R2"],
                                   "confidence": "supported"},
                                  {"text": "The pour was done.", "refs": ["R1"],
                                   "confidence": "supported"}], actions=[])
    model = Model(plan=[plan_obj()], write=[write])
    payload = st.run(make_ctx(), deps(model=model, verify=HideVerify(hide={0})))
    assert payload["conclusion"][0].get("hidden") is True
    assert {s["ref"] for s in payload["sources"]} == {"R1"}
