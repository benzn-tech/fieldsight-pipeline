"""scripts/action_pack_eval: gold validation, annotations + the blind second-annotator
subset, the report, the noise-floor compare and the live subcommands (spec §8, §14 S16).
Nothing here reaches the network: the live subcommands run on a fake client with sockets
patched to raise."""
from __future__ import annotations

import copy
import json
import socket
from pathlib import Path

import pytest

from scripts.action_pack_eval import annotations as ann_mod
from scripts.action_pack_eval import cli
from scripts.action_pack_eval import compare as compare_mod
from scripts.action_pack_eval import gold as gold_mod
from scripts.action_pack_eval import live
from scripts.action_pack_eval import metrics
from scripts.action_pack_eval import paths
from scripts.action_pack_eval import report as report_mod
from scripts.action_pack_eval import runs as runs_mod

EX = Path(__file__).resolve().parents[2] / "scripts" / "action_pack_eval" / "examples"
S = "00000000-0000-4000-8000-0000000000"


def _entry(i, scenario="other", **kw):
    e = {"id": f"G{i}", "site": "Example Site", "instruction": f"instruction {i}",
         "scenario": scenario, "expected_actions": [], "must_not_touch": [],
         "expected_uncertain": []}
    e.update(kw)
    return e


def _full_gold():
    out = [_entry(0, "pour_review")]
    out += [_entry(i, "tidy_open_items") for i in range(1, 4)]
    out += [_entry(i, "standard_in_library") for i in range(4, 9)]
    out += [_entry(i, "standard_not_in_library") for i in range(9, 14)]
    out += [_entry(i) for i in range(14, 20)]
    return out


@pytest.fixture()
def runs_dir(tmp_path):
    d = tmp_path / "action_pack_eval_runs"
    d.mkdir()
    return d


# ---------------------------------------------------------------- gold validation

def test_a_complete_gold_set_validates_clean():
    errors, warnings = gold_mod.validate_gold(_full_gold())
    assert errors == [] and warnings == []


def test_the_example_gold_set_is_valid_but_warns_it_is_short():
    errors, warnings = gold_mod.validate_gold(gold_mod.load_gold(EX / "gold.json"))
    assert errors == []
    assert any("3 instructions" in w and "20" in w for w in warnings)
    # ... and that it misses the spec's composition (5 in-library, 5 not, 3 tidy).
    assert any("standard_in_library" in w for w in warnings)


@pytest.mark.parametrize("mutate,needle", [
    (lambda g: g[1].update(id="G0"), "duplicate id"),
    (lambda g: g[0].update(scenario="pour"), "scenario"),
    (lambda g: g[0].update(instruction=" "), "instruction text is required"),
    (lambda g: g[0].update(instruction="x" * 2001), "2000"),
    (lambda g: g[0].update(site=""), "site is required"),
    (lambda g: g[0].update(expected_actions=[{"id": "e1", "type": "close_item"}]),
     "target"),
    (lambda g: g[0].update(expected_actions=[{"id": "e1", "type": "create_item"}]),
     "match_hint"),
    (lambda g: g[0].update(expected_actions=[{"id": "e1", "type": "delete_item",
                                              "match_hint": "x"}]), "type must be"),
    (lambda g: g[0].update(expected_actions=[
        {"id": "e1", "type": "create_item", "match_hint": "x"},
        {"id": "e1", "type": "create_item", "match_hint": "y"}]), "duplicate expected"),
    (lambda g: g[0].update(must_not_touch=[1.5]), "must_not_touch"),
    (lambda g: g[0].update(expected_uncertain=[{"standard": "NZS"}]), "expected_uncertain"),
])
def test_gold_validation_errors(mutate, needle):
    g = _full_gold()
    mutate(g)
    errors, _ = gold_mod.validate_gold(g)
    assert any(needle in e for e in errors), errors


def test_gold_loads_from_jsonl(tmp_path):
    f = tmp_path / "gold.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in _full_gold()) + "\n", encoding="utf-8")
    assert [e["id"] for e in gold_mod.load_gold(f)] == [f"G{i}" for i in range(20)]


# ---------------------------------------------------------------- annotations

