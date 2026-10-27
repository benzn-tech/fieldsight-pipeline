"""Web bench ledger: the report-bench surface added for the shared file.

The web `Ledger` must behave exactly as before; report dollars live only in
`report/*` pools and never touch `spent_usd` / `cap_usd`.
"""
import importlib.util
import json
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(REPO, "tools", "web-answer-bench", "ledger.py")


def _load():
    spec = importlib.util.spec_from_file_location("web_ledger_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = _load()


def _write(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def test_report_spend_leaves_web_keys_alone(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    web = {"spent_usd": 44.0, "cap_usd": 45.0,
           "pools": {"stage0": 2.0, "matrix": 17.0, "scored": 25.0}}
    _write(p, web)
    L.add_report_spend(p, "report/round0", 1.5)
    data = json.load(open(p, encoding="utf-8"))
    assert data["spent_usd"] == 44.0 and data["cap_usd"] == 45.0
    for k, v in web["pools"].items():
        assert data["pools"][k] == v
    assert data["pools"]["report/round0"] == 1.5
    assert all(isinstance(v, float) for v in data["pools"].values())
    led = L.Ledger(p)
    assert led.spent == 44.0 and not led.exhausted()


def test_web_add_keeps_report_pools(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    L.add_report_spend(p, "report/round0", 3.0)
    led = L.Ledger(p)
    assert led.spent == 0.0
    led.add(1.0, pool="matrix")
    st = L.read_state(p)
    assert st["pools"]["report/round0"] == 3.0
    assert st["spent_usd"] == 1.0 and st["cap_usd"] == 45.0
    assert L.report_spent(st) == 3.0
    assert L.global_spent(st) == 4.0


def test_fresh_file_is_readable_by_web(tmp_path):
    p = str(tmp_path / "l.json")
    L.add_report_spend(p, "report/pilot", 0.25)
    data = json.load(open(p, encoding="utf-8"))
    assert data["spent_usd"] == 0.0 and data["cap_usd"] == L.CAP_USD


def test_global_spent_uses_spent_usd_not_web_pools(tmp_path):
    p = str(tmp_path / "l.json")
    _write(p, {"spent_usd": 10.0})          # pre-pools file: all stage0
    st = L.read_state(p)
    assert st["pools"] == {"stage0": 10.0}
    L.add_report_spend(p, "report/round0", 2.0)
    st = L.read_state(p)
    assert L.global_spent(st) == 12.0
    assert L.Ledger(p).pools["stage0"] == 10.0


def test_non_report_pool_refused(tmp_path):
    with pytest.raises(ValueError):
        L.add_report_spend(str(tmp_path / "l.json"), "matrix", 1.0)


def test_caps():
    assert L.REPORT_CAP_USD == 40.0 and L.GLOBAL_CAP_USD == 85.0
    assert L.CAP_USD + L.REPORT_CAP_USD == L.GLOBAL_CAP_USD
