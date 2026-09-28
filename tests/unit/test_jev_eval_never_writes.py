"""Never-writes proof for the Jev shadow-eval harness (Track A, Task 9).

Global rule for this track: nothing here writes to Aurora or S3, and nothing
runs in a Lambda. The harness only ever reaches AWS through
`subprocess.run(["aws", ...])` -- never through a boto3 client -- so this
file proves the claim the way the code actually reaches AWS:

(a) every `scripts/jev_shadow_eval.py` / `scripts/jev_eval/*` module (plus
    `systemone_client`) imports cleanly even when `boto3.client`/`resource`
    would raise -- none of them is silently relying on boto3 at import time
    (or at all; none of them imports boto3 in the first place).
(b) the runner's normal mode, driven end-to-end with a fake `ask`, a fake
    `llm_utils.call_llm`, and a fake `subprocess.run` that records every
    argv, never issues `put-object`, `s3 cp`, a bare `execute-statement`
    outside a begin/rollback pair, `commit-transaction`, or `invoke`.
(c) `export_labels.run_export`'s aws sequence is always
    begin-transaction -> execute-statement(s) -> rollback-transaction, and
    NEVER commit-transaction, driven with a fake subprocess.run.
(d) no harness module imports `db` / `db.connection` (this repo's real
    Aurora connection helper, `src/db/connection.py`, confirmed by grep) or
    `psycopg` directly.

`test_the_forbidden_argv_check_actually_bites` and
`test_the_commit_transaction_check_actually_bites` prove the assertions in
(b)/(c) are not vacuous: a forbidden call is injected and shown to fail the
check, standing in for "run it, see it fail, then remove it" during
development (both are asserted with `pytest.raises` so they stay in CI as a
permanent proof the check bites, not a one-off manual step).
"""
from __future__ import annotations

import importlib
import json
import sys

import pytest

import lambda_programme_matcher
import llm_utils
import scripts.jev_shadow_eval as jse
import systemone_client as sc

def _forbidden_reason(argv: list):
    """None if `argv` (an `aws ...` call) is allowed under this track's
    global rule; otherwise a short reason string. `execute-statement` is only
    forbidden BARE -- i.e. not inside a `--transaction-id`-carrying call, the
    shape `export_labels.py` always uses -- matching the brief's "outside a
    begin/rollback pair" wording without needing to track a running
    transaction-id across calls (a `--transaction-id` on an execute is only
    ever valid because some earlier call in the same run began one)."""
    if "put-object" in argv:
        return "put-object"
    if "s3" in argv and "cp" in argv:
        return "s3 cp"
    if "execute-statement" in argv and "--transaction-id" not in argv:
        return "execute-statement outside a transaction"
    if "commit-transaction" in argv:
        return "commit-transaction"
    if "invoke" in argv:
        return "invoke"
    return None


# ---------------------------------------------------------------------------
# (a) import every harness module with boto3.client/resource set to explode.
# ---------------------------------------------------------------------------

HARNESS_MODULES = (
    "scripts.jev_shadow_eval",
    "scripts.jev_eval.export_labels",
    "scripts.jev_eval.baseline",
    "scripts.jev_eval.questions",
    "scripts.jev_eval.score",
    "scripts.jev_eval.state",
    "systemone_client",
)


def test_harness_modules_import_without_touching_boto3(monkeypatch):
    boto3 = pytest.importorskip("boto3")

    def _boom(*a, **k):
        raise AssertionError(
            "boto3.client/resource must never be called while importing the "
            "Jev shadow-eval harness")

    monkeypatch.setattr(boto3, "client", _boom)
    monkeypatch.setattr(boto3, "resource", _boom)

    originals = {name: sys.modules.get(name) for name in HARNESS_MODULES}
    try:
        for name in HARNESS_MODULES:
            sys.modules.pop(name, None)
        for name in HARNESS_MODULES:
            importlib.import_module(name)
    finally:
        # Restore the module identities other already-imported test modules
        # hold references to (e.g. `import scripts.jev_shadow_eval as jse`),
        # so this test cannot leave the rest of the session on a different
        # module object than the one it started with.
        for name, mod in originals.items():
            if mod is not None:
                sys.modules[name] = mod
            else:
                sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# (d) no harness module imports the real DB connection path.
