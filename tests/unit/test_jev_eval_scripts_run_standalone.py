"""Proves the fix for the owner's first real (non-pytest) runs of the Jev
shadow-eval scripts (Track A follow-up).

Two failures never showed up in the rest of this suite because pytest puts
`src` and the repo root on `sys.path` itself (`pyproject.toml`'s
`pythonpath = ["src", "."]`) -- a real terminal invocation
(`python scripts/jev_eval/export_labels.py ...`) gets neither:

(a) `ModuleNotFoundError: No module named 'deleted_predicates'` (and
    equivalents for the other four entry scripts) -- fixed by a `sys.path`
    bootstrap at the top of each script, before any repo import.
(b) `'gbk' codec can't encode/decode character ...'` on a Chinese-locale
    Windows box, when an `aws` subprocess response carries a non-ASCII byte
    (BUG-35, e.g. a programme task name) -- fixed by decoding `_aws`'s (and
    `baseline.load_deployed_llm_env`'s) subprocess call as UTF-8 explicitly,
    with three UTF-8-forcing env vars added on top of the inherited
    environment.

(a) and (b) are proven directly. This file also proves the export's
mid-failure atomicity fix: a failure partway through the read transaction
must leave whatever was already on disk untouched, not a mix of fresh and
stale output files.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.jev_eval import export_labels as ex

REPO_ROOT = Path(__file__).resolve().parents[2]

ENTRY_SCRIPTS = (
    REPO_ROOT / "scripts" / "jev_eval" / "export_labels.py",
    REPO_ROOT / "scripts" / "jev_eval" / "sample_batch.py",
    REPO_ROOT / "scripts" / "jev_eval" / "label_page.py",
    REPO_ROOT / "scripts" / "jev_eval" / "import_labels.py",
    REPO_ROOT / "scripts" / "jev_shadow_eval.py",
)


# ---------------------------------------------------------------------------
# (a) each entry script must run standalone, with no PYTHONPATH set and no
# help from pytest's own sys.path rigging. This is the check that fails
# against the pre-fix code with `ModuleNotFoundError: No module named
# 'deleted_predicates'` (confirmed by hand: `git show
# <pre-fix-sha>:scripts/jev_eval/export_labels.py`, run the same way, exits
# 1 with that traceback -- see the commit/PR description for the transcript).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", ENTRY_SCRIPTS, ids=lambda p: p.name)
def test_entry_script_runs_standalone_with_help(script, tmp_path):
    assert script.exists(), script

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"{script.name} --help failed outside pytest's own sys.path "
        f"rigging.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "ModuleNotFoundError" not in result.stderr


# ---------------------------------------------------------------------------
# (b) _aws decodes as UTF-8 and carries the three UTF-8-forcing env vars,
# without dropping anything already in os.environ.
# ---------------------------------------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_aws_passes_utf8_encoding_and_forcing_env_vars(monkeypatch):
    monkeypatch.setenv("SOME_PRE_EXISTING_VAR", "keep-me")
    captured = {}

    def _fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeCompleted(stdout=json.dumps({"ok": True}))

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    ex._aws(["sts", "get-caller-identity"])

    kwargs = captured["kwargs"]
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("errors") == "replace"

    env = kwargs.get("env")
    assert env is not None
    assert env.get("PYTHONUTF8") == "1"
    assert env.get("PYTHONIOENCODING") == "utf-8"
    assert env.get("AWS_CLI_FILE_ENCODING") == "UTF-8"
    # never removes anything already in os.environ
    assert env.get("SOME_PRE_EXISTING_VAR") == "keep-me"


def test_aws_decodes_non_ascii_response_correctly(monkeypatch):
    """A fake aws response carrying `ç` and Chinese characters -- the exact
    shape of BUG-35 (a programme task name in a Data API response) -- must
    decode intact, not raise `UnicodeDecodeError` and not raise
    `UnicodeEncodeError` either (both directions of BUG-35's failure)."""
    payload = {"records": [{"stringValue": "façade 验收 扫描"}]}

    def _fake_run(args, **kwargs):
        assert kwargs.get("encoding") == "utf-8"
        return _FakeCompleted(stdout=json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    result = ex._aws(["rds-data", "execute-statement"])

    assert result["records"][0]["stringValue"] == "façade 验收 扫描"


def test_aws_raises_runtime_error_with_stderr_on_failure(monkeypatch):
    def _fake_run(args, **kwargs):
        return _FakeCompleted(returncode=1, stderr="boom: syntax error")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError, match="boom: syntax error"):
        ex._aws(["rds-data", "execute-statement"])


# ---------------------------------------------------------------------------
# Atomicity: a failure partway through the read transaction must leave
# whatever was already on disk (fixtures from a PREVIOUS successful export)
# byte-identical, and must write nothing new.
# ---------------------------------------------------------------------------

def _pm_record_with_company_id(company_id="c1"):
    # Positional, matching ex.PROGRAMME_MATCH_COLUMNS order exactly.
    values = [
        "pm-1", "confirmed", "2026-09-01T00:00:00Z", "2026-09-01",
        "topic title", "topic summary", "task name", "in_progress", "10",
        "in_progress", "20", "0.9", "task-1", "s1", company_id,
    ]
    return [{"stringValue": v} for v in values]


def test_run_export_failure_on_last_statement_leaves_existing_fixtures_untouched(
    monkeypatch, tmp_path
):
    existing = {
        "programme_match.jsonl": json.dumps(
            {
                "set": "programme_match", "id": "old-pm", "label": "yes",
                "features": {}, "site_id": "s1", "company_id": "c1",
                "decided_at": "2026-08-01T00:00:00Z", "baseline": {},
                "label_source": "db",
            },
            sort_keys=True,
        ) + "\n",
        "threads.jsonl": "",
        "work_class.jsonl": "",
        "counts.json": json.dumps(
            {"programme_match": {"n": 1}, "route_note": ex.ROUTE_NOTE},
            indent=2, sort_keys=True,
        ) + "\n",
        "name_aliases.json": json.dumps(
            [{"wrong_term": "Old Co", "right_term": "[COMPANY]", "kind": "company"}],
            indent=2, sort_keys=True,
        ) + "\n",
    }
    for name, content in existing.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    before = {name: (tmp_path / name).read_bytes() for name in existing}
    # Nothing else must appear in out_dir after the failed export either
    # (no stray .tmp files left behind by the atomic write helpers).
    before_listing = sorted(p.name for p in tmp_path.iterdir())

    empty_result = json.dumps({"records": []})

    def _fake_run(args, **kwargs):
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        if "execute-statement" in args:
            sql = args[args.index("--sql") + 1]
            if "FROM programme_progress_suggestions" in sql:
                return _FakeCompleted(
                    stdout=json.dumps({"records": [_pm_record_with_company_id()]}))
            if "FROM programme_tasks pt" in sql:
                # The LAST read statement `run_export` issues (task names,
                # gated on company_ids -- reached only because the
                # programme_match row above carries one). Fails here to
                # prove nothing was written before this point either.
                raise RuntimeError("boom: last statement failed")
            return _FakeCompleted(stdout=empty_result)
        raise AssertionError(f"unexpected aws call: {args}")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError, match="boom: last statement failed"):
        ex.run_export("fieldsight_test", out_dir=tmp_path)

    for name, content in before.items():
        assert (tmp_path / name).read_bytes() == content, (
            f"{name} changed even though the export failed before any write"
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == before_listing, (
        "no new file (including a stray .tmp) may appear on a failed export"
    )
