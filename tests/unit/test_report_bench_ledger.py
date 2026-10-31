"""Report bench, Task 2: report pools inside the ledger shared with the web bench."""
import importlib.util
import json
import os
import sys

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


RL = _load("rb_ledger")


def test_call_cost_haiku_long_context():
    assert RL.call_cost("claude-haiku-5-5", 120_000, 1_000) == \
        (120_000 * 0.50 + 1_000 * 2.50) / 1e6
    assert RL.call_cost("claude-haiku-5-5", 100_000, 1_000) == \
        (100_000 * 0.10 + 1_000 * 0.50) / 1e6
    assert RL.call_cost("claude-sonnet-5-5", 50_000, 40_000) == \
        (50_000 * 2.0 + 40_000 * 10.0) / 1e6


def test_constants():
    assert (RL.REPORT_CAP, RL.HELDOUT_RESERVE, RL.PRE_HELDOUT_STOP, RL.GLOBAL_CAP) == \
        (40.0, 9.0, 31.0, 85.0)


def test_pre_heldout_stop_and_reserve(tmp_path):
    led = RL.ReportLedger(str(tmp_path / "anthropic_ledger.json"))
    assert led.can_spend("report/round0", 31.0)
    led.add("report/round0", 30.9)
    assert not led.can_spend("report/round0", 0.2)
    assert not led.can_spend("report/rounds12", 0.2)
    assert led.can_spend("report/heldout", 9.0)
    assert not led.can_spend("report/heldout", 9.2)     # report cap 40


def test_web_format_file_survives(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    web = {"spent_usd": 44.0, "cap_usd": 45.0,
           "pools": {"stage0": 2.0, "matrix": 17.0, "scored": 25.0}}
    json.dump(web, open(p, "w", encoding="utf-8"))
    RL.ReportLedger(p).add("report/round0", 0.5)
    data = json.load(open(p, encoding="utf-8"))
    assert data["spent_usd"] == 44.0 and data["cap_usd"] == 45.0
    assert {k: data["pools"][k] for k in web["pools"]} == web["pools"]
    assert all(isinstance(v, float) for v in data["pools"].values())


def test_global_cap(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    json.dump({"spent_usd": 44.0, "cap_usd": 45.0, "pools": {"matrix": 44.0,
               "report/round0": 31.0, "report/heldout": 9.5}}, open(p, "w"))
    led = RL.ReportLedger(p)
    assert led.global_spent() == 84.5
    assert not led.can_spend("report/heldout", 0.6)


def test_web_style_rewrite_keeps_report_pools(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    led = RL.ReportLedger(p)
    led.add("report/round0", 2.0)
    # The web Ledger.add, exactly as it rewrites the file.
    web = RL.web_ledger().Ledger(p)
    web.add(1.25, pool="matrix")
    data = json.load(open(p, encoding="utf-8"))
    assert data["pools"]["report/round0"] == 2.0
    assert led.report_spent() == 2.0
    assert led.global_spent() == 3.25


def test_add_rereads_the_file(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    a, b = RL.ReportLedger(p), RL.ReportLedger(p)
    a.add("report/round0", 1.0)
    b.add("report/round0", 1.0)
    assert a.report_spent() == 2.0


def test_openrouter_spend_never_counts_toward_the_anthropic_stops(tmp_path):
    led = RL.ReportLedger(str(tmp_path / "anthropic_ledger.json"))
    led.add("report/round0", 30.0)
    led.add_openrouter(9.5)
    assert led.report_spent() == 30.0 and led.global_spent() == 30.0
    assert led.can_spend("report/round0", 1.0)
    assert led.openrouter_spent() == 9.5
    assert led.openrouter_room(10.0) == 0.5
