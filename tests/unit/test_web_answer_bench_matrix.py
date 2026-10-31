"""Web-answer bench, prompt x model matrix (plan 2026-10-28-web-answer-prompt-matrix).

Fakes only: no SDK, no network. The modules are loaded by path because the
directory name has a hyphen.
"""
import importlib.util
import json
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


CANDIDATES = ("deepseek", "muse", "sonnet", "haiku")


def test_round0_cells_and_order():
    # Spec 2026-10-31 Part A: gemini only on the shipped draft_v1 / verify_v2.
    cells = matrix.round0_cells()
    assert len(cells) == 22 and len(set(cells)) == 22
    assert cells[0] == ("draft", "draft_v1", "gemini")
    assert sorted(c for c in cells if c[0] == "draft") == sorted(
        ("draft", "draft_v1", m) for m in ("gemini",) + CANDIDATES)
    assert sorted(c for c in cells if c[0] == "verify") == sorted(
        [("verify", p, m) for p in ("verify_v1", "verify_v2") for m in CANDIDATES]
        + [("verify", "verify_v2", "gemini")])
    assert sorted(c for c in cells if c[0] == "single") == sorted(
        ("single", p, m) for p in ("web_v1", "merged_v1") for m in CANDIDATES)
    assert sorted(c for c in cells if c[2] == "gemini") == [
        ("draft", "draft_v1", "gemini"), ("verify", "verify_v2", "gemini")]
    assert not any(c[2] == "mimo" for c in cells)
    vendor = [{"sonnet": 2, "haiku": 1}.get(m, 0) for _, _, m in cells]
    assert vendor == sorted(vendor)             # OpenRouter, then Haiku, then Sonnet


def test_plan_hand_count():
    # Cells 5 + 1 + 8 + 8 = 22, runs 22 x 15 = 330.
    # Anthropic: drafts 2 x 15 x 1, verify 4 x 15 x 2, single 4 x 15 x 2 = 270.
    # OpenRouter: drafts 3 x 15 = 45, verify (gemini v2 + deepseek / muse x v1, v2)
    # 5 x 15 x 2 (retry) = 150, single 4 x 15 = 60 -> 255. Total 525.
    # $: Sonnet 135 calls x (46k x $2 + 4096 x $10)/M = 17.9496; Haiku 135 x
    # (46k x $0.1 + 4096 x $0.5)/M = 0.89748; searches 120 x 3 + 120 x 2 = 600 x $0.01.
    p = matrix.plan(matrix.round0_cells(), reps=3, price_per_1000=10.0)
    assert (p["cells"], p["runs"], p["anthropic_upper"], p["openrouter_upper"]) == \
        (22, 330, 270, 255)
    assert p["upper"] == 525 and p["searches_upper"] == 600
    assert p["anthropic_usd_upper"] == pytest.approx(17.9496 + 0.89748 + 6.0, abs=1e-4)
    # OpenRouter at the pinned prices, 46k input, max_tokens per step:
    # gemini 15 x 1024 + 30 x 2048 out; deepseek / muse 15 x 1024 + 60 x 2048 + 30 x 2048.
    gem = (45 * 46_000 * 0.825 + (15 * 1024 + 30 * 2048) * 4.125) / 1e6
    ds = (105 * 46_000 * 0.13 + (15 * 1024 + 90 * 2048) * 0.65) / 1e6
    mu = (105 * 46_000 * 0.10 + (15 * 1024 + 90 * 2048) * 0.20) / 1e6
    # + the web plugin fee on every verify / single call: 150 + 60 = 210 x $0.02.
    fee = 210 * 0.02
    assert p["openrouter_usd_upper"] == pytest.approx(gem + ds + mu + fee, abs=1e-5)
    assert ledger.WEB_PLUGIN_USD_PER_CALL_2026_10_31 == pytest.approx(0.02)


def _m_argv(out, *extra):
    return ["--out", str(out), "--round", "0", "--search-price-per-1000", "10", *extra]


def test_validate_confirm_and_out(tmp_path):
    with pytest.raises(SystemExit) as e:
        matrix.validate(_m_argv(tmp_path, "--confirm-calls", "524"), KEYS, REPO)
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        matrix.validate(_m_argv(tmp_path, "--confirm-calls", "525"), {}, REPO)
    with pytest.raises(SystemExit):
        matrix.validate(_m_argv(os.path.join(REPO, "x"), "--dry-run"), {}, REPO)
    assert matrix.validate(_m_argv(tmp_path, "--confirm-calls", "525"), KEYS, REPO)
    assert matrix.validate(_m_argv(tmp_path, "--dry-run"), {}, REPO).dry_run


def test_key_file_replaces_the_env_key(tmp_path):
    kf = tmp_path / "or.key"
    kf.write_text("sk-or-bench", encoding="utf-8")
    only_anthropic = {"ANTHROPIC_API_KEY": "sk-ant-x"}
    with pytest.raises(SystemExit):
        matrix.validate(_m_argv(tmp_path / "o", "--confirm-calls", "525"), only_anthropic, REPO)
    a = matrix.validate(_m_argv(tmp_path / "o", "--confirm-calls", "525",
                                "--openrouter-key-file", str(kf)), only_anthropic, REPO)
    assert a.openrouter_key_file == str(kf) and a.openrouter_cap == 10.0
    with pytest.raises(SystemExit):
        matrix.validate(_m_argv(tmp_path / "o", "--dry-run", "--openrouter-key-file",
                                str(tmp_path / "nope.key")), {}, REPO)


