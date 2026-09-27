"""Shape tests for the Jev shadow-eval label exporter (Track A, Task 1).

`scripts/jev_eval/export_labels.py` is split into pure SQL/mapper functions
and an I/O runner (RDS Data API via `aws` subprocess calls, always inside a
begin/execute/rollback transaction). These tests exercise the pure half on
fake Data API-shaped records and prove the runner's safety properties
(prod gate before any `aws` call, rollback even on failure) without ever
touching a real database -- per the task brief, this suite cannot run the
SQL, only prove the shapes.
"""
import json

import pytest

from scripts.jev_eval import export_labels as ex

_BANNED_KEYS = {"transcript", "turns", "quote", "evidence", "text_window"}


def _walk_keys(value):
    """Yield every dict key anywhere in `value`, at any depth."""
    if isinstance(value, dict):
        for key, sub in value.items():
            yield key
            yield from _walk_keys(sub)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


# ---------------------------------------------------------------------------
# SQL shape
# ---------------------------------------------------------------------------

def test_every_sql_string_starts_with_select_or_with():
    sqls = [
        ex.sql_programme_match(),
        ex.sql_threads(),
        ex.sql_work_class(),
        ex.sql_name_aliases(["11111111-1111-1111-1111-111111111111"]),
        ex.sql_users(["11111111-1111-1111-1111-111111111111"]),
        ex.sql_companies(["11111111-1111-1111-1111-111111111111"]),
    ]
    for sql in sqls:
        stripped = sql.strip().upper()
        assert stripped.startswith("SELECT") or stripped.startswith("WITH"), sql


# ---------------------------------------------------------------------------
# programme_match mapper
# ---------------------------------------------------------------------------

def _pm_rec(**overrides):
    rec = {
        "id": "pm-1", "state": "confirmed", "decided_at": "2026-09-01T00:00:00Z",
        "report_date": "2026-08-30", "topic_title": "Slab pour delayed",
        "topic_summary": "Pour pushed to Thursday.", "task_name": "Pour ground slab",
        "task_status_before": "in_progress", "task_progress_before": 40,
        "suggested_status": "delayed", "suggested_progress": 40,
        "confidence": 0.81, "task_id": "T-9", "site_id": "site-1",
        "company_id": "company-1",
    }
    rec.update(overrides)
    return rec


def test_programme_match_row_has_exact_keys_and_shape():
    row = ex.map_programme_match_row(_pm_rec())
    assert set(row) == {"set", "id", "label", "features", "site_id",
                        "company_id", "decided_at", "baseline"}
    assert row["set"] == "programme_match"
    assert row["label"] == "yes"
    assert set(row["features"]) == {"observation", "task"}
    assert set(row["features"]["observation"]) == {"title", "summary", "date"}
    assert set(row["features"]["task"]) == {"name", "status", "progress_pct"}
    assert set(row["baseline"]) == {"confidence", "suggested_status",
                                    "suggested_progress", "task_id"}


def test_programme_match_label_mapping():
    assert ex.map_programme_match_row(_pm_rec(state="confirmed"))["label"] == "yes"
    assert ex.map_programme_match_row(_pm_rec(state="rejected"))["label"] == "no"
    assert "_excluded" in ex.map_programme_match_row(_pm_rec(state="pending"))
    assert "_excluded" in ex.map_programme_match_row(_pm_rec(state="stale"))


def test_programme_match_never_carries_match_evidence():
    row = ex.map_programme_match_row(_pm_rec())
    keys = set(_walk_keys(row))
    assert "match_evidence" not in keys
    assert not (keys & _BANNED_KEYS)


# ---------------------------------------------------------------------------
# threads mapper -- both the parent_topic_id and thread_id branches
# ---------------------------------------------------------------------------

