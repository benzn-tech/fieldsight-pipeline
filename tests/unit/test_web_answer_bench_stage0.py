"""Claude web-answer bench, Stage 0 (plan 2026-10-27-claude-web-answer-bench-stage0).

Fake Anthropic client and fake draft function: no SDK, no network. The modules
are loaded by path because the directory name has a hyphen.
"""
import importlib.util
import io
import json
import os
import sys
from types import SimpleNamespace as NS

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "web-answer-bench")
if BENCH_DIR not in sys.path:
    sys.path.insert(0, BENCH_DIR)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cv = _load("claude_verify")
ledger = _load("ledger")
stage0 = _load("stage0")

SECRET_Q = "QSENTINEL-what-is-the-levy"
SECRET_A = "ASENTINEL-the-answer-text"


# ------------------------------------------------------------------ fakes

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def usage(inp=1000, out=200, searches=1):
    return NS(input_tokens=inp, output_tokens=out,
              server_tool_use=NS(web_search_requests=searches))


def results_block(n=2):
    return NS(type="web_search_tool_result", tool_use_id="srv_1",
              content=[NS(type="web_search_result", url=f"https://ex{i}.govt.nz/p",
                          title=f"ex{i}.govt.nz") for i in range(n)])


def error_block(code="too_many_requests"):
    return NS(type="web_search_tool_result", tool_use_id="srv_1",
              content=NS(type="web_search_tool_result_error", error_code=code))


def response(stop="end_turn", blocks=None, text=SECRET_A, u=None):
    content = list(blocks if blocks is not None else [results_block()])
    if text is not None:
        content.append(NS(type="text", text=text))
    return NS(stop_reason=stop, content=content, usage=u or usage())


class FakeStatusError(Exception):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


class APITimeoutError(Exception):
    pass


class FakeClient:
    """`script` maps a model/tool key to a list of outcomes consumed in order:
    a response object, an exception instance, or a (seconds, outcome) pair that
    advances the clock before returning."""

    def __init__(self, clock, script=None, default=None):
        self.clock = clock
        self.script = script or {}
        self.default = default
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        key = (kw["model"], kw["tools"][0]["type"])
        queue = self.script.get(key)
        item = queue.pop(0) if queue else (self.default() if self.default else response())
        if isinstance(item, tuple):
            secs, item = item
            self.clock.t += secs
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------- Task 1: verify

def test_request_shape():
    req = cv.build_request("claude-sonnet-5-5", "web_search_20260209", "PROMPT")
    assert req["model"] == "claude-sonnet-5-5"
    assert req["max_tokens"] == 4096
    assert req["output_config"] == {"effort": "low"}
    assert "thinking" not in req
    assert req["tools"] == [{"type": "web_search_20260209", "name": "web_search",
                             "max_uses": 3,
                             "user_location": {"type": "approximate", "country": "NZ",
                                               "timezone": "Pacific/Auckland"}}]
    assert req["messages"] == [{"role": "user", "content": "PROMPT"}]


def test_error_object_content_is_not_searched():
    p = cv.parse(response(blocks=[error_block("too_many_requests")]))
    assert p["searched"] is False
    assert p["error_code"] == "too_many_requests"
    assert p["result_count"] == 0


def test_results_list_is_searched():
    p = cv.parse(response(blocks=[results_block(2)], u=usage(500, 50, 1)))
    assert p["searched"] is True
    assert p["result_count"] == 2
    assert p["source_count"] == 2
    assert (p["input_tokens"], p["output_tokens"], p["search_requests"]) == (500, 50, 1)


def test_empty_results_list_is_not_searched():
    assert cv.parse(response(blocks=[results_block(0)]))["searched"] is False


def _verify(client, clock, budget=14.0):
    return cv.verify(client, model="claude-sonnet-5-5", tool_version="web_search_20260209",
                     prompt="P", budget_s=budget, clock=clock)


def test_pause_turn_one_continuation_then_ok():
    clock = Clock()
    paused = response(stop="pause_turn", text=None, u=usage(100, 10, 1))
    key = ("claude-sonnet-5-5", "web_search_20260209")
    client = FakeClient(clock, {key: [(5.0, paused), (3.0, response(u=usage(200, 20, 1)))]})
    r = _verify(client, clock)
    assert r["outcome"] == "ok"
    assert r["continuation"] is True
    assert len(client.requests) == 2
    assert client.requests[0]["timeout"] == pytest.approx(14.0)
    assert client.requests[1]["timeout"] == pytest.approx(9.0)
    assert client.requests[1]["messages"] == [
        {"role": "user", "content": "P"},
        {"role": "assistant", "content": paused.content}]
    assert r["latency_ms"] == 8000
    assert (r["input_tokens"], r["output_tokens"], r["search_requests"]) == (300, 30, 2)


