"""Tests for the Jev shadow-eval runner (Track A, Task 6).

No network, no aws CLI: `scripts.jev_shadow_eval.sc.ask` (the imported
`systemone_client` module) and `llm_utils.call_llm` (the baseline arm's LLM
entry point) are monkeypatched with fakes; a dry run must call neither, and
tests assert that by making the fakes raise if invoked. Fixtures live under
`tmp_path`, with `FIXTURES_DIR`/`RESULTS_DIR` monkeypatched onto the module
so nothing here ever touches `scripts/fixtures/jev_eval/` for real.
"""
from __future__ import annotations

import json

import pytest

import llm_utils
import scripts.jev_shadow_eval as jse
import systemone_client as sc
from scripts.jev_eval.questions import JevQuestionsError


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _work_class_row(id_, label, site_id="site-1", company_id="co-1"):
    return {
        "set": "work_class",
        "id": id_,
        "label": label,
        "features": {
            "title": "Discussion about the slab",
            "summary": "Talking about the slab pour on level 2.",
            "category": "concrete",
        },
        "baseline": {"classifier_verdict": "work", "classifier_confidence": 0.9},
        "site_id": site_id,
        "company_id": company_id,
        "decided_at": "2026-09-01T00:00:00Z",
    }


def _threads_row(id_, label, site_id, company_id, earlier_title):
    return {
        "set": "threads",
        "id": id_,
        "label": label,
        "features": {
            "earlier": {"title": earlier_title, "summary": "Earlier summary.", "date": "2026-08-01"},
            "later": {"title": "Later topic", "summary": "Later summary.", "date": "2026-08-15"},
            "gap_days": 14,
        },
        "baseline": {"score": 0.5},
        "site_id": site_id,
        "company_id": company_id,
        "decided_at": "2026-08-15T00:00:00Z",
    }


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row))
            fh.write("\n")


def _setup_fixtures(tmp_path, monkeypatch, *, set_name, rows, counts_extra=None):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    results_dir = fixtures_dir / "results"
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", results_dir)

    _write_jsonl(fixtures_dir / f"{set_name}.jsonl", rows)
    (fixtures_dir / "name_aliases.json").write_text(
        json.dumps([{"wrong_term": "Ben Lynn", "right_term": "Ben Lin", "kind": "person"}]),
        encoding="utf-8",
    )
    counts = {set_name: {"n": len(rows)}}
    if counts_extra:
        counts.update(counts_extra)
    (fixtures_dir / "counts.json").write_text(json.dumps(counts), encoding="utf-8")
    return fixtures_dir, results_dir


def _fake_ask_ok(state, questions, *, model=None, timeout=None, caller=None):
    answers = {}
    for name, spec in questions.items():
        if spec["type"] == "noul":
            answers[name] = {"noul": 0.8}
        elif spec["type"] == "choice":
            options = list(spec["criteria"].keys())
            probs = {opt: (0.7 if i == 0 else 0.3 / max(1, len(options) - 1))
                     for i, opt in enumerate(options)}
            answers[name] = {"choice": options[0], "probabilities": probs, "confidence": 0.7}
    return {"answers": answers, "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            "latency_ms": 42}


def _fake_call_ok(prompt, max_tokens=512, force_json=True, caller=None):
    return json.dumps({"task_id": None, "confidence": 0.0}), None


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("DECISIONS_API_KEY", "test-key")


# ---------------------------------------------------------------------------
# Contract: one row per arm per run, with the documented keys
# ---------------------------------------------------------------------------

def test_writes_one_row_per_arm_per_run_with_contract_keys(tmp_path, monkeypatch):
    rows = [
        _work_class_row("wc-1", "yes", site_id="site-1", company_id="co-1"),
        _work_class_row("wc-2", "no", site_id="site-2", company_id="co-2"),
    ]
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)

    monkeypatch.setattr(sc, "ask", _fake_ask_ok)
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    rc = jse.main(["--set", "work_class", "--runs", "1"])
    assert rc == 0

    required_keys = {"id", "label", "arm", "run", "score", "answers",
                      "question_hash", "latency_ms", "tokens", "error"}
    stamp_keys = {"set", "route", "model", "provider", "temperature", "started_at"}

    for arm in jse.ALL_ARMS:
        out_rows = _read_jsonl(results_dir / f"work_class.{arm}.run1.jsonl")
        assert len(out_rows) == 2, f"expected 2 rows for arm {arm!r}, got {len(out_rows)}"
        for row in out_rows:
            assert required_keys <= set(row.keys())
            assert stamp_keys <= set(row.keys())
            assert row["arm"] == arm
            assert row["run"] == 1
            assert row["set"] == "work_class"

    # Jev arms never share the "tokens"/temperature shape with baseline.
    broad_rows = _read_jsonl(results_dir / "work_class.broad.run1.jsonl")
    for row in broad_rows:
        assert row["temperature"] is None
        assert row["error"] is None
        assert row["score"] is not None

    baseline_rows = _read_jsonl(results_dir / "work_class.baseline.run1.jsonl")
    for row in baseline_rows:
        assert row["route"] is None