def test_annotation_validation_against_the_run():
    gold = gold_mod.load_gold(EX / "gold.json")
    run = runs_mod.load_run(EX / "run_a.json")
    rows = ann_mod.load_annotations(EX / "annotations_a.jsonl")
    assert ann_mod.validate_annotations(rows, run=run, gold=gold) == []
    bad = copy.deepcopy(rows)
    bad[0]["supported_by_refs"] = "maybe"
    bad[1]["element"] = "conclusion:9"
    bad[2]["matches_expected"] = "e9"
    bad.append(dict(rows[3]))                      # duplicate (pack, element, annotator)
    errors = ann_mod.validate_annotations(bad, run=run, gold=gold)
    assert any("supported_by_refs" in e for e in errors)
    assert any("conclusion:9" in e for e in errors)
    assert any("e9" in e for e in errors)
    assert any("duplicate" in e for e in errors)


def test_blind_subset_is_deterministic_and_label_stripped(runs_dir):
    run = runs_mod.load_run(EX / "run_a.json")
    rows = ann_mod.load_annotations(EX / "annotations_a.jsonl")
    a = ann_mod.make_blind_subset(run, rows, n=5, seed=7)
    b = ann_mod.make_blind_subset(run, rows, n=5, seed=7)
    c = ann_mod.make_blind_subset(run, rows, n=5, seed=8)
    assert a == b and len(a) == 5
    assert [(x["pack_id"], x["element"]) for x in a] != [(x["pack_id"], x["element"])
                                                         for x in c]
    text = json.dumps(a)
    for stripped in ("verifier", "confidence", "applicable", "\"state\"", "ann1",
                     "matches_expected", "expected_uncertain_id", "unsupported"):
        assert stripped not in text, stripped
    for item in a:
        assert item["supported_by_refs"] is None and item["is_standards_claim"] is None
        assert item["annotator"] is None
        assert item["sources"] and all("excerpt" in s for s in item["sources"])
    # Every sampled element was labelled by the first annotator.
    labelled = {(r["pack_id"], r["element"]) for r in rows}
    assert all((x["pack_id"], x["element"]) in labelled for x in a)


def test_blind_subset_fraction_rounds_up_and_caps():
    run = runs_mod.load_run(EX / "run_a.json")
    rows = ann_mod.load_annotations(EX / "annotations_a.jsonl")
    assert len(ann_mod.make_blind_subset(run, rows, fraction=0.25, seed=1)) == 4   # ceil(3.25)
    assert len(ann_mod.make_blind_subset(run, rows, n=99, seed=1)) == 13


# ---------------------------------------------------------------- report + thresholds

def _report(**kw):
    gold = gold_mod.load_gold(EX / "gold.json")
    run = runs_mod.load_run(EX / "run_a.json")
    rows = ann_mod.load_annotations(EX / "annotations_a.jsonl")
    prices = metrics.load_prices(EX / "prices.json")
    return report_mod.build_report(gold, run, rows, prices=prices, gold_sha256="abc", **kw)


def test_report_gates_fail_on_the_example():
    rep = _report()
    assert rep["gates"]["fabrication"]["verdict"] == "fail"
    assert rep["gates"]["wrong_target"]["verdict"] == "fail"
    assert rep["gates"]["recall"]["verdict"] == "pass"
    assert rep["gates"]["latency_p90"]["verdict"] == "insufficient"     # n 3 < 20
    assert rep["overall"] == "fail"
    assert rep["gold_sha256"] == "abc" and rep["config_label"] == "baseline"


def test_report_passes_when_thresholds_are_loosened():
    th = dict(metrics.DEFAULT_THRESHOLDS, fabrication_shown_max=0.2, wrong_target_max=1,
              latency_p90_max_s=400, latency_min_n=3)
    rep = _report(thresholds=th)
    # wrong_target_max is a count on the SHOWN targeted actions (1 wrong).
    assert {k: v["verdict"] for k, v in rep["gates"].items()} == {
        "fabrication": "pass", "wrong_target": "pass", "recall": "pass",
        "latency_p90": "pass"}
    assert rep["overall"] == "pass"


def test_report_markdown_has_raw_and_post_verifier_rows():
    md = report_mod.render_markdown(_report())
    assert "raw writer" in md.lower() and "post-verifier" in md.lower()
    assert "| fabrication |" in md.lower().replace("**", "")
    assert "18.2%" in md                                     # 2/11 shown