def _thread_rec(**overrides):
    rec = {
        "id": "th-1", "status": "confirmed", "score": 0.46, "gap_days": 5,
        "resolved_at": "2026-09-01T00:00:00Z",
        "later_title": "Door hardware install", "later_summary": "Handles fitted.",
        "later_date": "2026-09-01", "site_id": "site-1", "company_id": "company-1",
        "sugg_thread_id": None, "sugg_parent_topic_id": "parent-topic-1",
        "parent_title": "Door hardware ordered", "parent_summary": "Order placed.",
        "parent_date": "2026-08-20",
        "thread_title": None, "thread_summary": None, "thread_date": None,
    }
    rec.update(overrides)
    return rec


def test_threads_row_has_exact_keys_and_shape():
    row = ex.map_threads_row(_thread_rec())
    assert set(row) == {"set", "id", "label", "features", "site_id",
                        "company_id", "decided_at", "baseline"}
    assert set(row["features"]) == {"earlier", "later", "gap_days"}
    assert set(row["features"]["earlier"]) == {"title", "summary", "date"}
    assert set(row["features"]["later"]) == {"title", "summary", "date"}
    assert set(row["baseline"]) == {"score"}


def test_threads_parent_topic_id_branch_uses_parent_as_earlier():
    row = ex.map_threads_row(_thread_rec())
    assert row["features"]["earlier"]["title"] == "Door hardware ordered"


def test_threads_thread_id_branch_uses_earliest_thread_topic_as_earlier():
    row = ex.map_threads_row(_thread_rec(
        sugg_parent_topic_id=None, sugg_thread_id="thread-1",
        thread_title="Door hardware first mention",
        thread_summary="Raised on day one.", thread_date="2026-08-01",
    ))
    assert row["features"]["earlier"]["title"] == "Door hardware first mention"


def test_threads_label_mapping():
    assert ex.map_threads_row(_thread_rec(status="confirmed"))["label"] == "yes"
    assert ex.map_threads_row(_thread_rec(status="rejected"))["label"] == "no"
    assert "_excluded" in ex.map_threads_row(_thread_rec(status="pending"))


def test_threads_excludes_orphaned_parent():
    rec = _thread_rec(parent_title=None)
    result = ex.map_threads_row(rec)
    assert result == {"_excluded": "orphaned_parent"}


def test_threads_excludes_orphaned_thread():
    rec = _thread_rec(sugg_parent_topic_id=None, sugg_thread_id="thread-1",
                      thread_title=None)
    result = ex.map_threads_row(rec)
    assert result == {"_excluded": "orphaned_thread"}


def test_threads_excludes_malformed_target_when_neither_side_set():
    rec = _thread_rec(sugg_parent_topic_id=None, sugg_thread_id=None)
    result = ex.map_threads_row(rec)
    assert result == {"_excluded": "malformed_target"}


# ---------------------------------------------------------------------------
# work_class mapper
# ---------------------------------------------------------------------------

def _wc_rec(**overrides):
    rec = {
        "id": "wc-1", "human_verdict": "confirm_non_work", "category": "personal",
        "classifier_verdict": "work", "classifier_confidence": 0.6,
        "created_at": "2026-09-01T00:00:00Z", "topic_id": "topic-1",
        "title": "Chat about weekend plans", "summary": "Not work related.",
        "site_id": "site-1", "company_id": "company-1",
    }
    rec.update(overrides)
    return rec


def test_work_class_row_has_exact_keys_and_shape():
    row = ex.map_work_class_row(_wc_rec())
    assert set(row) == {"set", "id", "label", "features", "site_id",
                        "company_id", "decided_at", "baseline"}
    assert set(row["features"]) == {"title", "summary", "category"}
    assert set(row["baseline"]) == {"classifier_verdict", "classifier_confidence"}


def test_work_class_label_mapping():
    assert ex.map_work_class_row(_wc_rec(human_verdict="confirm_non_work"))["label"] == "yes"
    assert ex.map_work_class_row(_wc_rec(human_verdict="missed_personal"))["label"] == "yes"
    assert ex.map_work_class_row(_wc_rec(human_verdict="reject_is_work"))["label"] == "no"


def test_work_class_excludes_missing_topic():
    result = ex.map_work_class_row(_wc_rec(topic_id=None))
    assert result == {"_excluded": "topic_missing"}