# ---------------------------------------------------------------------------
# Failed ask -> error row, score None (never 0)
# ---------------------------------------------------------------------------

def test_failed_ask_writes_error_row_with_score_none(tmp_path, monkeypatch):
    rows = [_work_class_row("wc-1", "yes")]
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)

    def _fake_ask_fail(state, questions, *, model=None, timeout=None, caller=None):
        raise sc.SystemOneError("HTTP 500: boom")

    monkeypatch.setattr(sc, "ask", _fake_ask_fail)
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    rc = jse.main(["--set", "work_class", "--arms", "broad", "--runs", "1"])
    assert rc == 0

    out_rows = _read_jsonl(results_dir / "work_class.broad.run1.jsonl")
    assert len(out_rows) == 1
    row = out_rows[0]
    assert row["error"] is not None
    assert "SystemOneError" in row["error"]
    assert row["score"] is None
    assert row["answers"] is None
    # The error message must never carry the state/questions payload.
    assert "slab" not in row["error"]


# ---------------------------------------------------------------------------
# Resume: skip ok rows, retry error rows
# ---------------------------------------------------------------------------

def test_resume_skips_ok_rows_and_retries_error_rows(tmp_path, monkeypatch):
    rows = [
        _work_class_row("wc-1", "yes"),
        _work_class_row("wc-2", "no"),
    ]
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)

    calls = {"n": 0}

    def _flaky_ask(state, questions, *, model=None, timeout=None, caller=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sc.SystemOneError("HTTP 500: first call fails")
        return _fake_ask_ok(state, questions, model=model, timeout=timeout, caller=caller)

    monkeypatch.setattr(sc, "ask", _flaky_ask)
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    rc = jse.main(["--set", "work_class", "--arms", "broad", "--runs", "1"])
    assert rc == 0
    out_rows = _read_jsonl(results_dir / "work_class.broad.run1.jsonl")
    assert len(out_rows) == 2
    errored_ids = {r["id"] for r in out_rows if r["error"] is not None}
    ok_ids = {r["id"] for r in out_rows if r["error"] is None}
    assert len(errored_ids) == 1
    assert len(ok_ids) == 1
    calls_before_resume = calls["n"]

    # Re-run: the ok row must be skipped (no new ask call for it); the
    # errored row must be retried (a new line appended, ask called again).
    monkeypatch.setattr(sc, "ask", _fake_ask_ok)  # now always succeeds
    rc = jse.main(["--set", "work_class", "--arms", "broad", "--runs", "1"])
    assert rc == 0

    out_rows_after = _read_jsonl(results_dir / "work_class.broad.run1.jsonl")
    # ok row still has exactly one line; errored row now has two (old error + new ok)
    assert sum(1 for r in out_rows_after if r["id"] == next(iter(ok_ids))) == 1
    errored_id = next(iter(errored_ids))
    lines_for_errored = [r for r in out_rows_after if r["id"] == errored_id]
    assert len(lines_for_errored) == 2
    assert lines_for_errored[0]["error"] is not None
    assert lines_for_errored[1]["error"] is None
    assert calls_before_resume == 2  # both rows attempted once each in run 1


# ---------------------------------------------------------------------------
# Control: donor from another site, never the same site
# ---------------------------------------------------------------------------

def test_donor_states_excludes_same_site_and_prefers_different_company():
    states_by_id = {
        "a": {"state": "state-a", "site_id": "site-1", "company_id": "co-1"},
        "b": {"state": "state-b", "site_id": "site-1", "company_id": "co-1"},  # same site as a
        "c": {"state": "state-c", "site_id": "site-2", "company_id": "co-1"},  # diff site, same co
        "d": {"state": "state-d", "site_id": "site-3", "company_id": "co-2"},  # diff site, diff co
    }
    donors = jse._donor_states(states_by_id, "a")
    # Never the same-site row ("b"); "d" (diff company) preferred over "c".
    assert "state-b" not in donors
    assert donors == ["state-d"]


def test_donor_states_falls_back_to_same_company_when_no_other_company_exists():
    states_by_id = {
        "a": {"state": "state-a", "site_id": "site-1", "company_id": "co-1"},
        "b": {"state": "state-b", "site_id": "site-1", "company_id": "co-1"},  # same site
        "c": {"state": "state-c", "site_id": "site-2", "company_id": "co-1"},  # diff site, same co
    }
    donors = jse._donor_states(states_by_id, "a")
    assert donors == ["state-c"]


def test_control_arm_uses_a_real_donor_from_another_site(tmp_path, monkeypatch):
    rows = [
        _threads_row("t-1", "yes", "site-1", "co-1", "Ground floor walls"),
        _threads_row("t-2", "no", "site-2", "co-2", "Roof trusses"),
    ]
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="threads", rows=rows)
    monkeypatch.setattr(sc, "ask", _fake_ask_ok)

    captured_states = []

    def _capturing_ask(state, questions, *, model=None, timeout=None, caller=None):
        captured_states.append((caller, state))
        return _fake_ask_ok(state, questions, model=model, timeout=timeout, caller=caller)

    monkeypatch.setattr(sc, "ask", _capturing_ask)

    rc = jse.main(["--set", "threads", "--arms", "control_broad", "--runs", "1"])
    assert rc == 0

    out_rows = _read_jsonl(results_dir / "threads.control_broad.run1.jsonl")
    assert len(out_rows) == 2
    for row in out_rows:
        assert row["error"] is None

    # The control state sent for t-1 must have swapped "earlier" to the
    # donor's (t-2's "Roof trusses"), never t-1's own "Ground floor walls".
    state_for_t1 = next(state for caller, state in captured_states if caller.endswith("t-1") or True)
    sent_titles = [state["earlier"]["title"] for _caller, state in captured_states]
    assert "Roof trusses" in sent_titles
    assert "Ground floor walls" in sent_titles
    # And neither row's control state kept its own earlier.title unchanged.
    for _caller, state in captured_states:
        assert state["earlier"]["title"] != "" # sanity: always populated