def test_out_with_rows_of_a_removed_cell_is_refused(tmp_path, capsys):
    rows = [{"run_id": "r0-0001", "cell": "draft/draft_v1/mimo", "item": "t1", "rep": 0},
            {"run_id": "r0-0002", "cell": "verify/verify_v1/gemini", "item": "t1", "rep": 0},
            {"run_id": "r0-0003", "cell": "draft/draft_v1/gemini", "item": "t1", "rep": 0}]
    (tmp_path / "matrix_r0_calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + '{"torn', encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        matrix.validate(_m_argv(tmp_path, "--dry-run"), {}, REPO)
    assert e.value.code == 2
    assert "2 rows" in capsys.readouterr().err
    assert len(matrix.stale_rows(rows)) == 2


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
                                               ["--confirm-calls", "525"])), KEYS, REPO)
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
    assert "330" in text and "525" in text and "$24.85" in text
    assert "openrouter: upper $7.51" in text and "room $10.00" in text
    assert not (tmp_path / "matrix_r0_calls.jsonl").exists()


def test_openrouter_shortfall_with_claude_work_left_is_not_refused(tmp_path):
    ledger.add_openrouter_spend(str(tmp_path / "anthropic_ledger.json"), 7.0)
    code, summary, text, client, or_fake = _m_run(tmp_path, dry=True)
    assert code == 0 and "shortfall $4.51" in text and "Claude cells still run" in text
    assert client.requests == [] and or_fake.calls == []