# ---------------------------------------------------------------------------
# Cross-cutting: label is only ever yes/no, no banned keys anywhere
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row", [
    ex.map_programme_match_row(_pm_rec(state="confirmed")),
    ex.map_programme_match_row(_pm_rec(state="rejected")),
    ex.map_threads_row(_thread_rec(status="confirmed")),
    ex.map_threads_row(_thread_rec(status="rejected")),
    ex.map_work_class_row(_wc_rec(human_verdict="confirm_non_work")),
    ex.map_work_class_row(_wc_rec(human_verdict="reject_is_work")),
])
def test_label_is_only_ever_yes_or_no(row):
    assert row["label"] in ("yes", "no")


@pytest.mark.parametrize("row", [
    ex.map_programme_match_row(_pm_rec()),
    ex.map_threads_row(_thread_rec()),
    ex.map_threads_row(_thread_rec(
        sugg_parent_topic_id=None, sugg_thread_id="thread-1",
        thread_title="T", thread_summary="S", thread_date="2026-08-01")),
    ex.map_work_class_row(_wc_rec()),
])
def test_no_banned_keys_anywhere_in_row(row):
    keys = set(_walk_keys(row))
    assert not (keys & _BANNED_KEYS)


# ---------------------------------------------------------------------------
# Data API decoding
# ---------------------------------------------------------------------------

def test_decode_field_handles_typed_values_and_null():
    assert ex.decode_field({"stringValue": "abc"}) == "abc"
    assert ex.decode_field({"longValue": 5}) == 5
    assert ex.decode_field({"doubleValue": 0.5}) == 0.5
    assert ex.decode_field({"booleanValue": True}) is True
    assert ex.decode_field({"isNull": True}) is None


def test_record_to_dict_zips_columns_positionally():
    columns = ("id", "state")
    record = [{"stringValue": "pm-1"}, {"stringValue": "confirmed"}]
    assert ex.record_to_dict(columns, record) == {"id": "pm-1", "state": "confirmed"}


# ---------------------------------------------------------------------------
# Alias fixture builder
# ---------------------------------------------------------------------------

def test_build_alias_rows_includes_active_aliases_as_is():
    rows = ex.build_alias_rows(
        [{"wrong_term": "Ben Lynn", "right_term": "Ben Lin", "kind": "person"}], [], [])
    assert rows == [{"wrong_term": "Ben Lynn", "right_term": "Ben Lin", "kind": "person"}]


def test_build_alias_rows_maps_full_name_to_first_name():
    rows = ex.build_alias_rows([], [{"first_name": "Heidi", "last_name": "Ansell"}], [])
    assert rows == [{"wrong_term": "Heidi Ansell", "right_term": "Heidi", "kind": "person"}]


def test_build_alias_rows_skips_users_with_no_first_name():
    rows = ex.build_alias_rows([], [{"first_name": None, "last_name": "Ansell"}], [])
    assert rows == []
    rows = ex.build_alias_rows([], [{"first_name": "  ", "last_name": "Ansell"}], [])
    assert rows == []


def test_build_alias_rows_single_token_user_maps_to_self():
    rows = ex.build_alias_rows([], [{"first_name": "Heidi", "last_name": None}], [])
    assert rows == [{"wrong_term": "Heidi", "right_term": "Heidi", "kind": "person"}]


def test_build_alias_rows_protects_company_names():
    rows = ex.build_alias_rows([], [], [{"name": "Naylor Love"}])
    assert rows == [{"wrong_term": "Naylor Love", "right_term": "Naylor Love", "kind": "company"}]


def test_build_alias_rows_drops_terms_that_cannot_anchor():
    # Leading punctuation would not anchor a \b regex.
    rows = ex.build_alias_rows(
        [{"wrong_term": "-Ben", "right_term": "Ben", "kind": "person"}], [], [])
    assert rows == []


def test_build_alias_rows_strips_whitespace():
    rows = ex.build_alias_rows([], [{"first_name": "  Heidi  ", "last_name": "  "}], [])
    assert rows == [{"wrong_term": "Heidi", "right_term": "Heidi", "kind": "person"}]