def test_second_pause_fails():
    clock = Clock()
    key = ("claude-sonnet-5-5", "web_search_20260209")
    client = FakeClient(clock, {key: [(2.0, response(stop="pause_turn", text=None)),
                                      (2.0, response(stop="pause_turn", text=None))]})
    r = _verify(client, clock)
    assert r["outcome"] == "paused_twice"
    assert len(client.requests) == 2


def test_pause_after_budget_spent_no_continuation():
    clock = Clock()
    key = ("claude-sonnet-5-5", "web_search_20260209")
    client = FakeClient(clock, {key: [(13.5, response(stop="pause_turn", text=None))]})
    r = _verify(client, clock)
    assert r["outcome"] == "timeout"
    assert len(client.requests) == 1


def test_400_is_rejected():
    clock = Clock()
    key = ("claude-sonnet-5-5", "web_search_20260209")
    r = _verify(FakeClient(clock, {key: [FakeStatusError(400)]}), clock)
    assert r["outcome"] == "rejected"
    assert r["http_status"] == 400


def test_timeout_is_timeout():
    clock = Clock()
    key = ("claude-sonnet-5-5", "web_search_20260209")
    r = _verify(FakeClient(clock, {key: [(14.0, APITimeoutError("t"))]}), clock)
    assert r["outcome"] == "timeout"


def test_refusal_and_max_tokens_outcomes():
    clock = Clock()
    key = ("claude-sonnet-5-5", "web_search_20260209")
    client = FakeClient(clock, {key: [response(stop="refusal"), response(stop="max_tokens")]})
    assert _verify(client, clock)["outcome"] == "refusal"
    assert _verify(client, clock)["outcome"] == "max_tokens"


def test_ok_without_search_is_failed():
    clock = Clock()
    key = ("claude-sonnet-5-5", "web_search_20260209")
    client = FakeClient(clock, {key: [response(blocks=[error_block()])]})
    assert _verify(client, clock)["outcome"] == "failed"


def test_answer_without_any_search_attempt_is_no_search():
    # Stage 0 10-27: Sonnet answered the unknowable item (t5) in ~4 s without
    # calling the tool. That is a decision, not an error: prod returns the draft
    # "unverified" (_verify_ok needs searched), so it costs no latency.
    clock = Clock()
    key = ("claude-sonnet-5-5", "web_search_20260209")
    client = FakeClient(clock, {key: [response(blocks=[], u=usage(searches=0))]})
    assert _verify(client, clock)["outcome"] == "no_search"


# ---------------------------------------------------------- Task 2: ledger

