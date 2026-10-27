"""Web-answer bench, prompt x model matrix (plan 2026-10-28-web-answer-prompt-matrix).

Fakes only: no SDK, no network. The modules are loaded by path because the
directory name has a hyphen.
"""
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "web-answer-bench")
SRC = os.path.join(REPO, "src")
for _p in (BENCH_DIR, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


import web_answer                                        # noqa: E402

prompts = _load("prompts")


# ------------------------------------------------------- Task 1: prompt texts

# git show 518900a8:src/web_answer.py, VERIFY_PROMPT ("colleague").
VERIFY_V1 = """A colleague answered the question below from memory. Search the open web
to check it. Keep what the sources confirm, correct what they contradict (citing
the source), and mark anything still unconfirmed as [unverified]. Be brief.

## Question
{question}

## Colleague's answer
{draft}
"""

# git show 518900a8^:src/web_answer.py, WEB_PROMPT.
WEB_V1 = """Search the open web and answer this question, for a reader working
on a New Zealand construction site.

Report only what public sources say. Do not speculate, and do not fill a gap with
general knowledge -- if the sources do not answer the question, say so plainly
rather than guessing.

Name the source of each fact you use. Be brief.

## Question
{question}
"""


def test_shipped_prompts_match_the_constants():
    assert prompts.load("draft_v1") == web_answer.DRAFT_PROMPT
    assert prompts.load("verify_v2") == web_answer.VERIFY_PROMPT


def test_historical_prompts_match_git():
    assert prompts.load("verify_v1") == VERIFY_V1
    assert prompts.load("web_v1") == WEB_V1


def test_candidates_and_placeholders():
    assert prompts.CANDIDATES == {"draft_v1": "draft", "verify_v1": "verify",
                                  "verify_v2": "verify", "web_v1": "single",
                                  "merged_v1": "single"}
    for pid, step in prompts.CANDIDATES.items():
        text = prompts.load(pid)
        assert "{question}" in text
        assert ("{draft}" in text) == (step == "verify"), pid
        # Each one fills with the keys its step supplies and nothing else.
        text.format(question="Q", draft="D")


def test_sha_is_of_the_file_text():
    import hashlib
    assert prompts.sha("draft_v1") == \
        hashlib.sha256(web_answer.DRAFT_PROMPT.encode("utf-8")).hexdigest()


def test_unknown_prompt_id_refused():
    with pytest.raises(KeyError):
        prompts.load("../set.v1")


# ------------------------------------------------------- Task 2: ledger pools

ledger = _load("ledger")


def test_old_format_ledger_counts_as_stage0(tmp_path):
    p = tmp_path / "anthropic_ledger.json"
    p.write_text('{"spent_usd": 1.42, "cap_usd": 45.0}', encoding="utf-8")
    lg = ledger.Ledger(str(p))
    assert lg.spent == pytest.approx(1.42)
    assert lg.pool_spent("stage0") == pytest.approx(1.42)
    assert lg.pool_spent("matrix") == 0.0
    assert lg.room() == pytest.approx(43.58)


def test_matrix_pool_exhaustion_and_persistence(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    lg = ledger.Ledger(p)
    lg.add(1.42)                                  # bare add() is Stage 0's
    lg.add(17.9, pool="matrix")
    assert not lg.pool_exhausted("matrix", ledger.MATRIX_POOL_CAP)
    lg.add(0.2, pool="matrix")
    assert lg.pool_exhausted("matrix", 18)
    again = ledger.Ledger(p)
    assert again.pool_spent("matrix") == pytest.approx(18.1)
    assert again.pool_spent("stage0") == pytest.approx(1.42)
    assert again.spent == pytest.approx(19.52)
    assert again.room() == pytest.approx(45 - 19.52)


def test_total_cap_still_45_across_pools(tmp_path):
    lg = ledger.Ledger(str(tmp_path / "l.json"))
    lg.add(30.0, pool="matrix")
    lg.add(14.99, pool="scored")
    assert not lg.exhausted()
    lg.add(0.01)
    assert lg.exhausted()
    assert (ledger.CAP_USD, ledger.MATRIX_POOL_CAP, ledger.SCORED_RESERVE) == (45.0, 18.0, 23.0)


# ------------------------------------------------ Task 3: OpenRouter bench caller

import json                                              # noqa: E402
from types import SimpleNamespace as NS                  # noqa: E402

import corroboration_client                              # noqa: E402

orb = _load("openrouter_bench")

SECRET_Q = "QSENTINEL-what-is-the-levy"
SECRET_A = "ASENTINEL-the-answer-text"
OR_KEY = "sk-or-SENTINEL-key"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def completion(text=SECRET_A, urls=("https://ex0.govt.nz/p",), choices=True, usage=None):
    data = {"model": "m", "provider": "Google AI Studio",
            "usage": usage or {"prompt_tokens": 300, "completion_tokens": 40, "cost": 0.0012}}
    if choices:
        data["choices"] = [{"finish_reason": "stop", "message": {
            "content": text,
            "annotations": [{"type": "url_citation",
                             "url_citation": {"url": u, "title": "ex.govt.nz"}} for u in urls]}}]
    return NS(status=200, data=json.dumps(data).encode("utf-8"))


class Timeout(Exception):
    pass


class FakeHttp:
    """Outcomes consumed in order: a response, an exception, or (seconds, outcome)."""

    def __init__(self, clock, script=(), default=None):
        self.clock, self.script, self.default = clock, list(script), default
        self.requests = []

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.requests.append({"method": method, "url": url, "payload": json.loads(body),
                              "headers": headers, "timeout": timeout})
        item = self.script.pop(0) if self.script else self.default()
        if isinstance(item, tuple):
            secs, item = item
            self.clock.t += secs
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def or_key(monkeypatch):
    monkeypatch.setenv("CORROBORATION_API_KEY", OR_KEY)


def test_payload_deepseek_pinned_reasoning_off():
    p = orb.build_payload("deepseek", "P", web=True, max_tokens=2048)
    assert p["model"] == "deepseek/deepseek-v4.1-flash"
    assert p["provider"] == {"only": ["coreweave/fp8"], "allow_fallbacks": False}
    assert p["reasoning"] == {"enabled": False}
    assert p["plugins"] == corroboration_client.WEB_PLUGIN
    assert p["max_tokens"] == 2048
    assert p["messages"] == [{"role": "user", "content": "P"}]


def test_payload_mimo_and_grader():
    p = orb.build_payload("mimo", "P", web=False, max_tokens=1024)
    assert p["model"] == "xiaomi/mimo-v2.6-pro"
    assert p["reasoning"] == {"enabled": False}
    assert "provider" not in p and "plugins" not in p
    g = orb.build_payload("grader", "P", web=False, max_tokens=256)
    assert g["model"] == "deepseek/deepseek-v4.1-flash"
    assert g["provider"] == {"only": ["coreweave/fp8"], "allow_fallbacks": False}
    with pytest.raises(ValueError):                 # draft only: web plugin unmeasured
        orb.build_payload("mimo", "P", web=True, max_tokens=1024)


def test_payload_gemini_uses_the_shipped_provider_order():
    p = orb.build_payload("gemini", "P", web=False, max_tokens=1024)
    assert p["model"] == "google/gemini-3.8-flash"
    assert p["reasoning"] == {"effort": "low"}
    assert p["provider"] == corroboration_client.provider_preference("google/gemini-3.8-flash")
    assert "plugins" not in p
    assert orb.build_payload("gemini", "P", web=True, max_tokens=1)["plugins"] == \
        corroboration_client.WEB_PLUGIN


def test_ok_with_citations(or_key):
    clock = Clock()
    http = FakeHttp(clock, [(4.0, completion())])
    r = orb.call("gemini", "P", web=True, timeout=14.0, http=http, clock=clock)
    assert r["outcome"] == "ok" and r["searched"] is True
    assert (r["result_count"], r["source_count"], r["calls"]) == (1, 1, 1)
    assert (r["input_tokens"], r["output_tokens"]) == (300, 40)
    assert r["latency_ms"] == 4000 and r["http_status"] == 200
    assert r["text"] == SECRET_A
    assert r["openrouter_cost_usd"] == pytest.approx(0.0012)
    assert http.requests[0]["url"] == corroboration_client.API_URL
    assert http.requests[0]["timeout"] == 14.0
    assert OR_KEY not in json.dumps(r)


def test_draft_without_web_is_ok_unsearched(or_key):
    clock = Clock()
    r = orb.call("mimo", "P", web=False, timeout=12.0,
                 http=FakeHttp(clock, [completion(urls=())]), clock=clock)
    assert r["outcome"] == "ok" and r["searched"] is False


def test_web_text_without_citations_is_no_search(or_key):
    clock = Clock()
    r = orb.call("deepseek", "P", web=True, timeout=14.0,
                 http=FakeHttp(clock, [completion(urls=())]), clock=clock)
    assert r["outcome"] == "no_search"


def test_200_without_choices_is_failed_no_retry_storm(or_key):
    clock = Clock()
    http = FakeHttp(clock, default=lambda: (1.0, completion(choices=False)))
    r = orb.call("gemini", "P", web=True, timeout=14.0, http=http, clock=clock)
    assert r["outcome"] == "failed" and len(http.requests) == 1
    r = orb.call("gemini", "P", web=True, timeout=14.0, retry_left_s=20.0, http=http,
                 clock=clock)
    assert r["outcome"] == "failed" and r["calls"] == 2 and len(http.requests) == 3


def test_empty_content_and_bad_json_are_failed(or_key):
    clock = Clock()
    bad = NS(status=200, data=b"<html>")
    for item in (completion(text=""), bad):
        r = orb.call("gemini", "P", web=False, timeout=12.0,
                     http=FakeHttp(clock, [item]), clock=clock)
        assert r["outcome"] == "failed"


def test_timeout_exception_is_timeout(or_key):
    clock = Clock()
    r = orb.call("gemini", "P", web=True, timeout=14.0,
                 http=FakeHttp(clock, [(14.0, Timeout("read timed out"))]), clock=clock)
    assert r["outcome"] == "timeout"


def test_http_error_status(or_key):
    clock = Clock()
    r = orb.call("gemini", "P", web=True, timeout=14.0,
                 http=FakeHttp(clock, [NS(status=502, data=b"{}")]), clock=clock)
    assert r["outcome"] == "http_error" and r["http_status"] == 502


def test_one_retry_only_with_ten_seconds_left(or_key):
    clock = Clock()
    fail = lambda: (5.0, completion(urls=()))                         # noqa: E731
    http = FakeHttp(clock, default=fail)
    r = orb.call("gemini", "P", web=True, timeout=14.0, retry_left_s=14.0, http=http,
                 clock=clock)
    assert r["calls"] == 1                         # 9 s left after attempt 1
    http = FakeHttp(clock, [fail(), (3.0, completion())])
    r = orb.call("gemini", "P", web=True, timeout=14.0, retry_left_s=16.0, http=http,
                 clock=clock)
    assert r["calls"] == 2 and r["outcome"] == "ok" and r["retried"] is True
    assert http.requests[1]["timeout"] == pytest.approx(11.0)
    assert r["latency_ms"] == 8000


def test_pinned_429_is_recorded_not_retried(or_key):
    clock = Clock()
    http = FakeHttp(clock, default=lambda: NS(status=429, data=b"{}"))
    r = orb.call("deepseek", "P", web=True, timeout=14.0, retry_left_s=22.0, http=http,
                 clock=clock)
    assert r["outcome"] == "http_error" and r["http_status"] == 429
    assert len(http.requests) == 1


def test_missing_key_fails_without_a_request(monkeypatch):
    monkeypatch.delenv("CORROBORATION_API_KEY", raising=False)
    clock = Clock()
    http = FakeHttp(clock, default=completion)
    r = orb.call("gemini", "P", web=True, timeout=14.0, http=http, clock=clock)
    assert r["outcome"] == "failed" and r["error_code"] == "no_key"
    assert http.requests == []


# ------------------------------------------------- Task 5: cells and round 0

import io                                                # noqa: E402

stage0 = _load("stage0")
matrix = _load("matrix")

KEYS = {"ANTHROPIC_API_KEY": "sk-ant-x", "CORROBORATION_API_KEY": "sk-or-x"}


def test_round0_cells_and_order():
    cells = matrix.round0_cells()
    assert len(cells) == 21 and len(set(cells)) == 21
    assert cells[0] == ("draft", "draft_v1", "gemini")
    assert sorted(c for c in cells if c[0] == "draft") == sorted(
        ("draft", "draft_v1", m) for m in matrix.MODEL_KEYS)
    for step, pids in (("verify", ("verify_v1", "verify_v2")),
                       ("single", ("web_v1", "merged_v1"))):
        assert sorted(c for c in cells if c[0] == step) == sorted(
            (step, p, m) for p in pids for m in ("gemini", "deepseek", "sonnet", "haiku"))
    vendor = [{"sonnet": 2, "haiku": 1}.get(m, 0) for _, _, m in cells]
    assert vendor == sorted(vendor)             # OpenRouter, then Haiku, then Sonnet


def test_plan_hand_count():
    # Anthropic: drafts 2 x 15 x 1, verify 4 x 15 x 2, single 4 x 15 x 2 = 270.
    # OpenRouter: drafts 3 x 15, verify 4 x 15 x 2 (retry), single 4 x 15 = 225.
    # $: Sonnet 135 calls x (46k x $2 + 4096 x $10)/M = 17.9496; Haiku 135 x
    # (46k x $0.1 + 4096 x $0.5)/M = 0.89748; searches 120 x 3 + 120 x 2 = 600 x $0.01.
    p = matrix.plan(matrix.round0_cells(), reps=3, price_per_1000=10.0)
    assert (p["runs"], p["anthropic_upper"], p["openrouter_upper"]) == (315, 270, 225)
    assert p["upper"] == 495 and p["searches_upper"] == 600
    assert p["anthropic_usd_upper"] == pytest.approx(17.9496 + 0.89748 + 6.0, abs=1e-4)


def _m_argv(out, *extra):
    return ["--out", str(out), "--round", "0", "--search-price-per-1000", "10", *extra]


def test_validate_confirm_and_out(tmp_path):
    with pytest.raises(SystemExit) as e:
        matrix.validate(_m_argv(tmp_path, "--confirm-calls", "494"), KEYS, REPO)
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        matrix.validate(_m_argv(tmp_path, "--confirm-calls", "495"), {}, REPO)
    with pytest.raises(SystemExit):
        matrix.validate(_m_argv(os.path.join(REPO, "x"), "--dry-run"), {}, REPO)
    assert matrix.validate(_m_argv(tmp_path, "--confirm-calls", "495"), KEYS, REPO)
    assert matrix.validate(_m_argv(tmp_path, "--dry-run"), {}, REPO).dry_run


def _m_items():
    return [{"id": f"t{i}", "question": f"{SECRET_Q}-{i}",
             "kind": "unsure" if i == 5 else "standard"} for i in range(1, 6)]


class ClaudeFake:
    """Echoes the prompt back as the answer; searches when it has a tool."""

    def __init__(self, clock, usage=(1000, 100, 1), secs=2.0):
        self.clock, self.usage, self.secs = clock, usage, secs
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        self.clock.t += self.secs
        inp, out, s = self.usage
        tools = bool(kw.get("tools"))
        content = []
        if tools:
            content.append(NS(type="web_search_tool_result", content=[
                NS(type="web_search_result", url="https://ex0.govt.nz/p", title="t")]))
        content.append(NS(type="text", text=SECRET_A + " ECHO " + kw["messages"][0]["content"]))
        return NS(stop_reason="end_turn", content=content,
                  usage=NS(input_tokens=inp, output_tokens=out,
                           server_tool_use=NS(web_search_requests=s if tools else 0)))


class OrFake:
    """Fake `openrouter_bench.call`: gemini drafts are numbered, the rest echo."""

    def __init__(self, clock, fail_draft=None, no_search_items=()):
        self.clock, self.fail_draft, self.no_search_items = clock, fail_draft, no_search_items
        self.calls = []
        self.n_drafts = 0

    def __call__(self, profile, prompt, *, web, timeout, max_tokens=1024, retry_left_s=None,
                 http=None, clock=None):
        self.calls.append({"profile": profile, "web": web, "timeout": timeout,
                           "retry_left_s": retry_left_s, "prompt": prompt})
        self.clock.t += 3.0
        base = {"searched": web, "result_count": int(web), "source_count": int(web),
                "result_urls": ["https://x.govt.nz/a"] if web else [],
                "result_titles": ["x.govt.nz"] if web else [],
                "latency_ms": 3000, "calls": 1, "input_tokens": 10, "output_tokens": 5,
                "http_status": 200, "error_code": None, "stop_reason": "stop",
                "retried": False, "openrouter_cost_usd": 0.001}
        if profile == "gemini" and not web:
            self.n_drafts += 1
            if self.n_drafts == self.fail_draft:
                return dict(base, text="", outcome="failed", error_code="empty_completion")
            return dict(base, text=f"{SECRET_A} DRAFT<{self.n_drafts}>", outcome="ok")
        if web and any(f"{SECRET_Q}-{i[1:]}\n" in prompt for i in self.no_search_items):
            return dict(base, text=SECRET_A, outcome="no_search", searched=False,
                        result_count=0, source_count=0, result_urls=[], result_titles=[])
        return dict(base, text=SECRET_A + " ECHO " + prompt, outcome="ok")


def _m_run(tmp_path, *, dry=False, client=None, or_fake=None, clock=None):
    clock = clock or Clock()
    args = matrix.validate(_m_argv(tmp_path, *(["--dry-run"] if dry else
                                               ["--confirm-calls", "495"])), KEYS, REPO)
    client = client or ClaudeFake(clock)
    or_fake = or_fake or OrFake(clock)
    out = io.StringIO()
    code, summary = matrix.run(args, items=_m_items(), client=client, or_call=or_fake,
                               clock=clock, stdout=out)
    return code, summary, out.getvalue(), client, or_fake


def _rows(tmp_path):
    with open(tmp_path / "matrix_r0_calls.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _is_verify_prompt(p):
    return "## Draft answer" in p or "## Colleague's answer" in p


def test_dry_run_prints_plan_and_makes_no_call(tmp_path):
    code, summary, text, client, or_fake = _m_run(tmp_path, dry=True)
    assert code == 0
    assert client.requests == [] and or_fake.calls == []
    assert "315" in text and "495" in text and "$24.85" in text
    assert not (tmp_path / "matrix_r0_calls.jsonl").exists()


def test_full_round0_counts_order_and_ledger(tmp_path):
    code, summary, _, client, or_fake = _m_run(tmp_path)
    assert code == 0
    rows = _rows(tmp_path)
    assert len(rows) == 315
    assert [r["cell"] for r in rows[:15]] == ["draft/draft_v1/gemini"] * 15
    assert len({r["run_id"] for r in rows}) == 315
    assert all(not any(m in r["run_id"] for m in matrix.MODEL_KEYS) for r in rows)
    assert len(client.requests) == 150 and len(or_fake.calls) == 165
    sonnet = [i for i, r in enumerate(rows) if r["model_key"] == "sonnet"]
    haiku = [i for i, r in enumerate(rows) if r["model_key"] == "haiku"]
    assert max(haiku) < min(sonnet)
    assert len([q for q in client.requests if "tools" not in q]) == 30
    tools = {q["model"]: q["tools"][0] for q in client.requests if "tools" in q}
    assert tools["claude-sonnet-5-5"]["max_uses"] == 3
    assert tools["claude-haiku-5-5"]["max_uses"] == 2
    assert {t["type"] for t in tools.values()} == {"web_search_20250305"}
    lg = ledger.Ledger(str(tmp_path / "anthropic_ledger.json"))
    assert lg.pool_spent("matrix") == pytest.approx(
        sum(r["cost_usd"] for r in rows if r["model_key"] in matrix.CLAUDE))
    assert rows[0]["prompt_sha256"] == prompts.sha("draft_v1")
    s = json.loads((tmp_path / "matrix_r0_summary.json").read_text("utf-8"))
    assert s["cells"]["verify/verify_v2/haiku"]["n"] == 15


def test_verify_uses_the_stored_draft_of_the_same_item_and_rep(tmp_path):
    _m_run(tmp_path)
    rows = _rows(tmp_path)
    drafts = {(r["item"], r["rep"]): r["text"] for r in rows
              if r["cell"] == "draft/draft_v1/gemini"}
    assert len(set(drafts.values())) == 15
    verify = [r for r in rows if r["step"] == "verify"]
    assert len(verify) == 120
    for r in verify:
        # The echo ends with the prompt, and the prompt ends with the draft.
        assert [k for k, d in drafts.items() if r["text"].rstrip().endswith(d)] == \
            [(r["item"], r["rep"])]
        assert r["draft_ms"] == 3000


def test_verify_budget_is_the_chain_left_after_the_draft(tmp_path):
    _, _, _, client, or_fake = _m_run(tmp_path)
    v = [c for c in or_fake.calls if c["web"] and _is_verify_prompt(c["prompt"])]
    assert len(v) == 60
    assert all(c["timeout"] == pytest.approx(14.0) for c in v)
    assert all(c["retry_left_s"] == pytest.approx(22.1 - 3.0) for c in v)
    single = [c for c in or_fake.calls if c["web"] and not _is_verify_prompt(c["prompt"])]
    assert len(single) == 60 and all(c["retry_left_s"] is None for c in single)
    drafts = [c for c in or_fake.calls if not c["web"]]
    assert all(c["timeout"] == pytest.approx(12.0) for c in drafts)
    claude_verify = [q for q in client.requests
                     if "tools" in q and _is_verify_prompt(q["messages"][0]["content"])]
    assert all(q["timeout"] == pytest.approx(14.0) for q in claude_verify)


def test_failed_gemini_draft_skips_its_verify_runs(tmp_path):
    clock = Clock()
    code, summary, _, client, or_fake = _m_run(tmp_path, clock=clock,
                                               or_fake=OrFake(clock, fail_draft=2))
    rows = _rows(tmp_path)
    failed = [(r["item"], r["rep"]) for r in rows
              if r["cell"] == "draft/draft_v1/gemini" and r["outcome"] != "ok"]
    assert len(failed) == 1
    skipped = [r for r in rows if r["outcome"] == "no_draft"]
    assert len(skipped) == 8
    assert {(r["item"], r["rep"]) for r in skipped} == set(failed)
    assert all(r["calls"] == 0 and r["answer"] is None for r in skipped)
    assert summary["cells"]["verify/verify_v1/sonnet"]["no_draft"] == 1
    assert summary["cells"]["verify/verify_v1/sonnet"]["n"] == 14


def test_no_search_on_a_web_item_is_failed(tmp_path):
    clock = Clock()
    _m_run(tmp_path, clock=clock, or_fake=OrFake(clock, no_search_items=("t1", "t5")))
    rows = _rows(tmp_path)
    single = {r["item"]: r for r in rows if r["cell"] == "single/web_v1/gemini"}
    assert single["t1"]["outcome"] == "failed" and single["t1"]["answer"] is None
    assert single["t5"]["outcome"] == "no_search"
    assert single["t5"]["answer"] == SECRET_A         # the reader still gets the text
    v = {r["item"]: r for r in rows if r["cell"] == "verify/verify_v2/gemini"}
    assert v["t1"]["outcome"] == "failed"
    assert v["t1"]["answer_source"] == "draft" and "[unverified]" in v["t1"]["answer"]
    assert v["t2"]["answer_source"] == "verify"


def test_matrix_pool_exhausted_mid_cell_stops_and_writes_summary(tmp_path):
    lg = ledger.Ledger(str(tmp_path / "anthropic_ledger.json"))
    lg.add(1.42)
    lg.add(17.99, pool="matrix")
    clock = Clock()
    client = ClaudeFake(clock, usage=(100_000, 1000, 1))        # ~$0.01 per Haiku draft
    code, summary, text, client, or_fake = _m_run(tmp_path, clock=clock, client=client)
    assert code == 3 and summary["hard_stop"] is True
    assert len(client.requests) == 1
    assert len(or_fake.calls) == 165                  # OpenRouter cells ran first
    s = json.loads((tmp_path / "matrix_r0_summary.json").read_text("utf-8"))
    assert s["hard_stop"] is True
    assert ledger.Ledger(str(tmp_path / "anthropic_ledger.json")).room() > \
        ledger.SCORED_RESERVE


def test_scored_reserve_is_never_touched(tmp_path):
    ledger.Ledger(str(tmp_path / "anthropic_ledger.json")).add(22.0)   # room = 23
    code, summary, _, client, _ = _m_run(tmp_path)
    assert code == 3 and client.requests == []


def test_no_question_or_answer_text_in_stdout_or_summary(tmp_path):
    code, summary, text, _, _ = _m_run(tmp_path)
    blob = text + json.dumps(summary) + \
        (tmp_path / "matrix_r0_summary.json").read_text("utf-8")
    assert SECRET_Q not in blob and SECRET_A not in blob and "DRAFT<" not in blob
    calls = (tmp_path / "matrix_r0_calls.jsonl").read_text("utf-8")
    assert SECRET_A in calls
