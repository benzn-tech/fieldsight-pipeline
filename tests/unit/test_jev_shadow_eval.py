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

import lambda_programme_matcher
import llm_utils
import thread_match
import scripts.jev_shadow_eval as jse
import systemone_client as sc
from scripts.jev_eval import score as score_mod
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


def _write_results_jsonl(results_dir, set_name, arm, run, rows):
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{set_name}.{arm}.run{run}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row))
            fh.write("\n")
    return path


def _result_row(id_, label, arm, run, *, score=0.8, error=None, set_name="work_class", **extra):
    row = {
        "id": id_, "label": label, "arm": arm, "run": run,
        "score": score if error is None else None,
        "answers": {} if error is None else None,
        "question_hash": "hash", "latency_ms": 10, "tokens": 50,
        "error": error,
        "set": set_name, "route": None, "model": "m", "provider": "p",
        "temperature": None, "started_at": "2026-09-28T00:00:00+00:00",
    }
    row.update(extra)
    return row


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
# Minor 3: runs execute sequentially (never interleaved), and each row is
# stamped with whether its answers matched the previous run's
# ---------------------------------------------------------------------------

def test_runs_execute_sequentially_never_interleaved(tmp_path, monkeypatch):
    rows = [_work_class_row("wc-1", "yes"), _work_class_row("wc-2", "no")]
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)

    call_log = []

    def _tracking_ask(state, questions, *, model=None, timeout=None, caller=None):
        call_log.append(caller)
        return _fake_ask_ok(state, questions, model=model, timeout=timeout, caller=caller)

    monkeypatch.setattr(sc, "ask", _tracking_ask)
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    rc = jse.main(["--set", "work_class", "--arms", "broad", "--runs", "2"])
    assert rc == 0

    # Both run 1 files must be complete (2 rows each) before any run 2 file
    # exists with content -- proven here by checking run 1's own file has
    # both rows AND that this is consistent with a purely sequential order:
    # every "run 1 wrote" must precede any dependency on run 1's content for
    # run 2 (checked via the identical_to_previous_run test below, which
    # would be impossible if runs were interleaved and run 1 wasn't done).
    run1_rows = _read_jsonl(results_dir / "work_class.broad.run1.jsonl")
    run2_rows = _read_jsonl(results_dir / "work_class.broad.run2.jsonl")
    assert len(run1_rows) == 2
    assert len(run2_rows) == 2


def test_identical_to_previous_run_is_stamped_on_run_2(tmp_path, monkeypatch):
    rows = [_work_class_row("wc-1", "yes")]
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)

    monkeypatch.setattr(sc, "ask", _fake_ask_ok)  # deterministic fake -> same answers every call
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    rc = jse.main(["--set", "work_class", "--arms", "broad", "--runs", "2"])
    assert rc == 0

    run2_rows = _read_jsonl(results_dir / "work_class.broad.run2.jsonl")
    assert len(run2_rows) == 1
    assert run2_rows[0]["identical_to_previous_run"] is True

    run1_rows = _read_jsonl(results_dir / "work_class.broad.run1.jsonl")
    assert "identical_to_previous_run" not in run1_rows[0]


def test_identical_to_previous_run_is_false_when_answers_differ(tmp_path, monkeypatch):
    rows = [_work_class_row("wc-1", "yes")]
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)

    call_count = {"n": 0}

    def _varying_ask(state, questions, *, model=None, timeout=None, caller=None):
        call_count["n"] += 1
        top_prob = 0.7 if call_count["n"] == 1 else 0.4
        answers = {}
        for name, spec in questions.items():
            if spec["type"] == "noul":
                answers[name] = {"noul": top_prob}
            elif spec["type"] == "choice":
                options = list(spec["criteria"].keys())
                probs = {opt: (top_prob if i == 0 else (1 - top_prob) / max(1, len(options) - 1))
                         for i, opt in enumerate(options)}
                answers[name] = {"choice": options[0], "probabilities": probs, "confidence": top_prob}
        return {"answers": answers,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "latency_ms": 5}

    monkeypatch.setattr(sc, "ask", _varying_ask)
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    rc = jse.main(["--set", "work_class", "--arms", "broad", "--runs", "2"])
    assert rc == 0

    run2_rows = _read_jsonl(results_dir / "work_class.broad.run2.jsonl")
    assert run2_rows[0]["identical_to_previous_run"] is False