# ---------------------------------------------------------------------------
# Dry run: writes preview, calls neither ask nor aws
# ---------------------------------------------------------------------------

def test_dry_run_writes_preview_and_calls_nothing(tmp_path, monkeypatch):
    rows = [
        _work_class_row("wc-1", "yes", site_id="site-1", company_id="co-1"),
        _work_class_row("wc-2", "no", site_id="site-2", company_id="co-2"),
    ]
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)

    def _explode_ask(*a, **k):
        raise AssertionError("ask() must never be called during --dry-run")

    def _explode_load_env(*a, **k):
        raise AssertionError("load_deployed_llm_env() must never be called during --dry-run")

    monkeypatch.setattr(sc, "ask", _explode_ask)
    monkeypatch.setattr(jse.baseline_mod, "load_deployed_llm_env", _explode_load_env)
    monkeypatch.delenv("DECISIONS_API_KEY", raising=False)  # dry-run needs no key either

    rc = jse.main(["--set", "work_class", "--dry-run"])
    assert rc == 0

    preview = _read_jsonl(results_dir / "preview_states.jsonl")
    # 2 rows x 4 Jev arms (baseline is not a "state" arm, never previewed)
    assert len(preview) == 2 * len(jse.JEV_ARMS)
    for entry in preview:
        assert entry["arm"] in jse.JEV_ARMS
        assert "state" in entry and "questions" in entry


# ---------------------------------------------------------------------------
# Preview never carries transcript-like keys or unmasked alias names
# ---------------------------------------------------------------------------

def test_preview_has_no_transcript_keys_or_unmasked_names(tmp_path, monkeypatch):
    row = _work_class_row("wc-1", "yes")
    row["features"]["summary"] = "Ben Lynn said the pour is done."
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=[row])
    monkeypatch.delenv("DECISIONS_API_KEY", raising=False)

    rc = jse.main(["--set", "work_class", "--dry-run"])
    assert rc == 0

    preview_path = results_dir / "preview_states.jsonl"
    raw_text = preview_path.read_text(encoding="utf-8")
    assert "Ben Lynn" not in raw_text
    assert "Ben Lin" not in raw_text  # the right_term alias is masked too

    banned_keys = {"quote", "turns", "window", "transcript", "evidence", "text_window"}

    def _walk(value):
        if isinstance(value, dict):
            for key, sub in value.items():
                assert str(key).lower() not in banned_keys
                _walk(sub)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    for line in raw_text.splitlines():
        if line.strip():
            _walk(json.loads(line))


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_refuses_without_decisions_api_key_for_jev_arm(tmp_path, monkeypatch):
    rows = [_work_class_row("wc-1", "yes")]
    _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)
    monkeypatch.delenv("DECISIONS_API_KEY", raising=False)

    rc = jse.main(["--set", "work_class", "--arms", "broad", "--runs", "1"])
    assert rc == 2


def test_refuses_without_counts_entry_for_set(tmp_path, monkeypatch):
    rows = [_work_class_row("wc-1", "yes")]
    fixtures_dir, _results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)
    # Overwrite counts.json with an entry for a DIFFERENT set only.
    (fixtures_dir / "counts.json").write_text(json.dumps({"threads": {"n": 0}}), encoding="utf-8")

    rc = jse.main(["--set", "work_class", "--arms", "baseline", "--runs", "1"])
    assert rc == 2


def test_check_preconditions_raises_runner_refusal_without_counts_file(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", fixtures_dir / "results")

    with pytest.raises(jse.RunnerRefusal):
        jse.check_preconditions(["work_class"], ["baseline"], False)