def test_build_alias_rows_dedupes():
    rows = ex.build_alias_rows(
        [{"wrong_term": "Ben Lynn", "right_term": "Ben Lin", "kind": "person"}] * 3, [], [])
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# counts.json shape
# ---------------------------------------------------------------------------

def test_summarize_set_marks_small_n_descriptive_only():
    rows = [{"label": "yes"}] * 10 + [{"label": "no"}] * 5
    counts = ex.summarize_set("threads", rows, {}, "fieldsight_test")
    assert counts["n"] == 15
    assert counts["positives"] == 10
    assert counts["negatives"] == 5
    assert counts["descriptive_only"] is True
    assert counts["database"] == "fieldsight_test"
    assert "exported_at" in counts


def test_summarize_set_marks_thirty_plus_as_not_descriptive_only():
    rows = [{"label": "yes"}] * 15 + [{"label": "no"}] * 15
    counts = ex.summarize_set("programme_match", rows, {}, "fieldsight_test")
    assert counts["descriptive_only"] is False


# ---------------------------------------------------------------------------
# prod gate -- must refuse before any `aws` call
# ---------------------------------------------------------------------------

def test_prod_database_without_allow_prod_exits_nonzero_before_any_aws_call(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(ex.subprocess, "run", _boom)

    exit_code = ex.main(["--database", "fieldsight"])
    assert exit_code != 0


def test_prod_database_with_allow_prod_does_not_exit_early(monkeypatch, tmp_path):
    """Sanity check that the gate is specific to the missing flag, not to the
    word 'fieldsight' -- with --allow-prod, execution proceeds far enough to
    actually invoke `aws` (which we then fail fast, to prove it got there)."""
    calls = []

    def _boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("stop here: proves the gate did not block this call")

    monkeypatch.setattr(ex.subprocess, "run", _boom)

    with pytest.raises(AssertionError):
        ex.main(["--database", "fieldsight", "--allow-prod"])
    assert calls  # aws WAS invoked this time


def test_test_database_default_does_not_require_allow_prod(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("stop here: proves the gate did not block this call")

    monkeypatch.setattr(ex.subprocess, "run", _boom)

    with pytest.raises(RuntimeError):
        ex.main([])


# ---------------------------------------------------------------------------
# runner always rolls back, even when execute-statement raises
# ---------------------------------------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_export_rolls_back_even_when_execute_raises(monkeypatch, tmp_path):
    calls = []

    def _fake_run(args, capture_output=True, text=True):
        calls.append(args)
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "execute-statement" in args:
            return _FakeCompleted(returncode=1, stderr="boom: syntax error")
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        raise AssertionError(f"unexpected aws call: {args}")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError):
        ex.run_export("fieldsight_test", out_dir=tmp_path)

    rollback_calls = [c for c in calls if "rollback-transaction" in c]
    assert len(rollback_calls) == 1, "rollback must be called exactly once even on failure"


def test_run_export_writes_fixtures_and_rolls_back_on_success(monkeypatch, tmp_path):
    calls = []

    empty_result = json.dumps({"records": []})

    def _fake_run(args, capture_output=True, text=True):
        calls.append(args)
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "execute-statement" in args:
            return _FakeCompleted(stdout=empty_result)
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        raise AssertionError(f"unexpected aws call: {args}")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    counts = ex.run_export("fieldsight_test", out_dir=tmp_path)

    assert (tmp_path / "programme_match.jsonl").exists()
    assert (tmp_path / "threads.jsonl").exists()
    assert (tmp_path / "work_class.jsonl").exists()
    assert (tmp_path / "counts.json").exists()
    assert (tmp_path / "name_aliases.json").exists()
    assert counts["route_note"] == ex.ROUTE_NOTE
    for set_name in ("programme_match", "threads", "work_class"):
        assert counts[set_name]["n"] == 0
        assert counts[set_name]["descriptive_only"] is True

    rollback_calls = [c for c in calls if "rollback-transaction" in c]
    assert len(rollback_calls) == 1