# ---------------------------------------------------------------------------

def test_no_harness_module_imports_db_connection_or_psycopg():
    for name in HARNESS_MODULES:
        mod = sys.modules.get(name) or importlib.import_module(name)
        source_path = getattr(mod, "__file__", None)
        assert source_path, f"{name} has no __file__ to scan"
        text = open(source_path, encoding="utf-8").read()
        for banned in ("import psycopg", "import db.connection",
                        "from db.connection", "from db import connection",
                        "import db\n", "from db import"):
            assert banned not in text, f"{name} references {banned!r}"


# ---------------------------------------------------------------------------
# (b) the runner's normal mode -- fake ask/call, fake subprocess.run,
# recording every aws argv.
# ---------------------------------------------------------------------------

def _programme_match_row():
    return {
        "id": "pm-1",
        "label": "yes",
        "features": {
            "observation": {
                "title": "Ground floor walls",
                "summary": "Sub A finished the ground floor walls today.",
                "date": "2026-09-20",
            },
            "task": {
                "name": "Ground floor walls",
                "status": "in_progress",
                "progress_pct": 40,
            },
        },
        "baseline": {
            "task_id": "task-1",
            "confidence": None,
            "suggested_status": None,
            "suggested_progress": None,
        },
        "site_id": "site-1",
        "company_id": "co-1",
        "decided_at": "2026-09-20T00:00:00Z",
    }


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_call_ok(prompt, max_tokens=512, force_json=True, caller=None):
    return json.dumps({"task_id": "task-1", "confidence": 0.9}), None


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
    return {"answers": answers, "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_ms": 5}


