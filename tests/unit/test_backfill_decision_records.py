"""Unit tests for scripts/backfill_decision_records.py (Track B Task 7).

Everything here is a fake-`aws`-subprocess test, same pattern as
tests/unit/test_jev_eval_scripts_run_standalone.py's `_FakeCompleted` /
`monkeypatch.setattr(ex.subprocess, "run", _fake_run)`. No AWS credentials, no
network, no real database -- proving the SQL actually behaves correctly against
Postgres is tests/integration/test_backfill_decision_records_sql.py's job.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.backfill_decision_records as bk
from thread_match import MIN_SCORE as THREAD_MIN_SCORE

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "backfill_decision_records.py"


class _FakeCompleted:
    """Mirrors subprocess.run with no text=/encoding= -- stdout/stderr are
    bytes, same convention _aws itself decodes."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
        self.stderr = stderr.encode("utf-8") if isinstance(stderr, str) else stderr


def _long(n):
    return {"longValue": n}


def _stats_response(eligible, skipped, already_present):
    return json.dumps({
        "records": [[_long(eligible), _long(skipped), _long(already_present)]],
    })


def _insert_response(n_rows):
    return json.dumps({"records": [[{"stringValue": f"id-{i}"}] for i in range(n_rows)]})


def _make_fake_run(calls, *, insert_counts=None, fail_on=None):
    """`calls` collects every args list issued, in order. `insert_counts` maps
    a substring found in the --sql value of an INSERT statement to how many
    RETURNING rows to report back. `fail_on` (optional) is a substring of a
    --sql value that raises instead of responding, to exercise the
    exception-rolls-back path."""
    insert_counts = insert_counts or {}

    def _fake_run(args, **kwargs):
        calls.append(args)
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "commit-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "Committed"}))
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        if "execute-statement" in args:
            sql = args[args.index("--sql") + 1]
            if fail_on is not None and fail_on in sql:
                raise RuntimeError(f"boom: {fail_on}")
            if "INSERT INTO decision_records" in sql:
                for marker, n in insert_counts.items():
                    if marker in sql:
                        return _FakeCompleted(stdout=_insert_response(n))
                return _FakeCompleted(stdout=_insert_response(0))
            # a stats query
            if "pps_candidates" in sql:
                return _FakeCompleted(stdout=_stats_response(5, 1, 2))
            if "tts_candidates" in sql:
                return _FakeCompleted(stdout=_stats_response(3, 0, 0))
            if "cf_candidates" in sql:
                return _FakeCompleted(stdout=_stats_response(4, 0, 4))
            raise AssertionError(f"unrecognised sql: {sql[:80]}")
        raise AssertionError(f"unexpected aws call: {args}")

    return _fake_run


# ---------------------------------------------------------------------------
# Runs standalone (no pytest sys.path rigging).
# ---------------------------------------------------------------------------

def test_entry_script_runs_standalone_with_help(tmp_path):
    import os
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


# ---------------------------------------------------------------------------
# Arg parsing.
# ---------------------------------------------------------------------------

def test_default_database_is_test_not_prod():
    assert bk.DEFAULT_DATABASE == "fieldsight_test"
    assert bk.PROD_DATABASE == "fieldsight"