def test_dry_run_refuses_when_only_openrouter_work_is_left_over_the_room(tmp_path):
    ledger.add_openrouter_spend(str(tmp_path / "anthropic_ledger.json"), 7.0)
    rows = [{"run_id": f"r0-{n:04d}", "cell": matrix._cell_id(c), "item": it["id"],
             "rep": rep, "outcome": "ok"}
            for n, (c, rep, it) in enumerate(
                ((c, rep, it) for c in matrix.round0_cells() if c[2] in matrix.CLAUDE
                 for rep in range(3) for it in _m_items()), start=1)]
    (tmp_path / "matrix_r0_calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    code, summary, text, client, or_fake = _m_run(tmp_path, dry=True)
    assert code == 2 and summary["refused"] == "openrouter_cap"
    assert "$7.51 > room $3.00" in text


def test_openrouter_spend_goes_to_its_pool_only(tmp_path):
    code, summary, _, client, or_fake = _m_run(tmp_path)
    assert code == 0
    st = ledger.read_state(str(tmp_path / "anthropic_ledger.json"))
    assert st["pools"]["openrouter"] == pytest.approx(0.001 * len(or_fake.calls))
    claude = sum(r["cost_usd"] for r in _rows(tmp_path) if r["model_key"] in matrix.CLAUDE)
    assert st["spent_usd"] == pytest.approx(claude)            # Anthropic only
    assert ledger.global_spent(st) == pytest.approx(claude)
    assert summary["openrouter_spent_usd"] == pytest.approx(0.001 * len(or_fake.calls))


def test_openrouter_pool_spent_stops_the_openrouter_cells(tmp_path):
    clock = Clock()
    lp = str(tmp_path / "anthropic_ledger.json")

    class Spender(OrFake):
        def __call__(self, *a, **kw):
            r = super().__call__(*a, **kw)
            if len(self.calls) == 10:
                ledger.add_openrouter_spend(lp, 9.99)     # the pool is now $10.00
            return r
    args = matrix.validate(_m_argv(tmp_path, "--confirm-calls", "525"), KEYS, REPO)
    or_fake = Spender(clock)
    code, summary = matrix.run(args, items=_m_items(), client=ClaudeFake(clock),
                               or_call=or_fake, clock=clock, stdout=io.StringIO())
    assert code == 3 and summary["openrouter_cap_hit"] is True
    assert len(or_fake.calls) == 10                    # 10 of the 15 gemini drafts
    assert ledger.openrouter_spent(lp) == pytest.approx(10.0)
    # Claude cells keep running; only the verifies of the 5 cut drafts are skipped.
    rows = _rows(tmp_path)
    claude = [r for r in rows if r["model_key"] in matrix.CLAUDE]
    assert len(claude) == 150 - 4 * 5
    assert not any(r["outcome"] == "no_draft" for r in rows)   # skipped, not recorded
    cut = set(summary["cut_by_pool"])
    assert {matrix._cell_id(c) for c in matrix.round0_cells() if c[2] not in matrix.CLAUDE} <= cut
    assert "verify/verify_v1/sonnet" in cut and "draft/draft_v1/sonnet" not in cut


def test_full_round0_counts_order_and_ledger(tmp_path):
    code, summary, _, client, or_fake = _m_run(tmp_path)
    assert code == 0
    rows = _rows(tmp_path)
    assert len(rows) == 330
    assert [r["cell"] for r in rows[:15]] == ["draft/draft_v1/gemini"] * 15
    assert len({r["run_id"] for r in rows}) == 330
    assert all(not any(m in r["run_id"] for m in matrix.MODEL_KEYS) for r in rows)
    assert len(client.requests) == 150 and len(or_fake.calls) == 180
    assert {r["model_key"] for r in rows} == {"gemini", "deepseek", "muse", "sonnet", "haiku"}
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
    assert len(verify) == 135
    for r in verify:
        # The echo ends with the prompt, and the prompt ends with the draft.
        assert [k for k, d in drafts.items() if r["text"].rstrip().endswith(d)] == \
            [(r["item"], r["rep"])]
        assert r["draft_ms"] == 3000


def test_verify_budget_is_the_chain_left_after_the_draft(tmp_path):
    _, _, _, client, or_fake = _m_run(tmp_path)
    v = [c for c in or_fake.calls if c["web"] and _is_verify_prompt(c["prompt"])]
    assert len(v) == 75
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
    assert len(skipped) == 9
    assert {(r["item"], r["rep"]) for r in skipped} == set(failed)
    assert all(r["calls"] == 0 and r["answer"] is None for r in skipped)
    assert summary["cells"]["verify/verify_v1/sonnet"]["no_draft"] == 1
    assert summary["cells"]["verify/verify_v1/sonnet"]["n"] == 14


def test_no_search_on_a_web_item_is_failed(tmp_path):
    clock = Clock()
    _m_run(tmp_path, clock=clock, or_fake=OrFake(clock, no_search_items=("t1", "t5")))
    rows = _rows(tmp_path)
    drafts = {(r["item"], r["rep"]): r["text"] for r in rows
              if r["cell"] == "draft/draft_v1/gemini"}
    single = {r["item"]: r for r in rows if r["cell"] == "single/web_v1/deepseek"}
    t1 = single["t1"]
    assert t1["outcome"] == "failed" and t1["fallback"] is True
    assert t1["answer_source"] == "fallback"
    assert t1["answer"] == drafts[("t1", t1["rep"])] + "\n\n[unverified]"
    assert single["t5"]["outcome"] == "no_search"
    assert single["t5"]["answer"] == SECRET_A         # declining on an unsure item
    assert single["t5"]["fallback"] is False
    v = {r["item"]: r for r in rows if r["cell"] == "verify/verify_v2/gemini"}
    assert v["t1"]["outcome"] == "failed"
    assert v["t1"]["answer_source"] == "draft" and "[unverified]" in v["t1"]["answer"]
    assert v["t1"]["fallback"] is True and v["t2"]["fallback"] is False
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
    assert len(or_fake.calls) == 180                  # OpenRouter cells ran first
    s = json.loads((tmp_path / "matrix_r0_summary.json").read_text("utf-8"))
    assert s["hard_stop"] is True
    claude_cells = [matrix._cell_id(c) for c in matrix.round0_cells()
                    if c[2] in matrix.CLAUDE]
    assert s["cut_by_pool"] == claude_cells
    assert "cut by pool: " + ", ".join(claude_cells) in text
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


# ------------------------------------------------------- Task 6: blind grader

grade = _load("grade")

GOLD = {"expected": ["GOLD-FACT"], "variants": ["GOLD-VARIANT"], "kind": "standard"}


def test_blind_strips_markers_and_links():
    raw = ("Clause E3 [unverified] applies [MBIE](https://www.building.govt.nz/e3) and "
           "see https://www.legislation.govt.nz/act/x?y=1. [Unverified]")
    b = grade.blind(raw)
    assert "unverified" not in b.lower()
    assert "](" not in b and "http" not in b
    assert "MBIE" in b and "legislation.govt.nz" in b


def test_source_flag_by_domain():
    answer = "See https://www.fake-standards.co.nz/x and [B](https://building.govt.nz/a)."
    f = grade.source_flag(answer, ["building.govt.nz"])
    assert f == ["fake-standards.co.nz"]
    assert grade.source_flag(answer, ["building.govt.nz", "fake-standards.co.nz"]) == []
    # A literal that is exactly a result URL is in the set even when its host is not
    # (gemini's grounding redirects, spec §5.2).
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
    assert grade.source_flag("x " + redirect, ["building.govt.nz"], urls=[redirect]) == []
    assert grade.source_flag("x " + redirect, ["building.govt.nz"]) == \
        ["vertexaisearch.cloud.google.com"]


class GraderFake:
    def __init__(self, replies=None, default='{"label": "correct"}'):
        self.replies, self.default = list(replies or []), default
        self.prompts = []

    def __call__(self, profile, prompt, *, web, timeout, max_tokens=1024, **kw):
        assert profile == "grader" and web is False
        self.prompts.append(prompt)
        text = self.replies.pop(0) if self.replies else self.default
        return {"text": text, "outcome": "ok" if text else "failed", "calls": 1,
                "latency_ms": 900, "http_status": 200, "openrouter_cost_usd": 0.0001}


def test_grade_one_labels_and_ungraded():
    for reply, label, ok in (('{"label": "partial"}', "partial", True),
                             ('```json\n{"label": "wrong"}\n```', "wrong", True),
                             ("The answer is correct.", "ungraded", False),
                             ('{"label": "excellent"}', "ungraded", False),
                             ('{"label": "hallucinated_source"}', "ungraded", False),
                             ("", "ungraded", False)):
        fake = GraderFake([reply])
        g = grade.grade_one("Q?", GOLD, "an answer", ["building.govt.nz"], call=fake)
        assert (g["label"], g["raw_ok"]) == (label, ok), reply


def test_grade_one_prompt_is_blind_and_carries_gold_and_flag():
    fake = GraderFake()
    g = grade.grade_one("Q?", GOLD, "Fact [unverified] https://www.fake.co.nz/p",
                        ["building.govt.nz"], call=fake)
    p = fake.prompts[0]
    assert "GOLD-FACT" in p and "GOLD-VARIANT" in p
    assert "[unverified]" not in p and "https://" not in p.split("## Answer")[1]
    assert "hallucinated_source" not in p
    # Flagged mechanically for the owner; the grader's label is not downgraded.
    assert g["label"] == "correct"
    assert g["source_flag"] is True and g["flagged_hosts"] == ["fake.co.nz"]
    g = grade.grade_one("Q?", GOLD, "Fact https://www.fake.co.nz/p", ["building.govt.nz"],
                        call=GraderFake(), check_sources=False)
    assert g["source_flag"] is False and g["source_checked"] is False


def test_grader_template_formats():
    t = prompts.load("grader_v1")
    filled = t.format(question="Q", kind="unsure", expected="E", variants="V", answer="A")
    assert '{"label"' in filled
    for label in grade.GRADER_LABELS:
        assert label in t
    assert "hallucinated_source" not in t
    assert set(grade.GRADER_LABELS) == set(grade.ORDER) - {"hallucinated_source"}


def test_majority_worst_stable():
    m = grade.majority
    assert m(["correct", "correct", "wrong"]) == ("correct", False)
    assert m(["correct", "correct", "correct"]) == ("correct", True)
    assert m(["correct", "partial", "wrong"]) == ("wrong", False)       # no majority
    assert m(["correctly_unsure", "partial", "hallucinated_source"]) == \
        ("hallucinated_source", False)
    assert m(["correct", "no_answer", "partial"]) == ("no_answer", False)
    assert m(["correct", "correct", "ungraded"]) == ("correct", False)
    assert m(["correct", "wrong", "ungraded"]) == ("wrong", False)
    assert m(["ungraded"] * 3) == ("ungraded", False)
    assert grade.ORDER == ["wrong", "hallucinated_source", "partial", "correctly_unsure",
                           "correct"]


def _calls_file(tmp_path, n_cells=2):
    rows, seq = [], 0
    for c in range(n_cells):
        model = ("deepseek", "sonnet")[c % 2]
        for rep in range(3):
            for i in range(1, 6):
                seq += 1
                rows.append({"run_id": f"r0-{seq:04d}", "cell": f"single/web_v1/{model}",
                             "step": "single", "prompt_id": "web_v1", "model_key": model,
                             "item": f"t{i}", "rep": rep, "outcome": "ok", "calls": 1,
                             "latency_ms": 1000 * (c + 1),
                             "answer": f"{SECRET_A}-{seq} https://building.govt.nz/a",
                             "result_domains": ["building.govt.nz"], "result_urls": []})
    rows[0].update(outcome="failed", answer=None)
    rows[1].update(outcome="no_draft", answer=None, calls=0)
    p = tmp_path / "matrix_r0_calls.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return rows


def _gold_file(tmp_path):
    p = tmp_path / "set.jsonl"
    p.write_text("".join(json.dumps({"id": f"t{i}", "split": "tune", "question":
                                     f"{SECRET_Q}-{i}", "expected": ["E"], "variants": [],
                                     "kind": "unsure" if i == 5 else "standard"}) + "\n"
                         for i in range(1, 6)), encoding="utf-8")
    return str(p)


def _g_argv(tmp_path, *extra):
    return ["--out", str(tmp_path), "--calls", "matrix_r0_calls.jsonl", "--seed", "7",
            "--set", _gold_file(tmp_path), *extra]


def test_grade_dry_run_counts_answers_no_calls(tmp_path):
    _calls_file(tmp_path)
    args = grade.validate(_g_argv(tmp_path, "--dry-run"), {}, REPO)
    fake = GraderFake()
    out = io.StringIO()
    code, info = grade.run(args, call=fake, stdout=out)
    assert code == 0 and fake.prompts == []
    assert info["to_grade"] == 28 and info["no_answer"] == 1 and info["skipped"] == 1
    assert "28" in out.getvalue()


def test_grade_refuses_without_calls_file_or_wrong_confirm(tmp_path):
    with pytest.raises(SystemExit) as e:
        grade.validate(_g_argv(tmp_path, "--dry-run"), {}, REPO)
    assert e.value.code == 2
    _calls_file(tmp_path)
    with pytest.raises(SystemExit):
        grade.validate(_g_argv(tmp_path, "--confirm-calls", "27"),
                       {"CORROBORATION_API_KEY": "k"}, REPO)
    with pytest.raises(SystemExit):
        grade.validate(_g_argv(tmp_path, "--confirm-calls", "28"), {}, REPO)


def test_grade_live_writes_grades_and_cells_blind_and_reproducible(tmp_path):
    rows = _calls_file(tmp_path)
    args = grade.validate(_g_argv(tmp_path, "--confirm-calls", "28"),
                          {"CORROBORATION_API_KEY": "k"}, REPO)
    fake = GraderFake()
    out = io.StringIO()
    code, _ = grade.run(args, call=fake, stdout=out)
    assert code == 0 and len(fake.prompts) == 28
    for p in fake.prompts:
        for leak in ("deepseek", "sonnet", "web_v1", "single/", "r0-"):
            assert leak not in p
    order1 = [p.split(SECRET_A + "-")[1].split()[0] for p in fake.prompts]
    assert order1 != sorted(order1, key=int)          # shuffled
    fake2 = GraderFake()
    grade.run(args, call=fake2, stdout=io.StringIO())
    assert fake2.prompts == fake.prompts              # same seed, same order
    grades = [json.loads(l) for l in open(tmp_path / "grades_r0.jsonl", encoding="utf-8")]
    by_id = {g["run_id"]: g["label"] for g in grades}
    assert by_id[rows[0]["run_id"]] == "no_answer" and rows[1]["run_id"] not in by_id
    cells = json.loads((tmp_path / "cells_r0.json").read_text("utf-8"))
    c = cells["single/web_v1/deepseek"]
    assert (c["step"], c["prompt_id"], c["model_key"], c["p50_ms"]) == \
        ("single", "web_v1", "deepseek", 1000)
    assert c["items"]["t1"]["labels"] == ["no_answer", "correct", "correct"]
    assert (c["items"]["t1"]["majority"], c["items"]["t1"]["stable"]) == ("correct", False)
    assert c["items"]["t2"]["labels"] == ["correct", "correct"]     # no_draft: not a label
    assert c["items"]["t2"]["stable"] is False
    assert c["items"]["t3"] == {"labels": ["correct"] * 3, "majority": "correct",
                                "stable": True, "kind": "standard"}
    assert c["items"]["t5"]["kind"] == "unsure"
    blob = out.getvalue() + (tmp_path / "grades_r0.jsonl").read_text("utf-8") + \
        (tmp_path / "cells_r0.json").read_text("utf-8")
    assert SECRET_A not in blob and SECRET_Q not in blob


# ------------------------------------- Task 7: keep rule, freeze, spot-check sheet

sf = _load("select_freeze")


def _cell(pid, majors, *, p50=5000, stable=True, step="verify", model="sonnet",
          kinds=None):
    return {"step": step, "prompt_id": pid, "model_key": model, "p50_ms": p50,
            "items": {f"t{i + 1}": {"labels": [m] * 3, "majority": m,
                                    "stable": stable if isinstance(stable, bool)
                                    else stable[i],
                                    "kind": (kinds or {}).get(f"t{i + 1}", "standard")}
                      for i, m in enumerate(majors)}}


def _cells(*cells):
    return {f"{c['step']}/{c['prompt_id']}/{c['model_key']}": c for c in cells}


def test_best_round0_most_good_items():
    cells = _cells(
        _cell("verify_v1", ["correct", "correct", "partial", "wrong", "correctly_unsure"]),
        _cell("verify_v2", ["correct", "correct", "correct", "wrong", "correctly_unsure"]),
        _cell("verify_v2", ["correct"] * 5, model="haiku"),
        _cell("web_v1", ["correct"] * 5, step="single"))
    assert sf.best_round0(cells, "verify", "sonnet") == "verify_v2"


def test_best_round0_ties_fewer_wrong_then_faster():
    a = _cell("verify_v1", ["correct", "correct", "wrong", "partial", "partial"], p50=4000)
    b = _cell("verify_v2", ["correct", "correct", "partial", "partial", "partial"], p50=9000)
    assert sf.best_round0(_cells(a, b), "verify", "sonnet") == "verify_v2"
    c = _cell("verify_v1", ["correct", "correct", "partial", "partial", "partial"], p50=4000)
    assert sf.best_round0(_cells(c, b), "verify", "sonnet") == "verify_v1"
    with pytest.raises(KeyError):
        sf.best_round0(_cells(c), "single", "mimo")


def test_keep_edit_branches():
    inc = _cell("verify_v2", ["correct", "partial", "partial", "wrong", "correctly_unsure"],
                stable=[True, True, False, True, True])
    # (a) an item worse -> rejected, even with another better.
    worse = _cell("sonnet_verify_v3",
                  ["partial", "correct", "partial", "wrong", "correctly_unsure"])
    assert sf.keep_edit(inc, worse) is False
    # (b) nothing better -> rejected.
    same = _cell("sonnet_verify_v3", ["correct", "partial", "partial", "wrong",
                                      "correctly_unsure"])
    assert sf.keep_edit(inc, same) is False
    # better only where the incumbent was unstable -> rejected (noise).
    noisy = _cell("sonnet_verify_v3", ["correct", "partial", "correct", "wrong",
                                       "correctly_unsure"])
    assert sf.keep_edit(inc, noisy) is False
    # better on a stable item, nothing worse -> kept.
    better = _cell("sonnet_verify_v3", ["correct", "correct", "partial", "partial",
                                        "correctly_unsure"])
    assert sf.keep_edit(inc, better) is True
    # an item the edit could not grade is not evidence it is no worse.
    missing = _cell("sonnet_verify_v3", ["correct", "correct", "partial", "wrong",
                                         "ungraded"])
    assert sf.keep_edit(inc, missing) is False


def test_write_frozen_entries_and_merge(tmp_path):
    path = str(tmp_path / "frozen.v1.json")
    params = sf.params_for("verify", "sonnet")
    assert params["model"] == "claude-sonnet-5-5" and params["max_uses"] == 3
    assert params["tool"] == "web_search_20250305"
    labels = {"t1": "correct", "t2": "partial"}
    sf.write_frozen(path, [{"step": "verify", "model": "sonnet", "prompt_id": "verify_v2",
                            "params": params, "round": 0, "tune_labels": labels,
                            "evidential": True}])
    data = json.loads(open(path, encoding="utf-8").read())
    (e,) = data["entries"]
    assert set(e) == {"step", "model", "prompt_id", "text", "sha256", "params", "round",
                      "tune_labels", "evidential"}
    assert e["evidential"] is False                  # winner's curse: never evidence
    assert e["text"] == prompts.load("verify_v2") and e["sha256"] == prompts.sha("verify_v2")
    sf.write_frozen(path, [{"step": "draft", "model": "gemini", "prompt_id": "draft_v1",
                            "params": sf.params_for("draft", "gemini"), "round": 0,
                            "tune_labels": {}}])
    data = json.loads(open(path, encoding="utf-8").read())
    assert {(x["step"], x["model"]) for x in data["entries"]} == \
        {("verify", "sonnet"), ("draft", "gemini")}
    g = [x for x in data["entries"] if x["model"] == "gemini"][0]
    assert g["params"]["provider"] == \
        corroboration_client.provider_preference("google/gemini-3.8-flash")
    assert g["params"]["web"] is False and g["params"]["reasoning"] == {"effort": "low"}


def _sheet_rows():
    rows = []
    for c, model in enumerate(("gemini", "sonnet", "haiku", "deepseek")):
        for i in range(1, 6):
            rows.append({"run_id": f"r0-{c * 5 + i:04d}",
                         "cell": f"verify/verify_v2/{model}",
                         "question": f"{SECRET_Q}-{i}", "gold": ["GOLD-FACT"],
                         "answer": f"{SECRET_A} [unverified] https://x.govt.nz/p",
                         "label": "hallucinated_source", "model_key": model})
    return rows


def test_spot_check_sheet_is_blind():
    md = sf.spot_check_sheet(_sheet_rows(), n=10, seed=3)
    table = [l for l in md.splitlines() if l.startswith("| r0-")]
    assert len(table) == 10
    for line in table:
        assert line.rstrip().endswith("|  |")         # blank owner label column
        for leak in ("hallucinated_source", "gemini", "sonnet", "haiku", "deepseek",
                     "verify_v2", "[unverified]", "https://"):
            assert leak not in line
        assert SECRET_Q in line and "GOLD-FACT" in line
    assert md == sf.spot_check_sheet(_sheet_rows(), n=10, seed=3)
    assert md != sf.spot_check_sheet(_sheet_rows(), n=10, seed=4)
    # Drawn across cells: 10 answers from 4 cells means every cell is in it.
    ids = {line.split("|")[1].strip() for line in table}
    cells = {r["cell"] for r in _sheet_rows() if r["run_id"] in ids}
    assert len(cells) == 4


def test_live_matrix_refuses_unconfirmed_tune_gold(tmp_path):
    rows = [{"id": f"t{i}", "split": "tune", "question": "q", "confirmed": i != 3}
            for i in range(1, 6)]
    gold = tmp_path / "set.jsonl"
    gold.write_text("".join(json.dumps(r) + chr(10) for r in rows), encoding="utf-8")
    assert matrix.unconfirmed_tune(str(gold)) == ["t3"]
    with pytest.raises(SystemExit):
        matrix.validate(_m_argv(tmp_path / "o", "--confirm-calls", "525", "--set", str(gold)),
                        KEYS, REPO)
    assert matrix.validate(_m_argv(tmp_path / "o", "--dry-run", "--set", str(gold)), {}, REPO)


# ------------------------------------------------------- review fixes (10-28)

class GraderFake429(GraderFake):
    def __init__(self, statuses):
        super().__init__()
        self.statuses = list(statuses)

    def __call__(self, profile, prompt, **kw):
        r = super().__call__(profile, prompt, **kw)
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            r.update(outcome="http_error", http_status=status, text="")
        return r


def test_grader_429_retried_once_after_two_seconds():
    slept = []
    fake = GraderFake429([429])
    g = grade.grade_one("Q?", GOLD, "a", [], call=fake, sleep=slept.append)
    assert g["label"] == "correct" and len(fake.prompts) == 2 and slept == [2.0]
    fake = GraderFake429([429, 429])
    g = grade.grade_one("Q?", GOLD, "a", [], call=fake, sleep=slept.append)
    assert g["label"] == "ungraded" and len(fake.prompts) == 2
    fake = GraderFake429([500])
    assert grade.grade_one("Q?", GOLD, "a", [], call=fake, sleep=slept.append)["label"] == \
        "ungraded"
    assert len(fake.prompts) == 1


def test_draft_answers_are_never_source_checked(tmp_path):
    rows = _calls_file(tmp_path)
    bad = "https://www.not-retrieved.co.nz/p"
    rows[2].update(step="draft", answer=f"{SECRET_A} {bad}")
    rows[3].update(answer_source="fallback", answer=f"{SECRET_A} {bad}")
    rows[4].update(answer_source="draft", answer=f"{SECRET_A} {bad}")
    rows[5].update(answer_source="model", answer=f"{SECRET_A} {bad}")
    (tmp_path / "matrix_r0_calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    args = grade.validate(_g_argv(tmp_path, "--confirm-calls", "28"),
                          {"CORROBORATION_API_KEY": "k"}, REPO)
    grade.run(args, call=GraderFake(), stdout=io.StringIO())
    g = {x["run_id"]: x for x in map(json.loads, open(tmp_path / "grades_r0.jsonl",
                                                       encoding="utf-8"))}
    for r in rows[2:5]:
        assert g[r["run_id"]]["source_flag"] is False
        assert g[r["run_id"]]["source_checked"] is False
    assert g[rows[5]["run_id"]]["source_flag"] is True
    assert g[rows[5]["run_id"]]["label"] == "correct"


def test_only_ungraded_regrades_and_merges(tmp_path):
    rows = _calls_file(tmp_path)
    args = grade.validate(_g_argv(tmp_path, "--confirm-calls", "28"),
                          {"CORROBORATION_API_KEY": "k"}, REPO)
    grade.run(args, call=GraderFake(replies=["nonsense"] * 3), stdout=io.StringIO())
    before = [json.loads(l) for l in open(tmp_path / "grades_r0.jsonl", encoding="utf-8")]
    ungraded = {g["run_id"] for g in before if g["label"] == "ungraded"}
    assert len(ungraded) == 3
    with pytest.raises(SystemExit):
        grade.validate(_g_argv(tmp_path, "--only-ungraded", "--confirm-calls", "28"),
                       {"CORROBORATION_API_KEY": "k"}, REPO)
    args = grade.validate(_g_argv(tmp_path, "--only-ungraded", "--confirm-calls", "3"),
                          {"CORROBORATION_API_KEY": "k"}, REPO)
    fake = GraderFake()
    code, info = grade.run(args, call=fake, stdout=io.StringIO())
    assert code == 0 and len(fake.prompts) == 3
    after = [json.loads(l) for l in open(tmp_path / "grades_r0.jsonl", encoding="utf-8")]
    assert len(after) == len(before) == 29
    assert len({g["run_id"] for g in after}) == 29
    assert all(g["label"] != "ungraded" for g in after)
    kept = {g["run_id"]: g for g in before if g["run_id"] not in ungraded}
    assert all(kept[g["run_id"]] == g for g in after if g["run_id"] in kept)
    cells = json.loads((tmp_path / "cells_r0.json").read_text("utf-8"))
    assert cells["single/web_v1/deepseek"]["items"]["t3"]["labels"] == ["correct"] * 3


class Boom(Exception):
    pass


class OrFakeDies(OrFake):
    def __init__(self, clock, after):
        super().__init__(clock)
        self.after = after

    def __call__(self, *a, **kw):
        if len(self.calls) >= self.after:
            raise Boom("killed")
        return super().__call__(*a, **kw)


def test_rerun_resumes_without_repeating_paid_calls(tmp_path):
    clock = Clock()
    with pytest.raises(Boom):
        _m_run(tmp_path, clock=clock, or_fake=OrFakeDies(clock, after=50))
    first = _rows(tmp_path)
    assert len(first) == 50
    clock = Clock()
    or2 = OrFake(clock)
    or2.n_drafts = 100                    # a re-drafted text would show up as DRAFT<101>
    code, summary, _, client, _ = _m_run(tmp_path, clock=clock, or_fake=or2)
    assert code == 0
    rows = _rows(tmp_path)
    assert len(rows) == 330
    assert rows[:50] == first                         # paid records kept, untouched
    assert len({(r["cell"], r["item"], r["rep"]) for r in rows}) == 330
    assert len({r["run_id"] for r in rows}) == 330
    assert len(or2.calls) == 180 - 50 and len(client.requests) == 150
    assert or2.n_drafts == 100                        # no gemini draft re-run
    drafts = {(r["item"], r["rep"]): r["text"] for r in rows
              if r["cell"] == "draft/draft_v1/gemini"}
    for r in rows:
        if r["step"] == "verify":
            assert r["text"].rstrip().endswith(drafts[(r["item"], r["rep"])])
    assert summary["cells"]["draft/draft_v1/gemini"]["n"] == 15


class ClaudeTimeout(ClaudeFake):
    def create(self, **kw):
        if kw["model"] == "claude-sonnet-5-5" and kw.get("tools"):
            self.requests.append(kw)
            raise APITimeoutError("t")
        return super().create(**kw)


class APITimeoutError(Exception):
    pass


def test_claude_timeout_is_booked_at_the_upper_cost(tmp_path):
    clock = Clock()
    code, summary, _, client, _ = _m_run(tmp_path, clock=clock, client=ClaudeTimeout(clock))
    rows = [r for r in _rows(tmp_path) if r["model_key"] == "sonnet" and r["step"] != "draft"]
    assert rows and all(r["outcome"] == "timeout" for r in rows)
    upper = ledger.call_cost("claude-sonnet-5-5", stage0.UPPER_INPUT_TOKENS,
                             stage0.UPPER_OUTPUT_TOKENS, 3, 10.0)
    assert all(r["cost_usd"] == pytest.approx(upper) and r["cost_estimated"] is True
               for r in rows)
    ok = [r for r in _rows(tmp_path) if r["model_key"] == "haiku"]
    assert all(r["cost_estimated"] is False for r in ok)
    lg = ledger.Ledger(str(tmp_path / "anthropic_ledger.json"))
    assert lg.pool_spent("matrix") >= len(rows) * upper


# --------------------------- spec 2026-10-31 Part A: OpenRouter pool, key, muse

def test_usage_include_is_sent_but_not_a_param(or_key):
    clock = Clock()
    http = FakeHttp(clock, [completion()])
    orb.call("deepseek", "P", web=True, timeout=14.0, http=http, clock=clock)
    sent = http.requests[0]["payload"]
    assert sent["usage"] == {"include": True}
    assert "usage" not in orb.build_payload("deepseek", "P", web=True, max_tokens=2048)
    sf_mod = _load("select_freeze")
    assert "usage" not in json.dumps(sf_mod.params_for("single", "deepseek"))


def test_missing_cost_is_charged_at_the_pinned_upper(or_key):
    clock = Clock()
    r = orb.call("deepseek", "P", web=False, timeout=14.0, clock=clock,
                 http=FakeHttp(clock, [completion(urls=(), usage={"prompt_tokens": 1000,
                                                                  "completion_tokens": 100})]))
    assert r["openrouter_cost_usd"] == pytest.approx((1000 * 0.13 + 100 * 0.65) / 1e6)
    assert r["cost_estimated"] is True
    r = orb.call("gemini", "P" * 500, web=False, timeout=14.0, max_tokens=1024, clock=clock,
                 http=FakeHttp(clock, [(14.0, Timeout("read timed out"))]))
    assert r["outcome"] == "timeout"
    assert r["openrouter_cost_usd"] == pytest.approx((500 * 0.825 + 1024 * 4.125) / 1e6)
    r = orb.call("gemini", "P", web=False, timeout=14.0, clock=clock,
                 http=FakeHttp(clock, [completion()]))
    assert r["openrouter_cost_usd"] == pytest.approx(0.0012) and r["cost_estimated"] is False


def test_explicit_key_wins_over_env(or_key):
    clock = Clock()
    http = FakeHttp(clock, [completion()])
    orb.call("gemini", "P", web=True, timeout=14.0, http=http, clock=clock,
             key="sk-or-BENCH")
    assert http.requests[0]["headers"]["Authorization"] == "Bearer sk-or-BENCH"


def test_payload_muse_reasoning_low_no_json():
    p = orb.build_payload("muse", "P", web=True, max_tokens=2048)
    assert p["model"] == "meta/muse-spark-1.3-contributor"
    assert p["reasoning"] == {"effort": "low"}
    assert "provider" not in p and "response_format" not in p
    assert p["plugins"] == corroboration_client.WEB_PLUGIN


def test_dry_run_never_prints_the_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CORROBORATION_API_KEY", OR_KEY)
    kf = tmp_path / "or.key"
    kf.write_text("sk-or-FILE-SENTINEL", encoding="utf-8")
    monkeypatch.setattr(stage0, "load_tune", lambda path: _m_items())
    for extra in ([], ["--openrouter-key-file", str(kf)]):
        assert matrix.main(_m_argv(tmp_path / "o", "--dry-run", *extra)) == 0
        cap = capsys.readouterr()
        assert "SENTINEL" not in cap.out + cap.err
        assert (ledger.PROD_KEY_WARNING in cap.err) == (not extra)


def test_grade_refuses_over_cap_and_rows_of_removed_cells(tmp_path):
    _calls_file(tmp_path)
    ledger.add_openrouter_spend(str(tmp_path / "anthropic_ledger.json"), 9.99)
    args = grade.validate(_g_argv(tmp_path, "--dry-run"), {}, REPO)
    out = io.StringIO()
    code, info = grade.run(args, call=GraderFake(), stdout=out)
    assert code == 2 and info["refused"] == "openrouter_cap"
    rows = _calls_file(tmp_path)
    rows[5]["cell"] = "draft/draft_v1/mimo"
    (tmp_path / "matrix_r0_calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        grade.validate(_g_argv(tmp_path, "--dry-run"), {}, REPO)
    assert e.value.code == 2


def test_grade_charges_the_openrouter_pool(tmp_path):
    _calls_file(tmp_path)
    args = grade.validate(_g_argv(tmp_path, "--confirm-calls", "28"),
                          {"CORROBORATION_API_KEY": "k"}, REPO)
    code, _ = grade.run(args, call=GraderFake(), stdout=io.StringIO())
    st = ledger.read_state(str(tmp_path / "anthropic_ledger.json"))
    assert code == 0 and st["pools"]["openrouter"] == pytest.approx(28 * 0.0001)
    assert st["spent_usd"] == 0.0


def test_web_call_without_cost_carries_the_plugin_fee(or_key):
    clock = Clock()
    r = orb.call("deepseek", "P", web=True, timeout=14.0, clock=clock,
                 http=FakeHttp(clock, [completion(usage={"prompt_tokens": 1000,
                                                         "completion_tokens": 100})]))
    assert r["openrouter_cost_usd"] == pytest.approx(
        (1000 * 0.13 + 100 * 0.65) / 1e6 + ledger.WEB_PLUGIN_USD_PER_CALL_2026_10_31)