def _setup_normal_run(tmp_path, monkeypatch):
    fixtures_dir = tmp_path / "jev_eval"
    fixtures_dir.mkdir(parents=True)
    results_dir = fixtures_dir / "results"
    monkeypatch.setattr(jse, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(jse, "RESULTS_DIR", results_dir)

    rows = [_programme_match_row()]
    with open(fixtures_dir / "programme_match.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    (fixtures_dir / "name_aliases.json").write_text("[]", encoding="utf-8")
    (fixtures_dir / "counts.json").write_text(
        json.dumps({"programme_match": {"n": len(rows)}}), encoding="utf-8")

    monkeypatch.setenv("DECISIONS_API_KEY", "test-key")
    monkeypatch.setattr(sc, "ask", _fake_ask_ok)
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_ok)

    return fixtures_dir, results_dir


def _install_recording_aws(monkeypatch):
    """Replaces `baseline_mod.load_deployed_llm_env` with a wrapper that
    forwards to the real function but with a fake, argv-recording
    `subprocess.run` injected as its `run=` seam -- the default
    `run=subprocess.run` parameter is bound at function-definition time, so
    monkeypatching the `subprocess` module's `run` attribute afterwards would
    NOT reach it; the seam has to be supplied explicitly, exactly as every
    other test in this repo already does for this function."""
    argvs = []

    def _fake_run(args, **kwargs):
        argvs.append(args)
        return _FakeCompleted(stdout=json.dumps({
            "Environment": {"Variables": {"LLM_PROVIDER": "anthropic"}}
        }))

    real_load_env = jse.baseline_mod.load_deployed_llm_env

    def _wrapped(function_name, *, profile=None, region=None):
        return real_load_env(function_name, profile=profile, region=region, run=_fake_run)

    monkeypatch.setattr(jse.baseline_mod, "load_deployed_llm_env", _wrapped)
    return argvs


def test_runner_normal_mode_never_issues_forbidden_aws_argv(tmp_path, monkeypatch):
    _setup_normal_run(tmp_path, monkeypatch)
    argvs = _install_recording_aws(monkeypatch)

    rc = jse.main(["--set", "programme_match", "--arms", "baseline,broad",
                   "--runs", "1"])
    assert rc == 0
    assert argvs, "expected the baseline arm to have called load_deployed_llm_env"

    for argv in argvs:
        reason = _forbidden_reason(argv)
        assert reason is None, f"forbidden aws call ({reason}): {argv}"


def test_the_forbidden_argv_check_actually_bites(tmp_path, monkeypatch):
    """Same setup as above, but the fake aws transport is made to also issue
    an `s3 cp` call -- proves the assertion above is not vacuous."""
    _setup_normal_run(tmp_path, monkeypatch)
    argvs = []

    def _fake_run_with_forbidden_call(args, **kwargs):
        argvs.append(args)
        # A forbidden call injected on purpose, to prove the check below
        # actually fails when one occurs.
        argvs.append(["aws", "s3", "cp", "local.json", "s3://bucket/key"])
        return _FakeCompleted(stdout=json.dumps({
            "Environment": {"Variables": {"LLM_PROVIDER": "anthropic"}}
        }))

    real_load_env = jse.baseline_mod.load_deployed_llm_env

    def _wrapped(function_name, *, profile=None, region=None):
        return real_load_env(function_name, profile=profile, region=region,
                              run=_fake_run_with_forbidden_call)

    monkeypatch.setattr(jse.baseline_mod, "load_deployed_llm_env", _wrapped)

    rc = jse.main(["--set", "programme_match", "--arms", "baseline,broad",
                   "--runs", "1"])
    assert rc == 0

    with pytest.raises(AssertionError):
        for argv in argvs:
            reason = _forbidden_reason(argv)
            assert reason is None, f"forbidden aws call ({reason}): {argv}"


# ---------------------------------------------------------------------------
# (c) export_labels: always begin -> execute(s) -> rollback, never commit.
# ---------------------------------------------------------------------------

def _fake_export_transport(calls):
    empty_result = json.dumps({"records": []})

    def _fake_run(args, **kwargs):
        calls.append(args)
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "execute-statement" in args:
            return _FakeCompleted(stdout=empty_result)
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        raise AssertionError(f"unexpected aws call: {args}")

    return _fake_run


def test_export_labels_sequence_is_begin_execute_rollback_never_commit(monkeypatch, tmp_path):
    from scripts.jev_eval import export_labels as ex

    calls = []
    monkeypatch.setattr(ex.subprocess, "run", _fake_export_transport(calls))

    counts = ex.run_export("fieldsight_test", out_dir=tmp_path)
    assert counts["route_note"] == ex.ROUTE_NOTE

    verbs = []
    for argv in calls:
        assert "commit-transaction" not in argv, f"export_labels must never commit: {argv}"
        if "begin-transaction" in argv:
            verbs.append("begin")
        elif "execute-statement" in argv:
            verbs.append("execute")
        elif "rollback-transaction" in argv:
            verbs.append("rollback")
        else:
            raise AssertionError(f"unexpected aws call: {argv}")

    assert verbs[0] == "begin"
    assert verbs[-1] == "rollback"
    assert all(v in ("begin", "execute", "rollback") for v in verbs)
    assert "execute" in verbs


def test_the_commit_transaction_check_actually_bites(monkeypatch, tmp_path):
    """Same export, but the fake transport is made to issue a stray
    commit-transaction after the rollback -- proves the guard above is not
    vacuous."""
    from scripts.jev_eval import export_labels as ex

    calls = []
    real_fake_run = _fake_export_transport(calls)

    def _fake_run_with_commit(args, **kwargs):
        result = real_fake_run(args, **kwargs)
        if "rollback-transaction" in args:
            calls.append(["aws", "rds-data", "commit-transaction",
                           "--transaction-id", "tx-1"])
        return result

    monkeypatch.setattr(ex.subprocess, "run", _fake_run_with_commit)
    ex.run_export("fieldsight_test", out_dir=tmp_path)

    with pytest.raises(AssertionError):
        for argv in calls:
            assert "commit-transaction" not in argv, f"must never commit: {argv}"