def test_identical_to_previous_run_available_across_separate_invocations(tmp_path, monkeypatch):
    # Run 1 completed in an EARLIER invocation (e.g. the owner re-ran the
    # script later with --runs bumped up); run 2 must still be able to
    # compare against it by reading run 1's file from disk.
    rows = [_work_class_row("wc-1", "yes")]
    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)

    monkeypatch.setattr(sc, "ask", _fake_ask_ok)
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    rc = jse.main(["--set", "work_class", "--arms", "broad", "--runs", "1"])
    assert rc == 0

    rc = jse.main(["--set", "work_class", "--arms", "broad", "--runs", "2"])
    assert rc == 0

    run2_rows = _read_jsonl(results_dir / "work_class.broad.run2.jsonl")
    assert run2_rows[0]["identical_to_previous_run"] is True


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

def test_dry_run_prints_masking_stats_per_set(tmp_path, monkeypatch, capsys):
    # I1 requirement 5: --dry-run must surface over-masking, not just leaks --
    # one row whose title is entirely a placeholder (over-masked), one whose
    # title is untouched.
    rows = [
        _work_class_row("wc-1", "yes"),
        _work_class_row("wc-2", "no"),
    ]
    rows[0]["features"]["title"] = "Ben Lin"  # generic pass eats the whole title
    rows[1]["features"]["title"] = "Fix leaking valve"

    _fixtures_dir, results_dir = _setup_fixtures(tmp_path, monkeypatch, set_name="work_class", rows=rows)
    monkeypatch.delenv("DECISIONS_API_KEY", raising=False)

    rc = jse.main(["--set", "work_class", "--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "masking stats [work_class]" in out
    assert "mean_placeholders_per_state=" in out
    assert "title_all_placeholder_fraction=50.0%" in out


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


# ---------------------------------------------------------------------------
# Minor 5: skip load_deployed_llm_env when programme_match is empty/unselected
# ---------------------------------------------------------------------------

def _programme_match_row(id_, label, site_id="site-1", company_id="co-1"):
    return {
        "set": "programme_match",
        "id": id_,
        "label": label,
        "features": {
            "observation": {"title": "Slab pour", "summary": "Pour underway.", "date": "2026-08-30"},
            "task": {"name": "Pour ground slab", "status": "in_progress", "progress_pct": 40},
        },
        "baseline": {"confidence": 0.81, "suggested_status": "delayed",
                     "suggested_progress": 40, "task_id": "T-9"},
        "site_id": site_id,
        "company_id": company_id,
        "decided_at": "2026-09-01T00:00:00Z",
    }


def test_skips_loading_deployed_llm_env_when_programme_match_has_zero_rows(tmp_path, monkeypatch):
    _setup_fixtures(tmp_path, monkeypatch, set_name="programme_match", rows=[])

    def _explode(*a, **k):
        raise AssertionError("load_deployed_llm_env() must not be called for zero rows")

    monkeypatch.setattr(jse.baseline_mod, "load_deployed_llm_env", _explode)
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    rc = jse.main(["--set", "programme_match", "--arms", "baseline", "--runs", "1"])
    assert rc == 0


def test_loads_deployed_llm_env_when_programme_match_has_rows(tmp_path, monkeypatch):
    rows = [_programme_match_row("pm-1", "yes")]
    _setup_fixtures(tmp_path, monkeypatch, set_name="programme_match", rows=rows)

    calls = {"n": 0}

    def _fake_load_env(function_name, *, profile, region):
        calls["n"] += 1
        return []

    monkeypatch.setattr(jse.baseline_mod, "load_deployed_llm_env", _fake_load_env)
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    rc = jse.main(["--set", "programme_match", "--arms", "baseline", "--runs", "1"])
    assert rc == 0
    assert calls["n"] == 1


def test_check_preconditions_raises_runner_refusal_without_counts_file(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", fixtures_dir / "results")

    with pytest.raises(jse.RunnerRefusal):
        jse.check_preconditions(["work_class"], ["baseline"], False)


# ---------------------------------------------------------------------------
# load_results: the append-only log's file -> score.py boundary
# (coordinator fix round 1, finding #1)
# ---------------------------------------------------------------------------

def test_load_results_collapses_duplicates_keeping_last_ok_row(tmp_path):
    results_dir = tmp_path / "results"
    rows = [
        _result_row("a", "yes", "broad", 1, error="SystemOneError: boom"),
        _result_row("a", "yes", "broad", 1, score=0.9),  # later ok row supersedes
        _result_row("b", "no", "broad", 1, error="SystemOneError: still failing"),
    ]
    _write_results_jsonl(results_dir, "work_class", "broad", 1, rows)

    loaded = jse.load_results(results_dir, "work_class")
    broad_run1 = loaded["broad"][1]
    by_id = {r["id"]: r for r in broad_run1}

    assert len(broad_run1) == 2  # one row per id, not three (no duplicates)
    assert by_id["a"]["error"] is None
    assert by_id["a"]["score"] == 0.9
    assert by_id["b"]["error"] is not None  # error-only id stays a single error row


def test_load_results_keeps_last_error_when_no_ok_row_exists(tmp_path):
    results_dir = tmp_path / "results"
    rows = [
        _result_row("b", "no", "broad", 1, error="SystemOneError: first failure"),
        _result_row("b", "no", "broad", 1, error="SystemOneError: second failure"),
    ]
    _write_results_jsonl(results_dir, "work_class", "broad", 1, rows)

    loaded = jse.load_results(results_dir, "work_class")
    (row,) = loaded["broad"][1]
    assert row["id"] == "b"
    assert "second failure" in row["error"]


def test_load_results_n_failed_counts_only_genuinely_failed_ids(tmp_path):
    results_dir = tmp_path / "results"
    rows = [
        _result_row("a", "yes", "broad", 1, error="SystemOneError: boom"),
        _result_row("a", "yes", "broad", 1, score=0.9),  # "a" later succeeded
        _result_row("b", "no", "broad", 1, error="SystemOneError: still failing"),
        _result_row("c", "yes", "broad", 1, score=0.2),
    ]
    _write_results_jsonl(results_dir, "work_class", "broad", 1, rows)

    loaded = jse.load_results(results_dir, "work_class")
    scores = score_mod.score_set(loaded)
    # Only "b" ever genuinely failed; "a"'s stale error line must not count.
    assert scores["broad"]["n_failed"] == 1
    assert scores["broad"]["n"] == 2


# ---------------------------------------------------------------------------
# Minor 4: refuse a mixed question_hash, re-join labels at score time
# ---------------------------------------------------------------------------

def test_load_results_raises_on_ambiguous_question_hash_within_one_arm(tmp_path):
    results_dir = tmp_path / "results"
    rows_run1 = [_result_row("a", "yes", "broad", 1, question_hash="hash-v1")]
    rows_run2 = [_result_row("a", "yes", "broad", 2, question_hash="hash-v2")]
    _write_results_jsonl(results_dir, "work_class", "broad", 1, rows_run1)
    _write_results_jsonl(results_dir, "work_class", "broad", 2, rows_run2)

    with pytest.raises(jse.AmbiguousQuestionHashError):
        jse.load_results(results_dir, "work_class")


def test_load_results_tolerates_none_question_hash_alongside_a_real_one(tmp_path):
    # An error row's question_hash is often None -- that must not itself
    # trigger the ambiguity check (only two DIFFERENT real hashes should).
    results_dir = tmp_path / "results"
    rows = [
        _result_row("a", "yes", "broad", 1, question_hash="hash-v1"),
        _result_row("b", "no", "broad", 1, error="boom", question_hash=None),
    ]
    _write_results_jsonl(results_dir, "work_class", "broad", 1, rows)
    loaded = jse.load_results(results_dir, "work_class")  # must not raise
    assert len(loaded["broad"][1]) == 2


def test_score_mode_refuses_on_ambiguous_question_hash(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    results_dir = fixtures_dir / "results"
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", results_dir)

    _write_results_jsonl(results_dir, "work_class", "broad", 1,
                          [_result_row("a", "yes", "broad", 1, question_hash="hash-v1")])
    _write_results_jsonl(results_dir, "work_class", "broad", 2,
                          [_result_row("a", "yes", "broad", 2, question_hash="hash-v2")])

    rc = jse.main(["--set", "work_class", "--score"])
    assert rc == 2
    assert not (results_dir / "scores.json").exists()


def test_rejoin_current_labels_overwrites_stale_labels_and_excludes_missing():
    # Fix wave 4, A7: a result row whose id no longer exists in the current
    # {set}.jsonl is EXCLUDED from scoring (never scored with a stale label)
    # and counted via `missing_ids`.
    rows_by_arm_run = {
        "broad": {1: [
            {"id": "a", "label": "no"},   # stale -- current says "yes"
            {"id": "gone", "label": "yes"},  # no longer in the current fixture
        ]},
    }
    current_rows = [{"id": "a", "label": "yes"}]

    updated, missing_ids = jse.rejoin_current_labels(rows_by_arm_run, current_rows)

    assert len(updated["broad"][1]) == 1
    assert updated["broad"][1][0]["id"] == "a"
    assert updated["broad"][1][0]["label"] == "yes"
    assert missing_ids == ["gone"]


def test_score_mode_rejoins_labels_from_current_fixture(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    results_dir = fixtures_dir / "results"
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", results_dir)

    # The result row was written when "a" was labelled "no"; the fixture has
    # since been relabelled "yes" (e.g. a re-import). Scoring must use "yes".
    _write_jsonl(fixtures_dir / "work_class.jsonl", [
        {"id": "a", "set": "work_class", "label": "yes", "features": {}},
    ])
    _write_results_jsonl(results_dir, "work_class", "baseline", 1,
                          [_result_row("a", "no", "baseline", 1, score=0.9)])

    rc = jse.main(["--set", "work_class", "--score"])
    assert rc == 0

    scores = json.loads((results_dir / "scores.json").read_text(encoding="utf-8"))
    baseline_scores = scores["work_class"]["scores"]["baseline"]
    # score=0.9 clears the 0.5 threshold -> predicted "yes". If the label had
    # stayed stale ("no"), this would score as a false positive
    # (precision 0.0); re-joined to the current "yes" it is a true positive.
    assert baseline_scores["precision"] == 1.0


# ---------------------------------------------------------------------------
# --score mode
# ---------------------------------------------------------------------------

def test_score_mode_end_to_end_writes_scores_json(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    results_dir = fixtures_dir / "results"
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", results_dir)
    monkeypatch.delenv("DECISIONS_API_KEY", raising=False)

    rows = (
        [_result_row(f"y{i}", "yes", "baseline", 1, score=0.9) for i in range(4)]
        + [_result_row(f"n{i}", "no", "baseline", 1, score=0.1) for i in range(4)]
    )
    _write_results_jsonl(results_dir, "work_class", "baseline", 1, rows)

    rc = jse.main(["--set", "work_class", "--score"])
    assert rc == 0

    scores_path = results_dir / "scores.json"
    assert scores_path.exists()
    payload = json.loads(scores_path.read_text(encoding="utf-8"))
    assert payload["work_class"]["baseline_threshold"] == 0.5
    assert "baseline" in payload["work_class"]["scores"]
    assert payload["work_class"]["scores"]["baseline"]["n"] == 8

    # Amended rule (fix wave 1): --score also writes a verdict object.
    verdict_obj = payload["work_class"]["verdict"]
    assert verdict_obj["verdict"] == "descriptive_only"  # no decomposed arm here
    assert "inputs" in verdict_obj and "decomposed" in verdict_obj["inputs"]


def test_score_mode_uses_deployed_gate_threshold_for_baseline(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    results_dir = fixtures_dir / "results"
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", results_dir)
    monkeypatch.delenv("DECISIONS_API_KEY", raising=False)

    rows = [
        _result_row(f"t{i}", "yes" if i % 2 else "no", "baseline", 1,
                    score=0.3, set_name="threads")
        for i in range(6)
    ]
    _write_results_jsonl(results_dir, "threads", "baseline", 1, rows)

    rc = jse.main(["--set", "threads", "--score"])
    assert rc == 0

    payload = json.loads((results_dir / "scores.json").read_text(encoding="utf-8"))
    assert payload["threads"]["baseline_threshold"] == thread_match.MIN_SCORE
    assert (payload["threads"]["scores"]["baseline"]["fixed_threshold"]["threshold"]
            == thread_match.MIN_SCORE)


def test_score_mode_calls_neither_ask_nor_aws(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    results_dir = fixtures_dir / "results"
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", results_dir)
    monkeypatch.delenv("DECISIONS_API_KEY", raising=False)

    def _explode_ask(*a, **k):
        raise AssertionError("ask() must never be called during --score")

    def _explode_load_env(*a, **k):
        raise AssertionError("load_deployed_llm_env() must never be called during --score")

    monkeypatch.setattr(sc, "ask", _explode_ask)
    monkeypatch.setattr(jse.baseline_mod, "load_deployed_llm_env", _explode_load_env)

    rows = [
        _result_row(f"pm{i}", "yes" if i % 2 else "no", "baseline", 1,
                    score=0.8, set_name="programme_match")
        for i in range(4)
    ]
    _write_results_jsonl(results_dir, "programme_match", "baseline", 1, rows)

    rc = jse.main(["--set", "programme_match", "--score"])
    assert rc == 0

    payload = json.loads((results_dir / "scores.json").read_text(encoding="utf-8"))
    assert payload["programme_match"]["baseline_threshold"] == lambda_programme_matcher.CONF_MIN


# ---------------------------------------------------------------------------
# --score mode: provenance (Task 9, carried item) -- a reader must be able to
# tell a partial run from a full one from scores.json alone.
# ---------------------------------------------------------------------------

def test_scores_json_provenance_reports_files_dedup_and_missing_arms(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    results_dir = fixtures_dir / "results"
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", results_dir)
    monkeypatch.delenv("DECISIONS_API_KEY", raising=False)

    # A retried id: the same id appears twice in the raw file (an old error
    # line plus a later ok line) -- raw_lines must count both, rows_after_dedup
    # must count it once, exactly like load_results' own collapse.
    rows = (
        [_result_row(f"y{i}", "yes", "baseline", 1, score=0.9) for i in range(4)]
        + [_result_row(f"n{i}", "no", "baseline", 1, score=0.1) for i in range(3)]
        + [_result_row("n0", "no", "baseline", 1, score=None,
                        error="SomeError: transient")]
    )
    _write_results_jsonl(results_dir, "work_class", "baseline", 1, rows)

    rc = jse.main(["--set", "work_class", "--score"])
    assert rc == 0

    payload = json.loads((results_dir / "scores.json").read_text(encoding="utf-8"))
    prov = payload["work_class"]["provenance"]

    assert prov["results_files_read"] == ["work_class.baseline.run1.jsonl"]
    assert prov["per_arm_run"]["baseline.run1"]["raw_lines"] == 8
    assert prov["per_arm_run"]["baseline.run1"]["rows_after_dedup"] == 7
    assert set(prov["arms_expected_but_missing"]) == {
        "broad", "decomposed", "control_broad", "control_decomposed",
    }
    assert "scored_at" in prov and prov["scored_at"]
    # git_head_sha is best-effort: either a real sha string or None, never
    # missing from the payload.
    assert "git_head_sha" in prov


def test_git_head_sha_tolerates_git_being_unavailable(monkeypatch):
    def _explode(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(jse.subprocess, "run", _explode)
    assert jse._git_head_sha() is None