def test_report_carries_agreement_when_a_second_file_is_given():
    rows = ann_mod.load_annotations(EX / "annotations_a.jsonl")
    second = [dict(r, annotator="ann2") for r in rows[:4]]
    rep = _report(second=second)
    assert rep["metrics"]["agreement"]["n"] == 4
    assert rep["metrics"]["agreement"]["supported_by_refs"]["percent"] == 1.0


# ---------------------------------------------------------------- noise floor

def _summary(label="baseline", sha="abc", **m):
    base = {"fabrication_shown_rate": 0.04, "fabrication_raw_rate": 0.10,
            "wrong_target_rate": 0.0, "recall": 0.70, "uncertain_honesty": 0.8,
            "latency_p50_s": 100.0, "latency_p90_s": 200.0, "cost_mean": 0.20}
    base.update(m)
    return {"run_id": label + str(m), "config_label": label, "gold_sha256": sha,
            "headline": base}


def test_compare_refuses_with_fewer_than_two_baseline_runs():
    with pytest.raises(compare_mod.CompareRefused, match="two"):
        compare_mod.compare([_summary()], [_summary("v", recall=0.9)])


def test_compare_refuses_mixed_gold_sets_or_mixed_baseline_configs():
    with pytest.raises(compare_mod.CompareRefused, match="gold"):
        compare_mod.compare([_summary(), _summary(recall=0.72)],
                            [_summary("v", sha="other")])
    with pytest.raises(compare_mod.CompareRefused, match="config"):
        compare_mod.compare([_summary(), _summary("other")], [_summary("v")])


def test_compare_refuses_to_call_a_delta_inside_the_floor_better():
    base = [_summary(recall=0.60), _summary(recall=0.70)]       # floor 0.10
    out = compare_mod.compare(base, [_summary("v", recall=0.72)])
    rec = out["variants"][0]["metrics"]["recall"]
    assert rec["floor"] == pytest.approx(0.10)
    assert rec["delta"] == pytest.approx(0.07)                    # vs baseline mean 0.65
    assert rec["verdict"] == "within_noise"
    out = compare_mod.compare(base, [_summary("v", recall=0.80)])
    assert out["variants"][0]["metrics"]["recall"]["verdict"] == "better"
    out = compare_mod.compare(base, [_summary("v", recall=0.50)])
    assert out["variants"][0]["metrics"]["recall"]["verdict"] == "worse"
    # Lower is better for fabrication.
    base = [_summary(fabrication_shown_rate=0.04), _summary(fabrication_shown_rate=0.05)]
    out = compare_mod.compare(base, [_summary("v", fabrication_shown_rate=0.01)])
    assert out["variants"][0]["metrics"]["fabrication_shown_rate"]["verdict"] == "better"


def test_compare_with_no_variant_reports_the_floor_only():
    out = compare_mod.compare([_summary(recall=0.6), _summary(recall=0.7)], [])
    assert out["floor"]["recall"] == pytest.approx(0.10)
    assert out["variants"] == []


def test_compare_marks_missing_values_not_comparable():
    base = [_summary(cost_mean=None), _summary(cost_mean=None)]
    out = compare_mod.compare(base, [_summary("v")])
    assert out["variants"][0]["metrics"]["cost_mean"]["verdict"] == "n/a"


# ---------------------------------------------------------------- paths / CLI

def test_writes_outside_the_gitignored_root_are_refused(tmp_path):
    with pytest.raises(SystemExit):
        paths.require_run_dir(tmp_path / "elsewhere" / "report.json")
    assert paths.require_run_dir(tmp_path / "action_pack_eval_runs" / "r.json")


def test_gitignore_guards_the_run_root():
    text = (Path(__file__).resolve().parents[2] / ".gitignore").read_text(encoding="utf-8")
    assert "action_pack_eval_runs/" in text.splitlines()


def test_cli_score_writes_markdown_and_json(runs_dir):
    out = runs_dir / "example"
    rc = cli.main(["score", "--gold", str(EX / "gold.json"), "--run", str(EX / "run_a.json"),
                   "--annotations", str(EX / "annotations_a.jsonl"),
                   "--prices", str(EX / "prices.json"), "--out", str(out)])
    assert rc == 1                                   # gates fail on the example
    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert rep["headline"]["fabrication_shown_rate"] == pytest.approx(2 / 11)
    assert rep["headline"]["fabrication_raw_rate"] == pytest.approx(3 / 13)
    assert (out / "report.md").read_text(encoding="utf-8").startswith("# Action pack eval")
    assert len(rep["gold_sha256"]) == 64


