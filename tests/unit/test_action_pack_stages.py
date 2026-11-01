"""The action agent's stages (Task 11): plan, gather, write, snapshots, verify seam -> the
store payload. Every seam is faked: models (an injected call_model), tools (execute / items
run), the web (general_answer), the fetcher, S3 and the verifier. No network, no AWS."""
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
                        "refs": ["R1", "R2"]}]}
    obj.update(over)
    return obj


class Model:
    """call_model fake: answers by caller; records every call (prompt, deadline, caller)."""

    def __init__(self, plan=None, rounds=None, write=None, used=None, usage=None):
        self.answers = {"action_plan": list(plan or [plan_obj()]),
                        "action_round": list(rounds or []),
                        "action_write": list(write or [write_obj()])}
        self.used = used or {}
        self.usage = usage
        self.calls = []

    def __call__(self, prompt, *, max_tokens, force_json, model, deadline, caller, info):
        self.calls.append({"prompt": prompt, "deadline": deadline, "caller": caller,
                           "model": model})
        queue = self.answers[caller]
        ans = queue.pop(0) if len(queue) > 1 else (queue[0] if queue
                                                     else {"calls": [], "done": True})
        info["model"] = self.used.get(caller, model)
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
                 writer_served=None):
        self.calls.append({"draft": draft, "sources": sources, "writer_model": writer_model,
                           "deadline": deadline, "registry": registry})
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
    assert all(isinstance(v, (int, bool, dict, list)) for v in trace.values())


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
    monkeypatch.setattr(st, "WRITE_RENDER_MAX_CHARS", 380)
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
         "refs": ["R1"]},
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
         "rationale": "r", "refs": ["R1"]}]}
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
    assert dropped["targets"] == 2 and dropped["actions"] == 7
    assert dropped["conclusion"] == 1 and dropped["conclusion_downgraded"] == 1
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