def test_prod_database_requires_allow_prod(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(bk, "run_backfill", lambda **kw: called.append(kw))
    rc = bk.main(["--database", "fieldsight"])
    assert rc == 2
    assert called == []
    assert "refusing" in capsys.readouterr().err


def test_prod_database_allowed_with_allow_prod(monkeypatch):
    called = []
    monkeypatch.setattr(bk, "run_backfill", lambda **kw: called.append(kw) or {})
    rc = bk.main(["--database", "fieldsight", "--allow-prod"])
    assert rc == 0
    assert called and called[0]["database"] == "fieldsight"


def test_default_mode_is_dry_run(monkeypatch):
    captured = {}
    monkeypatch.setattr(bk, "run_backfill", lambda **kw: captured.update(kw) or {})
    bk.main([])
    assert captured["apply"] is False


def test_apply_flag_sets_apply_true(monkeypatch):
    captured = {}
    monkeypatch.setattr(bk, "run_backfill", lambda **kw: captured.update(kw) or {})
    bk.main(["--apply"])
    assert captured["apply"] is True


# ---------------------------------------------------------------------------
# dry-run never commits / --apply commits.
# ---------------------------------------------------------------------------

def test_dry_run_never_commits(monkeypatch):
    calls = []
    monkeypatch.setattr(bk.subprocess, "run", _make_fake_run(calls))

    report = bk.run_backfill(cluster="c", secret="s", database="fieldsight_test",
                             apply=False, profile="p", region="r")

    commands = [c[2] for c in calls]  # args = ["aws", "rds-data", <subcommand>, ...]
    assert "commit-transaction" not in commands
    assert commands.count("rollback-transaction") == 1
    # Only the three stats SELECTs ran -- no INSERT statement at all.
    execute_calls = [c for c in calls if "execute-statement" in c]
    assert len(execute_calls) == 3
    for c in execute_calls:
        sql = c[c.index("--sql") + 1]
        assert "INSERT INTO decision_records" not in sql
    assert report["programme_match"] == {
        "eligible": 5, "skipped_topic_null": 1, "already_present": 2, "would_insert": 3,
    }
    assert report["thread"]["would_insert"] == 3
    assert report["work_class"]["would_insert"] == 0
    assert "inserted" not in report["programme_match"]


def test_apply_commits(monkeypatch):
    calls = []
    fake = _make_fake_run(
        calls,
        insert_counts={"'programme_match'": 3, "'thread'": 3, "'work_class'": 0},
    )
    monkeypatch.setattr(bk.subprocess, "run", fake)

    report = bk.run_backfill(cluster="c", secret="s", database="fieldsight_test",
                             apply=True, profile="p", region="r")

    commands = [c[2] for c in calls]
    assert commands.count("commit-transaction") == 1
    assert "rollback-transaction" not in commands
    execute_calls = [c for c in calls if "execute-statement" in c]
    assert len(execute_calls) == 6  # 3 stats + 3 inserts
    assert report["programme_match"] == {
        "eligible": 5, "skipped_topic_null": 1, "already_present": 2, "inserted": 3,
    }
    assert report["thread"]["inserted"] == 3
    assert report["work_class"]["inserted"] == 0
    assert "would_insert" not in report["programme_match"]


def test_apply_rolls_back_on_mid_run_exception(monkeypatch):
    calls = []
    # Fail the thread source's insert -- the pps stats+insert already
    # succeeded by then, but nothing must be committed.
    fake = _make_fake_run(calls, fail_on="'thread', 'topic'")
    monkeypatch.setattr(bk.subprocess, "run", fake)

    with pytest.raises(RuntimeError, match="boom:"):
        bk.run_backfill(cluster="c", secret="s", database="fieldsight_test",
                        apply=True, profile="p", region="r")

    commands = [c[2] for c in calls]
    assert "commit-transaction" not in commands
    assert commands.count("rollback-transaction") == 1


# ---------------------------------------------------------------------------
# Data API call shapes.
# ---------------------------------------------------------------------------

def test_execute_statement_calls_carry_the_right_resource_and_tx_args(monkeypatch):
    calls = []
    monkeypatch.setattr(bk.subprocess, "run", _make_fake_run(calls))

    bk.run_backfill(cluster="arn:cluster:x", secret="arn:secret:y",
                    database="fieldsight_test", apply=False,
                    profile="my-profile", region="ap-southeast-2")

    execute_calls = [c for c in calls if "execute-statement" in c]
    for c in execute_calls:
        assert c[c.index("--resource-arn") + 1] == "arn:cluster:x"
        assert c[c.index("--secret-arn") + 1] == "arn:secret:y"
        assert c[c.index("--database") + 1] == "fieldsight_test"
        assert c[c.index("--transaction-id") + 1] == "tx-1"
        assert c[c.index("--profile") + 1] == "my-profile"
        assert c[c.index("--region") + 1] == "ap-southeast-2"


def test_execute_statement_sql_values_match_the_pure_sql_functions(monkeypatch):
    calls = []
    monkeypatch.setattr(bk.subprocess, "run", _make_fake_run(
        calls, insert_counts={"'programme_match'": 0, "'thread'": 0, "'work_class'": 0}))

    bk.run_backfill(cluster="c", secret="s", database="fieldsight_test",
                    apply=True, profile="p", region="r")

    sqls = [c[c.index("--sql") + 1] for c in calls if "execute-statement" in c]
    assert sqls == [
        bk.sql_stats_programme_match(), bk.sql_insert_programme_match(),
        bk.sql_stats_thread(), bk.sql_insert_thread(),
        bk.sql_stats_work_class(), bk.sql_insert_work_class(),
    ]


def test_parse_stats_handles_empty_records():
    assert bk._parse_stats({"records": []}) == {
        "eligible": 0, "skipped_topic_null": 0, "already_present": 0,
    }


# ---------------------------------------------------------------------------
# SQL shape -- pure, no I/O.
# ---------------------------------------------------------------------------

ALL_SQL_FNS = (
    bk.sql_stats_programme_match, bk.sql_insert_programme_match,
    bk.sql_stats_thread, bk.sql_insert_thread,
    bk.sql_stats_work_class, bk.sql_insert_work_class,
)


@pytest.mark.parametrize("fn", ALL_SQL_FNS, ids=lambda f: f.__name__)
def test_every_sql_statement_starts_with_with(fn):
    assert fn().lstrip().startswith("WITH ")


@pytest.mark.parametrize("fn", ALL_SQL_FNS, ids=lambda f: f.__name__)
def test_no_named_or_percent_s_parameters(fn):
    sql = fn()
    assert ":" not in sql.split("--")[0].replace("::", "")  # no :name params (casts use ::)
    assert "%s" not in sql


@pytest.mark.parametrize("fn", (bk.sql_insert_programme_match, bk.sql_insert_thread,
                                bk.sql_insert_work_class), ids=lambda f: f.__name__)
def test_insert_statements_target_decision_records_with_returning_id(fn):
    sql = fn()
    assert sql.count("INSERT INTO decision_records") == 1
    assert sql.rstrip().endswith("RETURNING id")
    assert "NOT EXISTS" in sql or "NOT " in sql


@pytest.mark.parametrize("fn", (bk.sql_insert_programme_match, bk.sql_insert_thread,
                                bk.sql_insert_work_class), ids=lambda f: f.__name__)
def test_insert_statements_use_legacy_provider(fn):
    assert "'legacy'" in fn()


def test_idempotency_predicate_uses_is_not_distinct_from():
    assert "IS NOT DISTINCT FROM" in bk._already_present("x", "programme_match")


def test_programme_match_threshold_is_0_70():
    assert "0.7" in bk.sql_insert_programme_match()


def test_thread_threshold_matches_thread_match_min_score():
    assert str(THREAD_MIN_SCORE) in bk.sql_insert_thread()
    assert bk._THREAD_MIN_SCORE == THREAD_MIN_SCORE


def test_work_class_object_ref_is_always_null():
    assert "NULL::text AS object_ref" in bk._CF_CTE


def test_work_class_human_outcome_mapping():
    sql = bk._CF_CTE
    assert "WHEN 'confirm_non_work' THEN 'confirmed'" in sql
    assert "ELSE 'rejected'" in sql


def test_thread_object_ref_falls_back_to_accepted_lookup():
    sql = bk._TTS_CTE
    assert "tts.parent_topic_id::text" in sql
    assert "auto_outcome = 'accepted'" in sql
    assert "ORDER BY dr.created_at DESC LIMIT 1" in sql


def test_thread_actor_join_compares_uuid_as_text_never_casts_resolved_by():
    sql = bk._TTS_CTE
    assert "hu.id::text = tts.resolved_by" in sql
    assert "tts.resolved_by::uuid" not in sql


def test_programme_match_skips_null_topic_id():
    sql = bk.sql_insert_programme_match()
    assert "topic_id IS NOT NULL" in sql
    stats_sql = bk.sql_stats_programme_match()
    assert "skipped_topic_null" in stats_sql
