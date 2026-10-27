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


def test_web_add_merges_a_report_write_made_after_web_start(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    led = L.Ledger(p)                       # web run starts, reads the (empty) file
    L.add_report_spend(p, "report/round0", 2.5)   # report bench writes afterwards
    led.add(1.0, pool="matrix")
    st = L.read_state(p)
    assert st["pools"]["report/round0"] == 2.5
    assert st["pools"]["matrix"] == 1.0 and st["spent_usd"] == 1.0
    led.add(0.5, pool="matrix")
    assert L.read_state(p)["pools"] == {"report/round0": 2.5, "matrix": 1.5}
    assert led.spent == 1.5


def test_web_add_keeps_web_spend_written_by_another_instance(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    a, b = L.Ledger(p), L.Ledger(p)
    a.add(1.0, pool="matrix")
    b.add(1.0, pool="matrix")
    assert L.read_state(p)["spent_usd"] == 2.0


def _load_web(name):
    import sys
    d = os.path.dirname(PATH)
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name,argv", [
    ("stage0", ["--search-price-per-1000", "10", "--dry-run"]),
    ("matrix", ["--round", "0", "--search-price-per-1000", "10", "--dry-run"]),
    ("reprobe", ["--config", None, "--max-uses", "1", "--search-price-per-1000", "10",
                 "--dry-run"]),
    ("grade", ["--calls", "matrix_r0_calls.jsonl", "--seed", "1", "--dry-run"]),
])
def test_web_scripts_refuse_while_the_report_bench_holds_the_lock(tmp_path, name, argv):
    mod = _load_web(name)
    if name == "reprobe":
        argv = [a if a is not None else sorted(mod.CONFIGS)[0] for a in argv]
    out = tmp_path / "out"
    out.mkdir()
    (out / L.LOCK_NAME).write_text("{}", encoding="utf-8")
    args = ["--out", str(out)] + argv
    with pytest.raises(SystemExit) as e:
        if name == "reprobe":
            mod.validate(args, {})
        else:
            mod.validate(args, {}, repo_root=REPO)
    assert e.value.code == 2
