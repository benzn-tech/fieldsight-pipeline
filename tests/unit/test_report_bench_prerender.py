"""Report bench, Task 8: S3 template check and per-session pre-render."""
import importlib.util
import json
import os
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


tc = _load("rb_template_check")
pre = _load("rb_prerender")
common = _load("rb_common")


def test_synthetic_match():
    assert tc.compare("A {x} B {y} C", "A 1 B 2\n3 C")["identical"]
    assert tc.compare('A {x} {{"k": 1}}', 'A 7 {"k": 1}')["identical"]


def test_synthetic_mismatch_names_the_segment():
    r = tc.compare("A {x} B {y} C", "A 1 Bz 2 C")
    assert r["identical"] is False and r["missing_static_segments"] == [1]


def test_git_v3_rendered_by_develop_is_identical(tmp_path, monkeypatch):
    import lambda_report_generator as rg
    tm = common.read_json(tc.GIT_TEMPLATES)
    monkeypatch.setattr(rg, "_prompt_templates_cache", tm)
    prompt = rg.build_daily_prompt(
        [{"time_str": "09:00", "device": "Dev1", "text": "Pour at 2pm {not a field}",
          "related_photos": []}],
        "Ben_Lin_test2", "Site A", "2026-10-01", role="PM", total_duration=65.0, num_photos=0)
    debug = tmp_path / "d.json"
    debug.write_text(json.dumps({"user_name": "Ben_Lin_test2", "target_date": "2026-10-01",
                                 "prompt": prompt}), encoding="utf-8")
    r = tc.check(str(debug))
    assert r["identical"] is True and r["missing_static_segments"] == []
    debug.write_text(json.dumps({"user_name": "Ben_Lin_test2",
                                 "prompt": prompt.replace("## ", "### ", 1)}), encoding="utf-8")
    assert tc.check(str(debug))["identical"] is False


def test_summary_debug_refused(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"user_name": "_summary", "prompt": "x"}), encoding="utf-8")
    with pytest.raises(PermissionError):
        tc.check(str(p))


def _setup(out):
    manifest = {"sidA": {"folder": "Ben_Lin_test2", "date": "2026-10-01", "bucket": "b",
                         "role": "set", "n_segments": 3, "n_turns": 2, "sha256": "x"},
                "sidH": {"folder": "Ben_Lin_test2", "date": "2026-10-02", "bucket": "b",
                         "role": "heldout", "n_segments": 1, "n_turns": 1, "sha256": "y"},
                "R": {"Ben_Lin_test2_2026-10-01": {"n_transcripts": 4}}}
    for sid in ("sidA", "sidH"):
        common.write_json(os.path.join(out, "inputs", "X", sid + ".turns.json"),
                          [{"abs_start_str": "09:00:00", "speaker": "spk_0", "text": "t"}])
    common.write_json(os.path.join(out, "inputs", "R", "Ben_Lin_test2_2026-10-01.json"),
                      {"kwargs": {"user_name": "Ben_Lin_test2"}, "n_transcripts": 4})
    return manifest


CANDS = {"X-0901": {"task": "X", "resolved": "s1", "names": False},
         "X-cur": {"task": "X", "resolved": "s2", "names": True},
         "T-cur": {"task": "T", "resolved": "s3", "names": True}}


def _stub(calls):
    def render(sha, task, inputs):
        calls.append((sha, task, inputs))
        return f"{task}|{sha}|{inputs.get('session_base') or inputs.get('scope', {}).get('date')}"
    return render


def _pin(out, runs_dir):
    ans = os.path.join(runs_dir, "a.txt")
    common.write_text(ans, '{"topics": []}')
    runs = os.path.join(runs_dir, "pilot.jsonl")
    common.append_jsonl(runs, {"task": "X", "candidate": "X-cur", "model": "muse", "rep": 1,
                               "outcome": "ok", "session": "sidA", "key": "k", "text_path": ans})
    return pre.pin_tcur_input([runs], out)


def test_prerender_writes_one_file_per_key(tmp_path, monkeypatch):
    out = str(tmp_path / "out")
    m = _setup(out)
    common.write_json(os.path.join(out, "inputs", "X", "sidA.names.json"),
                      [{"source_filename": "f", "speaker_label": "spk_0", "display_name": "A"}])
    pins = _pin(out, str(tmp_path))
    assert set(pins) == {"sidA"}
    monkeypatch.setattr(pre.rb_snapshot, "resolve", lambda ref, repo=None: "dev")
    calls = []
    idx = pre.run(CANDS, m, out, tasks=("X", "T", "R"), r_templates={"daily_report": {}},
                  renderer=_stub(calls))
    assert set(idx["X-0901"]) == {"sidA", "sidH"}
    assert set(idx["T-cur"]) == {"sidA"}                 # held-out not pinned yet
    assert set(idx["R-cur"]) == {"Ben_Lin_test2_2026-10-01"}
    for cid, keys in idx.items():
        for key, e in keys.items():
            p = os.path.join(out, "prompts", cid, key + ".txt")
            with open(p, encoding="utf-8", newline="") as f:
                assert common.sha256_text(f.read()) == e["sha256"]
    x0901 = [c for c in calls if c[0] == "s1"]
    xcur = [c for c in calls if c[0] == "s2" and c[2]["session_base"] == "sidA"]
    assert all("speaker_names" not in c[2] for c in x0901)
    assert "speaker_names" in xcur[0][2]
    assert idx["X-cur"]["sidA"]["n"] == 3 and idx["R-cur"]["Ben_Lin_test2_2026-10-01"]["n"] == 4
    assert common.read_json(os.path.join(out, "prompts", "index.json")) == idx


def test_tcur_tampered_pin_refused(tmp_path):
    out = str(tmp_path / "out")
    m = _setup(out)
    _pin(out, str(tmp_path))
    with open(os.path.join(out, "inputs", "T", "sidA.extraction.json"), "a") as f:
        f.write(" ")
    with pytest.raises(pre.PinError):
        pre.run({"T-cur": CANDS["T-cur"]}, m, out, tasks=("T",), renderer=_stub([]))


def test_tcur_missing_pin_refused(tmp_path):
    out = str(tmp_path / "out")
    m = _setup(out)
    with pytest.raises(pre.PinError):
        pre.run({"T-cur": CANDS["T-cur"]}, m, out, tasks=("T",), renderer=_stub([]))


def test_r_needs_template(tmp_path):
    out = str(tmp_path / "out")
    m = _setup(out)
    with pytest.raises(ValueError):
        pre.run({}, m, out, tasks=("R",), renderer=_stub([]))