def test_cli_score_refuses_an_invalid_gold_set(runs_dir, tmp_path):
    g = tmp_path / "gold.json"
    g.write_text(json.dumps({"instructions": [_entry(1, "nope")]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["score", "--gold", str(g), "--run", str(EX / "run_a.json"),
                  "--annotations", str(EX / "annotations_a.jsonl"),
                  "--out", str(runs_dir / "x")])


def test_cli_validate_gold_returns_zero_with_warnings(capsys):
    assert cli.main(["validate-gold", "--gold", str(EX / "gold.json")]) == 0
    assert "WARNING" in capsys.readouterr().out


def test_cli_blind_subset_and_compare(runs_dir):
    dest = runs_dir / "blind.jsonl"
    assert cli.main(["make-blind-subset", "--run", str(EX / "run_a.json"),
                     "--annotations", str(EX / "annotations_a.jsonl"), "--n", "4",
                     "--seed", "3", "--out", str(dest)]) == 0
    lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    reports = []
    for i, recall in enumerate((0.6, 0.7, 0.9)):
        p = runs_dir / f"r{i}.json"
        p.write_text(json.dumps(_summary("baseline" if i < 2 else "v", recall=recall)),
                     encoding="utf-8")
        reports.append(str(p))
    out = runs_dir / "cmp"
    assert cli.main(["compare", "--baseline", reports[0], reports[1], "--variant", reports[2],
                     "--out", str(out)]) == 0
    cmp = json.loads((out / "compare.json").read_text(encoding="utf-8"))
    assert cmp["variants"][0]["metrics"]["recall"]["verdict"] == "better"
    md = (out / "compare.md").read_text(encoding="utf-8")
    assert "| recall |" in md and "better" in md
    with pytest.raises(SystemExit):
        cli.main(["compare", "--baseline", reports[0], "--variant", reports[2],
                  "--out", str(out)])


def test_cli_refuses_to_write_outside_the_run_root(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["score", "--gold", str(EX / "gold.json"), "--run", str(EX / "run_a.json"),
                  "--annotations", str(EX / "annotations_a.jsonl"),
                  "--out", str(tmp_path / "not_ignored")])


# ---------------------------------------------------------------- live (fake client)

@pytest.fixture()
def no_sockets(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a socket was opened")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


class FakeApi:
    """Org-api double: POST creates a queued pack, each GET advances it one state; a second
    POST while one is queued/running answers 409 like the real one-active-per-creator index."""

    def __init__(self):
        self.calls, self.packs, self.n = [], {}, 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        assert headers.get("Authorization") == "tok"
        if method == "POST":
            if any(p["state"] in ("queued", "running") for p in self.packs.values()):
                return 409, {"error": "you already have a pack in progress"}
            self.n += 1
            pid = f"{S}f{self.n}"
            req = json.loads(body)
            self.packs[pid] = {"id": pid, "state": "queued", "site_id": req["site_id"],
                               "instruction": req["instruction"], "conclusion": [],
                               "actions": [], "sources": []}
            return 202, {"id": pid, "state": "queued"}
        pid = url.rsplit("/", 1)[1]
        p = self.packs[pid]
        p["state"] = {"queued": "running", "running": "ready"}.get(p["state"], p["state"])
        return 200, dict(p)


def test_submit_is_sequential_and_uses_the_injected_client(no_sockets, runs_dir):
    gold = gold_mod.load_gold(EX / "gold.json")
    api = FakeApi()
    run = live.submit(gold, base_url="https://test.example/api/org", token="tok", client=api,
                      site_map={"Example Site B": "00000000-0000-4000-8000-00000000a002"},
                      sleep=lambda s: None, clock=iter(range(0, 10_000, 5)).__next__,
                      poll_s=1, max_wait_s=600, config_label="baseline")
    assert [p["gold_id"] for p in run["packs"]] == ["G1", "G2", "G3"]
    assert all(p["pack"]["state"] == "ready" for p in run["packs"])
    posts = [c for c in api.calls if c[0] == "POST"]
    assert len(posts) == 3 and all(c[1] == "https://test.example/api/org/action-packs"
                                   for c in posts)
    assert json.loads(posts[1][3]) == {"instruction": gold[1]["instruction"],
                                       "site_id": "00000000-0000-4000-8000-00000000a002"}
    # Never a POST while the previous pack was still queued/running.
    assert all(status != 409 for status in [p.get("http_status") for p in run["packs"]])


def test_submit_skips_an_entry_whose_site_is_not_a_uuid_or_mapped(no_sockets):
    gold = gold_mod.load_gold(EX / "gold.json")
    api = FakeApi()
    run = live.submit(gold, base_url="https://t/api/org", token="tok", client=api,
                      site_map={}, sleep=lambda s: None,
                      clock=iter(range(0, 10_000, 5)).__next__, poll_s=1, max_wait_s=600)
    g2 = [p for p in run["packs"] if p["gold_id"] == "G2"][0]
    assert g2["pack"] is None and g2["error"] == "site_unresolved"
    assert len([c for c in api.calls if c[0] == "POST"]) == 2


def test_submit_stops_polling_at_max_wait(no_sockets):
    class Stuck(FakeApi):
        def __call__(self, method, url, headers, body):
            if method == "GET":
                self.calls.append((method, url, dict(headers), body))
                return 200, dict(self.packs[url.rsplit("/", 1)[1]], state="running")
            return super().__call__(method, url, headers, body)
    gold = gold_mod.load_gold(EX / "gold.json")[:1]
    run = live.submit(gold, base_url="https://t/api/org", token="tok", client=Stuck(),
                      site_map={}, sleep=lambda s: None,
                      clock=iter(range(0, 10_000, 50)).__next__, poll_s=1, max_wait_s=120)
    assert run["packs"][0]["error"] == "timeout"


def test_fetch_regets_every_pack_with_the_injected_client(no_sockets):
    api = FakeApi()
    api.packs = {f"{S}f1": {"id": f"{S}f1", "state": "ready"}}
    prior = {"run_id": "r", "config_label": "baseline",
             "packs": [{"gold_id": "G1", "pack": {"id": f"{S}f1"}},
                       {"gold_id": "G2", "pack": None, "error": "site_unresolved"}]}
    run = live.fetch(prior, base_url="https://t/api/org", token="tok", client=api)
    assert [c[:2] for c in api.calls] == [("GET", f"https://t/api/org/action-packs/{S}f1")]
    assert run["packs"][0]["pack"]["state"] == "ready"
    assert run["packs"][1]["pack"] is None


def test_attach_traces_merges_run_trace_and_models():
    run = runs_mod.load_run(EX / "run_a.json")
    for p in run["packs"]:
        p["pack"].pop("run_trace", None)
        p["pack"].pop("models", None)
    traces = [{"id": f"{S}b1", "run_trace": {"tokens": 7}, "models": {"writer": "m"}}]
    out = runs_mod.attach_traces(run, traces)
    assert out["packs"][0]["pack"]["run_trace"] == {"tokens": 7}
    assert "run_trace" not in out["packs"][1]["pack"]


def test_cli_live_subcommands_need_a_token(monkeypatch, runs_dir, no_sockets):
    monkeypatch.delenv("ACTION_PACK_EVAL_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["submit", "--gold", str(EX / "gold.json"), "--base-url", "https://t/api/org",
                  "--out", str(runs_dir / "run.json")])


def test_cli_submit_uses_the_client_factory(monkeypatch, runs_dir, no_sockets):
    api = FakeApi()
    monkeypatch.setenv("ACTION_PACK_EVAL_TOKEN", "tok")
    monkeypatch.setattr(live, "urllib_client", lambda timeout: api)
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    gold = EX / "gold.json"
    out = runs_dir / "run.json"
    assert cli.main(["submit", "--gold", str(gold), "--base-url", "https://t/api/org",
                     "--poll-s", "0", "--out", str(out)]) == 0
    run = json.loads(out.read_text(encoding="utf-8"))
    assert len(run["packs"]) == 3 and api.calls


# ---------------------------------------------------------------- fix round 1

def test_latency_counts_a_timed_out_instruction_as_a_censored_value():
    run = {"packs": [
        {"gold_id": "G1", "pack": {"id": "p", "state": "ready",
                                   "created_at": "2026-11-01T00:00:00+00:00",
                                   "updated_at": "2026-11-01T00:01:40+00:00"}},
        {"gold_id": "G2", "pack_id": "q", "pack": None, "error": "timeout",
         "client_elapsed_s": 900.0}]}
    out = metrics.score_latency(run, max_p90_s=300, min_n=1)
    # The slowest instruction must not vanish from the tail: 100 s and >= 900 s.
    assert out["gate_n"] == 2 and out["gate_p90_s"] == 900.0
    assert out["sources"]["timeout"] == 1
    assert out["verdict"] == metrics.FAIL


def test_submit_records_elapsed_time_on_a_timeout(no_sockets):
    class Stuck(FakeApi):
        def __call__(self, method, url, headers, body):
            if method == "GET":
                return 200, dict(self.packs[url.rsplit("/", 1)[1]], state="running")
            return super().__call__(method, url, headers, body)
    gold = gold_mod.load_gold(EX / "gold.json")[:1]
    run = live.submit(gold, base_url="https://t/api/org", token="tok", client=Stuck(),
                      site_map={}, sleep=lambda s: None,
                      clock=iter(range(0, 10_000, 50)).__next__, poll_s=1, max_wait_s=120)
    assert run["packs"][0]["error"] == "timeout"
    assert run["packs"][0]["client_elapsed_s"] >= 120


def test_a_label_file_holding_two_annotators_is_refused():
    rows = ann_mod.load_annotations(EX / "annotations_a.jsonl")
    both = rows + [dict(rows[0], annotator="ann2", supported_by_refs="no")]
    errors = ann_mod.validate_annotations(both)
    assert any("duplicate" in e and "one annotator per file" in e for e in errors)


def test_score_refuses_a_run_submitted_from_other_instructions(runs_dir, tmp_path):
    run = runs_mod.load_run(EX / "run_a.json")
    run["inputs_sha256"] = "0" * 64
    rp = tmp_path / "run.json"
    rp.write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(SystemExit, match="different instructions"):
        cli.main(["score", "--gold", str(EX / "gold.json"), "--run", str(rp),
                  "--annotations", str(EX / "annotations_a.jsonl"),
                  "--out", str(runs_dir / "x")])
    gold = gold_mod.load_gold(EX / "gold.json")
    run["inputs_sha256"] = gold_mod.inputs_sha256(gold)
    rp.write_text(json.dumps(run), encoding="utf-8")
    cli.main(["score", "--gold", str(EX / "gold.json"), "--run", str(rp),
              "--annotations", str(EX / "annotations_a.jsonl"), "--out", str(runs_dir / "x")])
    rep = json.loads((runs_dir / "x" / "report.json").read_text(encoding="utf-8"))
    assert rep["submitted_inputs_sha256"] == gold_mod.inputs_sha256(gold)


def test_inputs_hash_ignores_expected_actions_but_not_instruction_text():
    g = _full_gold()
    h = gold_mod.inputs_sha256(g)
    g[0]["expected_actions"] = [{"id": "e1", "type": "create_item", "match_hint": "x"}]
    assert gold_mod.inputs_sha256(g) == h
    g[0]["instruction"] = "changed"
    assert gold_mod.inputs_sha256(g) != h


def test_compare_refuses_runs_submitted_from_different_instructions():
    a, b = _summary(recall=0.6), _summary(recall=0.7)
    a["submitted_inputs_sha256"], b["submitted_inputs_sha256"] = "x", "y"
    with pytest.raises(compare_mod.CompareRefused, match="instructions"):
        compare_mod.compare([a, b], [])


def test_cli_fetch_uses_the_client_factory(monkeypatch, runs_dir, no_sockets, tmp_path):
    api = FakeApi()
    api.packs = {f"{S}f1": {"id": f"{S}f1", "state": "ready"}}
    monkeypatch.setenv("ACTION_PACK_EVAL_TOKEN", "tok")
    monkeypatch.setattr(live, "urllib_client", lambda timeout: api)
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({"run_id": "r", "packs": [
        {"gold_id": "G1", "pack_id": f"{S}f1", "pack": None}]}), encoding="utf-8")
    out = runs_dir / "fetched.json"
    assert cli.main(["fetch", "--run", str(prior), "--base-url", "https://t/api/org",
                     "--out", str(out)]) == 0
    assert [c[:2] for c in api.calls] == [("GET", f"https://t/api/org/action-packs/{S}f1")]
    assert json.loads(out.read_text(encoding="utf-8"))["packs"][0]["pack"]["state"] == "ready"