def test_ledger_persists_across_instances(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    a = ledger.Ledger(p)
    a.add(1.25)
    a.add(0.75)
    assert ledger.Ledger(p).spent == pytest.approx(2.0)


def test_ledger_exhausted_at_cap(tmp_path):
    lg = ledger.Ledger(str(tmp_path / "l.json"), cap=45.0)
    lg.add(44.99)
    assert not lg.exhausted()
    lg.add(0.01)
    assert lg.exhausted()


def test_call_cost_sonnet_and_haiku():
    assert ledger.call_cost("claude-sonnet-5-5", 1_000_000, 100_000, 10, 10.0) == \
        pytest.approx(2.0 + 1.0 + 0.1)
    assert ledger.call_cost("claude-haiku-5-5", 1_000_000, 1_000_000, 0, 10.0) == \
        pytest.approx(0.10 + 0.50)


# ------------------------------------------------------- Task 3: the script

KEYS = {"ANTHROPIC_API_KEY": "sk-ant-x", "CORROBORATION_API_KEY": "sk-or-x"}


def _argv(out, *extra):
    return ["--out", str(out), "--search-price-per-1000", "10", *extra]


def test_validate_requires_keys_for_live_run(tmp_path):
    with pytest.raises(SystemExit) as e:
        stage0.validate(_argv(tmp_path, "--confirm-calls", "77"), {}, REPO)
    assert e.value.code == 2


def test_validate_dry_run_needs_no_keys(tmp_path):
    a = stage0.validate(_argv(tmp_path, "--dry-run"), {}, REPO)
    assert a.dry_run


def test_validate_refuses_out_inside_repo():
    with pytest.raises(SystemExit) as e:
        stage0.validate(_argv(os.path.join(REPO, "tmp-out"), "--confirm-calls", "77"),
                        KEYS, REPO)
    assert e.value.code == 2


def test_validate_requires_search_price(tmp_path):
    with pytest.raises(SystemExit) as e:
        stage0.validate(["--out", str(tmp_path), "--dry-run"], KEYS, REPO)
    assert e.value.code == 2


def test_confirm_calls_must_equal_upper(tmp_path):
    for n in ("41", "76", "78"):
        with pytest.raises(SystemExit) as e:
            stage0.validate(_argv(tmp_path, "--confirm-calls", n), KEYS, REPO)
        assert e.value.code == 2
    assert stage0.validate(_argv(tmp_path, "--confirm-calls", "77"), KEYS, REPO)


def test_plan_counts():
    assert stage0.plan() == {"drafts": 5, "planned": 41, "upper": 77, "searches_upper": 216}


def test_cost_upper_at_10_per_1000():
    # sonnet 48 x (46k x $2 + 4096 x $10)/M + haiku 24 x (46k x $0.1 + 4096 x $0.5)/M
    # + 216 searches x $0.01
    assert stage0.cost_upper(10.0) == pytest.approx(6.38208 + 0.159552 + 2.16, abs=1e-4)


def test_round_robin_order():
    assert stage0.order(["t1", "t2", "t3", "t4", "t5"]) == \
        ["t1", "t2", "t3", "t4", "t5", "t1", "t2", "t3", "t4", "t5", "t1", "t2"]


def test_load_tune_reads_the_five_tune_rows():
    items = stage0.load_tune(os.path.join(BENCH_DIR, "set.v1.jsonl"))
    assert [i["id"] for i in items] == ["t1", "t2", "t3", "t4", "t5"]


def _items():
    return [{"id": f"t{i}", "question": f"{SECRET_Q}-{i}"} for i in range(1, 6)]


def _run(tmp_path, client, clock, *, dry=False):
    args = stage0.validate(_argv(tmp_path, "--dry-run" if dry else "--confirm-calls",
                                 *([] if dry else ["77"])), KEYS, REPO)
    out = io.StringIO()
    drafts = []

    def draft_fn(q):
        drafts.append(q)
        return f"draft of {q}"

    code, summary = stage0.run(args, items=_items(), client=client, draft_fn=draft_fn,
                               clock=clock, stdout=out)
    return code, summary, out.getvalue(), drafts


def test_dry_run_prints_counts_and_dollars_no_calls(tmp_path):
    clock = Clock()
    client = FakeClient(clock)
    code, _, text, drafts = _run(tmp_path, client, clock, dry=True)
    assert code == 0
    assert client.requests == [] and drafts == []
    assert "77" in text and "41" in text and "$8.70" in text


def test_full_run_order_and_counts(tmp_path):
    clock = Clock()
    client = FakeClient(clock, default=lambda: (2.0, response()))
    code, summary, _, drafts = _run(tmp_path, client, clock)
    assert code == 0
    assert len(drafts) == 5
    assert len(client.requests) == 36
    calls = [json.loads(l) for l in open(tmp_path / "stage0_calls.jsonl", encoding="utf-8")]
    first = [c["item"] for c in calls if c["config"] == "sonnet-20260209"]
    assert first == stage0.order(["t1", "t2", "t3", "t4", "t5"])
    assert summary["configs"]["sonnet-20260209"]["p50_ms"] == 2000


def test_rejected_config_stops_only_itself(tmp_path):
    clock = Clock()
    key = ("claude-sonnet-5-5", "web_search_20250305")
    client = FakeClient(clock, {key: [FakeStatusError(400)]},
                        default=lambda: (2.0, response()))
    code, summary, _, _ = _run(tmp_path, client, clock)
    sent = [r["tools"][0]["type"] for r in client.requests if r["model"] == "claude-sonnet-5-5"]
    assert sent.count("web_search_20250305") == 1
    assert summary["configs"]["sonnet-20250305"]["rejected"] is True
    assert summary["configs"]["haiku-20250305"]["n"] == 12


def test_ledger_hard_stop_across_runs(tmp_path):
    ledger.Ledger(str(tmp_path / "anthropic_ledger.json")).add(45.0)
    clock = Clock()
    client = FakeClient(clock)
    code, summary, _, _ = _run(tmp_path, client, clock)
    assert code == 3
    assert client.requests == []
    assert summary["hard_stop"] is True


def test_ledger_accumulates_spend(tmp_path):
    clock = Clock()
    client = FakeClient(clock, default=lambda: (1.0, response(u=usage(1000, 100, 1))))
    _run(tmp_path, client, clock)
    spent = ledger.Ledger(str(tmp_path / "anthropic_ledger.json")).spent
    expected = 24 * ledger.call_cost("claude-sonnet-5-5", 1000, 100, 1, 10.0) + \
        12 * ledger.call_cost("claude-haiku-5-5", 1000, 100, 1, 10.0)
    assert spent == pytest.approx(expected)


def test_summary_counts_refusals_and_max_tokens_separately(tmp_path):
    clock = Clock()
    key = ("claude-haiku-5-5", "web_search_20250305")
    client = FakeClient(clock, {key: [response(stop="refusal"), response(stop="max_tokens"),
                                      response(stop="max_tokens")]},
                        default=lambda: (1.0, response()))
    _, summary, _, _ = _run(tmp_path, client, clock)
    h = summary["configs"]["haiku-20250305"]
    assert (h["refusals"], h["max_tokens_stops"]) == (1, 2)
    assert h["searched_rate"] == pytest.approx(12 / 12)


def test_timeout_fails_the_max_gate(tmp_path):
    clock = Clock()
    key = ("claude-haiku-5-5", "web_search_20250305")
    client = FakeClient(clock, {key: [(20.0, APITimeoutError("t"))]},
                        default=lambda: (1.0, response()))
    _, summary, _, _ = _run(tmp_path, client, clock)
    h = summary["configs"]["haiku-20250305"]
    assert h["max_ms"] > stage0.MAX_GATE_MS
    assert "claude-haiku-5-5" in stage0.stop_rule(summary)["dropped"]


def test_a_fast_failure_fails_the_max_gate(tmp_path):
    # A verify that returns quickly without searching is not a fast verify.
    clock = Clock()
    key = ("claude-haiku-5-5", "web_search_20250305")
    client = FakeClient(clock, {key: [(0.5, FakeStatusError(429))]},
                        default=lambda: (1.0, response()))
    _, summary, _, _ = _run(tmp_path, client, clock)
    h = summary["configs"]["haiku-20250305"]
    assert h["failures"] == 1
    assert h["max_ms"] > stage0.MAX_GATE_MS
    assert "claude-haiku-5-5" in stage0.stop_rule(summary)["dropped"]


def test_max_tokens_stop_is_not_a_gate_failure(tmp_path):
    clock = Clock()
    key = ("claude-haiku-5-5", "web_search_20250305")
    client = FakeClient(clock, {key: [response(stop="max_tokens")]},
                        default=lambda: (1.0, response()))
    _, summary, _, _ = _run(tmp_path, client, clock)
    assert summary["configs"]["haiku-20250305"]["failures"] == 0
    assert summary["stop_rule"]["frozen"].get("claude-haiku-5-5") == "web_search_20250305"


def test_no_search_counts_its_wall_time_but_is_reported(tmp_path):
    clock = Clock()
    key = ("claude-haiku-5-5", "web_search_20250305")
    client = FakeClient(clock, {key: [(0.5, response(blocks=[], u=usage(searches=0)))]},
                        default=lambda: (1.0, response()))
    _, summary, _, _ = _run(tmp_path, client, clock)
    h = summary["configs"]["haiku-20250305"]
    assert h["failures"] == 0
    assert h["no_search"] == 1
    assert h["max_ms"] <= stage0.MAX_GATE_MS


def test_resummarize_reclassifies_recorded_no_search_rows():
    rows = [{"config": "haiku-20250305", "outcome": "failed", "http_status": 200,
             "stop_reason": "end_turn", "searched": False, "error_code": None,
             "search_requests": 0, "latency_ms": 3500, "continuation": False,
             "cost_usd": 0.01},
            {"config": "haiku-20250305", "outcome": "failed", "http_status": 200,
             "stop_reason": "end_turn", "searched": False,
             "error_code": "too_many_requests", "search_requests": 1,
             "latency_ms": 2000, "continuation": False, "cost_usd": 0.01}]
    fixed = [stage0.reclassify(r) for r in rows]
    assert [r["outcome"] for r in fixed] == ["no_search", "failed"]


def test_stop_rule_freezes_fastest_tool_per_model_and_drops_others():
    s = {"configs": {
        "sonnet-20260209": {"model": "claude-sonnet-5-5", "tool": "web_search_20260209",
                            "rejected": False, "n": 12, "p50_ms": 8000, "max_ms": 13000},
        "sonnet-20250305": {"model": "claude-sonnet-5-5", "tool": "web_search_20250305",
                            "rejected": False, "n": 12, "p50_ms": 6000, "max_ms": 9000},
        "haiku-20250305": {"model": "claude-haiku-5-5", "tool": "web_search_20250305",
                           "rejected": False, "n": 12, "p50_ms": 5000, "max_ms": 15000}}}
    r = stage0.stop_rule(s)
    assert r["proceed"] is True
    assert r["frozen"] == {"claude-sonnet-5-5": "web_search_20250305"}
    assert r["dropped"] == ["claude-haiku-5-5"]


def test_stop_rule_none_passing_stops():
    s = {"configs": {"sonnet-20260209": {"model": "claude-sonnet-5-5",
                                         "tool": "web_search_20260209", "rejected": True,
                                         "n": 1, "p50_ms": None, "max_ms": None}}}
    assert stage0.stop_rule(s)["proceed"] is False


def test_no_question_or_answer_text_in_stdout_or_summary(tmp_path):
    clock = Clock()
    client = FakeClient(clock, default=lambda: (1.0, response()))
    _, summary, text, _ = _run(tmp_path, client, clock)
    blob = text + json.dumps(summary) + (tmp_path / "stage0_summary.json").read_text("utf-8")
    assert SECRET_Q not in blob and SECRET_A not in blob and "draft of" not in blob
    calls = (tmp_path / "stage0_calls.jsonl").read_text("utf-8")
    assert SECRET_A in calls        # the text lives only in the --out calls file


# ------------------------------------------- re-probe (one config, lower max_uses)

reprobe = _load("reprobe")



def _stage0_out(tmp_path):
    out = tmp_path / "s0"
    out.mkdir()
    with open(out / "stage0_drafts.jsonl", "w", encoding="utf-8") as f:
        for i in range(1, 6):
            f.write(json.dumps({"item": f"t{i}", "draft_ms": 3000, "draft": f"D{i}"}) + "\n")
    return out


def _rp_argv(out, *extra):
    return ["--out", str(out), "--config", "haiku-20250305", "--max-uses", "2",
            "--search-price-per-1000", "10", *extra]


def test_reprobe_plan_is_one_config_no_drafts():
    assert reprobe.plan(2) == {"verifies": 12, "upper": 24, "searches_upper": 48}


def test_reprobe_confirm_calls_must_equal_upper(tmp_path):
    with pytest.raises(SystemExit):
        reprobe.validate(_rp_argv(_stage0_out(tmp_path), "--confirm-calls", "77"), KEYS)


def test_reprobe_run_uses_stored_drafts_and_the_lower_max_uses(tmp_path):
    out = _stage0_out(tmp_path)
    args = reprobe.validate(_rp_argv(out, "--confirm-calls", "24"), KEYS)
    clock = Clock()
    client = FakeClient(clock, default=lambda: (1.0, response()))
    code, summary = reprobe.run(args, client=client, clock=clock, stdout=io.StringIO())
    assert code == 0
    assert len(client.requests) == 12
    assert {r["tools"][0]["max_uses"] for r in client.requests} == {2}
    assert all(r["model"] == "claude-haiku-5-5" for r in client.requests)
    assert "D1" in client.requests[0]["messages"][0]["content"]
    c = summary["config"]
    assert c["n"] == 12 and c["max_uses"] == 2
    assert (out / "reprobe_haiku-20250305_mu2_calls.jsonl").exists()
    assert json.loads((out / "anthropic_ledger.json").read_text())  # shared ledger


def test_reprobe_missing_stage0_drafts_refuses(tmp_path):
    empty = tmp_path / "e"
    empty.mkdir()
    with pytest.raises(SystemExit):
        reprobe.validate(_rp_argv(empty, "--confirm-calls", "24"), KEYS)
