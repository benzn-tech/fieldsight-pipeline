"""Report bench, Task 10: runner with dry-run, confirm-calls and report pools."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "report-bench")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


run = _load("rb_run")
common = _load("rb_common")
ledger = _load("rb_ledger")
SECRET_A = "ANSWERSENTINEL"
SESSIONS = ["s1", "s2", "s3", "s4"]
DAYS = ["F_d1", "F_d2", "F_d3"]


def _index():
    idx = {}
    for cid in ("X-0901", "X-0917", "X-1005", "X-cur"):
        idx[cid] = {s: {"sha256": "h", "task": "X", "n": 10, "chars": 40000} for s in SESSIONS}
    for cid in ("T-0928", "T-1001", "T-cur"):
        idx[cid] = {s: {"sha256": "h", "task": "T", "n": 10, "chars": 30000} for s in SESSIONS}
    idx["R-cur"] = {d: {"sha256": "h", "task": "R", "n": 5, "chars": 60000} for d in DAYS}
    return idx


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")


def _setup(tmp_path, index=None):
    out = str(tmp_path / "out")
    index = index or _index()
    common.write_json(os.path.join(out, "prompts", "index.json"), index)
    for cid, keys in index.items():
        for k in keys:
            common.write_text(os.path.join(out, "prompts", cid, k + ".txt"), "PROMPT " + k)
    return out


def test_plan_counts_and_order():
    idx = _index()
    assert len(run.plan("pilot", ["X"], idx)) == 6
    assert {c["candidate"] for c in run.plan("pilot", ["X"], idx)} == {"X-cur"}
    calls = run.plan("round0", ["X", "T", "R"], idx)
    by = [c["task"] for c in calls]
    assert by.count("X") == 288 and by.count("T") == 216 and by.count("R") == 54
    assert by == sorted(by, key="XTR".index)
    assert len({c["key"] for c in calls}) == len(calls)
    assert all(c["pool"] == "report/round0" for c in calls)


def test_plan_skips_heldout_sessions_in_round0():
    idx = _index()
    manifest = {s: {"role": "set"} for s in SESSIONS[:3]}
    manifest["s4"] = {"role": "heldout"}
    manifest["R"] = {d: {} for d in DAYS}
    calls = run.plan("round0", ["X"], idx, manifest)
    assert "s4" not in {c["session"] for c in calls}


def test_usd_upper():
    up = run.usd_upper("T", "sonnet", 15000, 8000)
    assert up == pytest.approx((15000 * 2 + 16000 * 10) / 1e6 + (15000 * 2 + 20000 * 10) / 1e6)
    assert run.usd_upper("T", "muse", 15000, 8000) == pytest.approx(0.08)
    assert run.usd_upper("X", "muse", 15000, 8000) == pytest.approx(0.05)


def test_cjk_counts_one_token_per_character():
    assert common.est_tokens("abcd") == 2
    assert common.est_tokens("明天浇筑") == 4
    assert common.est_tokens(30000) == 15000


def test_heldout_r_day_is_planned():
    idx = _index()
    manifest = {s: {"role": "set"} for s in SESSIONS}
    manifest["R"] = {"F_d1": {"role": "set"}, "F_d2": {"role": "set"},
                     "F_d3": {"role": "heldout"}}
    keys = {c["session"] for c in run.plan("heldout", ["R"], idx, manifest,
                                          cells={("R", "R-cur", "muse")})}
    assert keys == {"F_d3"}


class FakeCaller:
    def __init__(self, raise_on=None):
        self.n = 0
        self.raise_on = raise_on

    def __call__(self, key, task, prompt, budget, http=None, client=None, with_format=True):
        self.n += 1
        if self.raise_on and self.n == self.raise_on:
            raise KeyboardInterrupt
        claude = key in ("sonnet", "haiku")
        usd = 0.01 if claude else None
        return {"attempts": [{"outcome": "ok", "latency_ms": 5, "usd": usd,
                              "openrouter_cost_usd": None if claude else 0.001}],
                "outcome": "ok", "text": SECRET_A, "usd": usd or 0.0, "usd_upper": None}


def _main(out, args, caller):
    return run.main(["--out", out] + args, caller=caller, client_factory=lambda: object())


def test_dry_run_makes_no_call(tmp_path, capsys):
    out = _setup(tmp_path)
    fc = FakeCaller()
    assert _main(out, ["--phase", "round0", "--dry-run"], fc) == 0
    assert fc.n == 0
    text = capsys.readouterr().out
    assert "to make 558" in text and "anthropic" in text


def test_confirm_mismatch_exits_2(tmp_path):
    out = _setup(tmp_path)
    fc = FakeCaller()
    assert _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "5"], fc) == 2
    assert fc.n == 0
    assert not os.path.exists(os.path.join(out, "runs", "pilot.jsonl"))


def test_cap_cuts_claude_cells_exit_3(tmp_path):
    out = _setup(tmp_path)
    led = ledger.ReportLedger(os.path.join(out, ledger.LEDGER_NAME))
    led.add("report/round0", 30.95)
    fc = FakeCaller()
    n = len(run.plan("round0", ["X"], _index()))
    assert _main(out, ["--phase", "round0", "--task", "X", "--confirm-calls", str(n)], fc) == 3
    s = common.read_json(os.path.join(out, "summary_round0.json"))
    assert s["cut_calls"] == 2 * 4 * 4 * 3                 # sonnet + haiku, all X cells
    assert "X|X-cur|sonnet" in s["cut_cells"] and not any("muse" in c for c in s["cut_cells"])
    assert fc.n == 4 * 4 * 4 * 3                           # OpenRouter ran
    assert not os.path.exists(os.path.join(out, common.LOCK_NAME))


def test_resume_skips_finished_runs(tmp_path):
    out = _setup(tmp_path)
    fc = FakeCaller(raise_on=3)
    with pytest.raises(KeyboardInterrupt):
        _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "6"], fc)
    recs = common.read_jsonl(os.path.join(out, "runs", "pilot.jsonl"))
    assert len(recs) == 2
    assert not os.path.exists(os.path.join(out, common.LOCK_NAME))
    fc2 = FakeCaller()
    assert _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "6"], fc2) == 2
    assert _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "4"], fc2) == 0
    assert fc2.n == 4
    recs = common.read_jsonl(os.path.join(out, "runs", "pilot.jsonl"))
    assert len({r["key"] for r in recs}) == 6 == len(recs)
    led = ledger.ReportLedger(os.path.join(out, ledger.LEDGER_NAME))
    claude_usd = sum(r["usd"] for r in recs if r["model"] in ("sonnet", "haiku"))
    assert led.report_spent() == pytest.approx(claude_usd)
    assert led.pool_spent("report/pilot") == pytest.approx(claude_usd)


def test_stdout_has_no_answer_text(tmp_path, capsys):
    out = _setup(tmp_path)
    _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "6"], FakeCaller())
    assert SECRET_A not in capsys.readouterr().out
    s = json.dumps(common.read_json(os.path.join(out, "summary_pilot.json")))
    assert SECRET_A not in s
    rec = common.read_jsonl(os.path.join(out, "runs", "pilot.jsonl"))[0]
    assert "text" not in rec and open(rec["text_path"], encoding="utf-8").read() == SECRET_A


def test_lock_held_refuses(tmp_path):
    out = _setup(tmp_path)
    common.write_text(os.path.join(out, common.LOCK_NAME), "{}")
    with pytest.raises(RuntimeError):
        _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "6"], FakeCaller())


def test_schema_rejected_reruns_without_format(tmp_path):
    out = _setup(tmp_path)
    seen = []

    def caller(key, task, prompt, budget, http=None, client=None, with_format=True):
        seen.append((key, with_format))
        if key == "sonnet" and with_format:
            return {"attempts": [{"outcome": "schema_rejected", "usd": None}],
                    "outcome": "schema_rejected", "text": "", "usd": 0.0}
        return {"attempts": [{"outcome": "ok", "usd": 0.0}], "outcome": "ok", "text": "{}",
                "usd": 0.0}
    _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "6"], caller)
    assert ("sonnet", True) in seen and ("sonnet", False) in seen
    rec = [r for r in common.read_jsonl(os.path.join(out, "runs", "pilot.jsonl"))
           if r["model"] == "sonnet"][0]
    assert rec["format_dropped"] is True and rec["outcome"] == "ok"
    assert len(rec["attempts"]) == 2


def test_import_does_not_pull_the_sdk():
    code = ("import sys; sys.path.insert(0, r'%s'); import rb_run, rb_callers; "
            "print('anthropic' in sys.modules)" % BENCH_DIR)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=REPO, env=dict(os.environ))
    assert r.stdout.strip() == "False", r.stderr[-500:]


def test_missing_key_exits_2_before_lock(tmp_path, monkeypatch):
    out = _setup(tmp_path)
    monkeypatch.delenv("QWEN_API_KEY")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    fc = FakeCaller()
    assert _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "6"], fc) == 2
    assert fc.n == 0 and not os.path.exists(os.path.join(out, common.LOCK_NAME))
    monkeypatch.setenv("QWEN_API_KEY", "k")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "6"], fc) == 2
    assert fc.n == 0


def test_missing_sdk_exits_2(tmp_path, monkeypatch):
    out = _setup(tmp_path)
    monkeypatch.setattr(run.importlib.util, "find_spec",
                        lambda name, *a: None if name == "anthropic" else object())
    fc = FakeCaller()
    assert run.main(["--out", out, "--phase", "pilot", "--task", "X", "--confirm-calls", "6"],
                    caller=fc) == 2
    assert fc.n == 0


def test_infra_failures_are_not_done_on_resume(tmp_path):
    out = _setup(tmp_path)
    keys = [c["key"] for c in run.plan("pilot", ["X"], _index())]
    outcomes = [("http_error", None, "no_key"), ("http_error", 429, None),
                ("http_error", 503, None), ("http_error", None, "ConnectionError"),
                ("truncated", 200, None), ("timeout", None, "ReadTimeout")]
    for k, (o, st, ec) in zip(keys, outcomes):
        common.append_jsonl(os.path.join(out, "runs", "pilot.jsonl"),
                            {"key": k, "task": "X", "candidate": "X-cur", "model": k.split("|")[3],
                             "outcome": o, "attempts": [{"http_status": st, "error_code": ec}],
                             "usd": 0.0})
    fc = FakeCaller()
    assert _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "4"], fc) == 0
    assert fc.n == 4
    s = common.read_json(os.path.join(out, "summary_pilot.json"))
    assert s["runs"] == 6 and sum(v for k, v in s["counts"].items() if k.endswith("|ok")) == 4


def test_schema_rerun_checks_the_pool(tmp_path):
    out = _setup(tmp_path)
    led = ledger.ReportLedger(os.path.join(out, ledger.LEDGER_NAME))
    calls = []

    def caller(key, task, prompt, budget, http=None, client=None, with_format=True):
        calls.append((key, with_format))
        if key == "sonnet" and with_format:
            led.add("report/pilot", 31.0)          # pool gone between the two calls
            return {"attempts": [{"outcome": "schema_rejected", "usd": None}],
                    "outcome": "schema_rejected", "text": "", "usd": 0.0}
        return {"attempts": [{"outcome": "ok", "usd": 0.0}], "outcome": "ok", "text": "{}",
                "usd": 0.0}
    assert _main(out, ["--phase", "pilot", "--task", "X", "--confirm-calls", "6"], caller) == 3
    assert ("sonnet", False) not in calls
