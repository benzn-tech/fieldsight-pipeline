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
        ex.sql_sites(["11111111-1111-1111-1111-111111111111"]),
        ex.sql_programme_task_names(["11111111-1111-1111-1111-111111111111"]),
    ]
    for sql in sqls:
        stripped = sql.strip().upper()
        assert stripped.startswith("SELECT") or stripped.startswith("WITH"), sql


def test_sql_sites_filters_by_company_and_excludes_archived():
    sql = ex.sql_sites(["co-1"])
    assert "company_id IN ('co-1')" in sql
    assert "archived_at IS NULL" in sql


def test_sql_programme_task_names_filters_by_company_and_excludes_removed():
    sql = ex.sql_programme_task_names(["co-1"])
    assert "s.company_id IN ('co-1')" in sql
    assert "removed_in_version IS NULL" in sql


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


def test_build_alias_rows_user_with_last_name_groups_first_last_and_full():
    # Fix wave 2 I4: the old behaviour emitted ONLY "Heidi Ansell" -> "Heidi",
    # which left the bare surname "Ansell" (and the full name itself)
    # unmasked. Now all three collapse to one placeholder via alias_group.
    rows = ex.build_alias_rows([], [{"first_name": "Heidi", "last_name": "Ansell"}], [])
    assert len(rows) == 3
    groups = {row["alias_group"] for row in rows}
    assert len(groups) == 1
    by_wrong = {row["wrong_term"]: row for row in rows}
    assert set(by_wrong) == {"Heidi", "Ansell", "Heidi Ansell"}
    assert by_wrong["Heidi"]["right_term"] == "Heidi"
    assert by_wrong["Ansell"]["right_term"] == "Ansell"
    assert by_wrong["Heidi Ansell"]["right_term"] == "Heidi"
    for row in rows:
        assert row["kind"] == "person"


def test_build_alias_rows_two_users_get_separate_alias_groups():
    rows = ex.build_alias_rows(
        [], [{"first_name": "Heidi", "last_name": "Ansell"},
             {"first_name": "Ben", "last_name": "Lin"}], [])
    groups = {row["alias_group"] for row in rows}
    assert len(groups) == 2


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


def test_build_alias_rows_protects_site_names():
    rows = ex.build_alias_rows([], [], [], [{"name": "SB1108 Ellesmere College"}], [])
    assert rows == [{"wrong_term": "SB1108 Ellesmere College",
                     "right_term": "SB1108 Ellesmere College", "kind": "company"}]


def test_build_alias_rows_protects_programme_task_names():
    rows = ex.build_alias_rows([], [], [], [], [{"name": "Roof Framing"}])
    assert rows == [{"wrong_term": "Roof Framing", "right_term": "Roof Framing", "kind": "company"}]


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


def test_summarize_set_marks_twenty_per_class_as_not_descriptive_only():
    # Fix wave 4, A6: descriptive_only is now the >= 20-per-EACH-class
    # eligibility gate (score.ELIGIBILITY_MIN_PER_CLASS), not a bare n < 30.
    rows = [{"label": "yes"}] * 20 + [{"label": "no"}] * 20
    counts = ex.summarize_set("programme_match", rows, {}, "fieldsight_test")
    assert counts["descriptive_only"] is False


def test_summarize_set_thirty_total_but_one_class_thin_is_still_descriptive_only():
    # 29 yes / 1 no: n=30 (the OLD threshold) but nowhere near eligible under
    # the amended rule -- proves this is no longer a bare n < 30 check.
    rows = [{"label": "yes"}] * 29 + [{"label": "no"}] * 1
    counts = ex.summarize_set("threads", rows, {}, "fieldsight_test")
    assert counts["descriptive_only"] is True


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
    """Mirrors subprocess.run with no text=/encoding= -- stdout/stderr are
    bytes. A str convenience arg is auto-encoded UTF-8."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
        self.stderr = stderr.encode("utf-8") if isinstance(stderr, str) else stderr


def test_run_export_rolls_back_even_when_execute_raises(monkeypatch, tmp_path):
    calls = []

    def _fake_run(args, **kwargs):
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

    def _fake_run(args, **kwargs):
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


# ---------------------------------------------------------------------------
# I5: re-export must not wipe owner-labelled rows
# ---------------------------------------------------------------------------

def test_merge_export_rows_keeps_owner_rows_on_id_collision():
    existing = [
        {"id": "a", "label": "yes", "label_source": "owner"},
        {"id": "b", "label": "no", "label_source": "db"},
    ]
    new_db_rows = [
        {"id": "a", "label": "no"},   # DB export disagrees with the owner
        {"id": "c", "label": "yes"},
    ]
    merged = ex.merge_export_rows(existing, new_db_rows)
    by_id = {r["id"]: r for r in merged}
    assert by_id["a"]["label"] == "yes"
    assert by_id["a"]["label_source"] == "owner"
    assert by_id["c"]["label_source"] == "db"
    assert "b" not in by_id  # dropped: was db-sourced and absent from this export


def test_merge_export_rows_tags_new_rows_db_without_mutating_input():
    new_rows = [{"id": "x", "label": "yes"}]
    merged = ex.merge_export_rows([], new_rows)
    assert merged == [{"id": "x", "label": "yes", "label_source": "db"}]
    assert "label_source" not in new_rows[0]  # input untouched


def test_run_export_keeps_owner_rows_after_a_re_export(monkeypatch, tmp_path):
    (tmp_path / "threads.jsonl").write_text(
        json.dumps({
            "id": "owner-row-1", "set": "threads", "label": "yes",
            "features": {}, "site_id": "s1", "company_id": "c1",
            "decided_at": "2026-09-01T00:00:00Z", "baseline": {"score": 0.5},
            "label_source": "owner", "topic_ids": ["t-later", "t-earlier"],
        }) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "counts.json").write_text(json.dumps({
        "threads": {"label_source_breakdown": {"owner": 1}},
    }), encoding="utf-8")

    empty_result = json.dumps({"records": []})

    def _fake_run(args, **kwargs):
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "execute-statement" in args:
            if "FROM topics t WHERE t.id IN" in args[args.index("--sql") + 1]:
                return _FakeCompleted(stdout=json.dumps({"records": [
                    [{"stringValue": "t-later"}], [{"stringValue": "t-earlier"}]]}))
            return _FakeCompleted(stdout=empty_result)
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        raise AssertionError(f"unexpected aws call: {args}")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    counts = ex.run_export("fieldsight_test", out_dir=tmp_path)

    threads_rows = [json.loads(line) for line in
                    (tmp_path / "threads.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert len(threads_rows) == 1
    assert threads_rows[0]["id"] == "owner-row-1"
    assert threads_rows[0]["label_source"] == "owner"
    assert counts["threads"]["n"] == 1
    assert counts["threads"]["label_source_breakdown"] == {"owner": 1}


# ---------------------------------------------------------------------------
# Fix wave 4, B9: owner-labelled rows are re-checked against the deletion
# predicate on EVERY export, not just once at labelling time.
# ---------------------------------------------------------------------------

def test_owner_row_topic_ids_only_for_owner_rows_with_the_field():
    assert ex.owner_row_topic_ids({"label_source": "db", "topic_ids": ["t1"]}, "threads") == []
    assert ex.owner_row_topic_ids(
        {"label_source": "owner", "topic_ids": ["t1", "t2"]}, "threads") == ["t1", "t2"]
    # Fix wave 5, item 6: a partially-missing id list is unverifiable, not
    # "check the ids that are there".
    assert ex.owner_row_topic_ids(
        {"label_source": "owner", "topic_ids": ["t1", None]}, "threads") is None


def test_owner_row_without_topic_ids_fails_closed():
    # Fix wave 5, item 6: work_class ids ARE topic ids -> fall back to the id;
    # a threads id is a hash of two topic ids -> unverifiable (None).
    assert ex.owner_row_topic_ids(
        {"id": "topic-9", "label_source": "owner"}, "work_class") == ["topic-9"]
    assert ex.owner_row_topic_ids(
        {"id": "threads:abc", "label_source": "owner"}, "threads") is None


def test_apply_owner_deletion_predicate_drops_rows_with_any_invisible_topic():
    rows = [
        {"id": "a", "label_source": "owner", "topic_ids": ["t1", "t2"]},  # both visible
        {"id": "b", "label_source": "owner", "topic_ids": ["t1", "t3"]},  # t3 not visible
        {"id": "c", "label_source": "db"},  # nothing to check -- kept
        {"id": "d", "label_source": "owner"},  # pre-wave-4 threads row -- unverifiable
    ]
    kept, n_deleted, n_unverifiable = ex.apply_owner_deletion_predicate(
        rows, {"t1", "t2"}, "threads")
    assert {r["id"] for r in kept} == {"a", "c"}
    assert n_deleted == 1
    assert n_unverifiable == 1


def test_apply_owner_deletion_predicate_work_class_falls_back_to_row_id():
    rows = [
        {"id": "t1", "label_source": "owner"},  # pre-wave-4, topic visible
        {"id": "t9", "label_source": "owner"},  # pre-wave-4, topic deleted
    ]
    kept, n_deleted, n_unverifiable = ex.apply_owner_deletion_predicate(
        rows, {"t1"}, "work_class")
    assert [r["id"] for r in kept] == ["t1"]
    assert (n_deleted, n_unverifiable) == (1, 0)


def test_run_export_checks_pre_wave4_work_class_owner_row_by_its_id(monkeypatch, tmp_path):
    (tmp_path / "work_class.jsonl").write_text(
        json.dumps({
            "id": "topic-gone", "set": "work_class", "label": "yes",
            "features": {}, "site_id": "s1", "company_id": "c1",
            "decided_at": "2026-09-01T00:00:00Z", "baseline": {},
            "label_source": "owner",
        }) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "threads.jsonl").write_text(
        json.dumps({
            "id": "threads:old", "set": "threads", "label": "yes",
            "features": {}, "site_id": "s1", "company_id": "c1",
            "decided_at": "2026-09-01T00:00:00Z", "baseline": {"score": 0.5},
            "label_source": "owner",
        }) + "\n",
        encoding="utf-8",
    )
    seen_visibility_sql = []

    def _fake_run(args, **kwargs):
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "execute-statement" in args:
            sql = args[args.index("--sql") + 1]
            if "FROM topics t WHERE t.id IN" in sql:
                seen_visibility_sql.append(sql)
            return _FakeCompleted(stdout=json.dumps({"records": []}))
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        raise AssertionError(f"unexpected aws call: {args}")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)
    counts = ex.run_export("fieldsight_test", out_dir=tmp_path)

    assert any("topic-gone" in sql for sql in seen_visibility_sql)
    assert (tmp_path / "work_class.jsonl").read_text(encoding="utf-8").strip() == ""
    assert (tmp_path / "threads.jsonl").read_text(encoding="utf-8").strip() == ""
    assert counts["work_class"]["owner_rows_dropped_deleted"] == 1
    assert counts["threads"]["owner_rows_dropped_no_topic_ids"] == 1


def test_run_export_drops_owner_row_whose_topic_was_soft_deleted(monkeypatch, tmp_path):
    (tmp_path / "work_class.jsonl").write_text(
        json.dumps({
            "id": "topic-deleted", "set": "work_class", "label": "yes",
            "features": {}, "site_id": "s1", "company_id": "c1",
            "decided_at": "2026-09-01T00:00:00Z", "baseline": {},
            "label_source": "owner", "topic_ids": ["topic-deleted"],
        }) + "\n",
        encoding="utf-8",
    )

    empty_result = json.dumps({"records": []})

    def _fake_run(args, **kwargs):
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "execute-statement" in args:
            sql_index = args.index("--sql") + 1
            sql = args[sql_index]
            if "FROM topics t WHERE t.id IN" in sql:
                # The owner row's topic was soft-deleted since labelling --
                # it is no longer in the visible set.
                return _FakeCompleted(stdout=empty_result)
            return _FakeCompleted(stdout=empty_result)
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        raise AssertionError(f"unexpected aws call: {args}")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    counts = ex.run_export("fieldsight_test", out_dir=tmp_path)

    wc_rows = [json.loads(line) for line in
               (tmp_path / "work_class.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert wc_rows == []
    assert counts["work_class"]["owner_rows_dropped_deleted"] == 1


def test_run_export_keeps_owner_row_whose_topic_is_still_visible(monkeypatch, tmp_path):
    (tmp_path / "work_class.jsonl").write_text(
        json.dumps({
            "id": "topic-live", "set": "work_class", "label": "yes",
            "features": {}, "site_id": "s1", "company_id": "c1",
            "decided_at": "2026-09-01T00:00:00Z", "baseline": {},
            "label_source": "owner", "topic_ids": ["topic-live"],
        }) + "\n",
        encoding="utf-8",
    )

    def _fake_run(args, **kwargs):
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "execute-statement" in args:
            sql_index = args.index("--sql") + 1
            sql = args[sql_index]
            if "FROM topics t WHERE t.id IN" in sql:
                return _FakeCompleted(stdout=json.dumps(
                    {"records": [[{"stringValue": "topic-live"}]]}))
            return _FakeCompleted(stdout=json.dumps({"records": []}))
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        raise AssertionError(f"unexpected aws call: {args}")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    counts = ex.run_export("fieldsight_test", out_dir=tmp_path)

    wc_rows = [json.loads(line) for line in
               (tmp_path / "work_class.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert len(wc_rows) == 1
    assert wc_rows[0]["id"] == "topic-live"
    assert counts["work_class"]["owner_rows_dropped_deleted"] == 0


# ---------------------------------------------------------------------------
# Fix wave 4, B10: alias-lookup company ids must include owner rows, not
# only fresh DB rows.
# ---------------------------------------------------------------------------

def test_run_export_includes_owner_row_company_id_in_alias_lookup(monkeypatch, tmp_path):
    (tmp_path / "threads.jsonl").write_text(
        json.dumps({
            "id": "owner-row-2", "set": "threads", "label": "yes",
            "features": {}, "site_id": "s1", "company_id": "owner-only-co",
            "decided_at": "2026-09-01T00:00:00Z", "baseline": {"score": 0.5},
            "label_source": "owner", "topic_ids": ["tA", "tB"],
        }) + "\n",
        encoding="utf-8",
    )

    seen_company_id_queries = []

    def _fake_run(args, **kwargs):
        if "begin-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionId": "tx-1"}))
        if "execute-statement" in args:
            sql_index = args.index("--sql") + 1
            sql = args[sql_index]
            if "owner-only-co" in sql:
                seen_company_id_queries.append(sql)
            if "FROM topics t WHERE t.id IN" in sql:
                return _FakeCompleted(stdout=json.dumps(
                    {"records": [[{"stringValue": "tA"}], [{"stringValue": "tB"}]]}))
            return _FakeCompleted(stdout=json.dumps({"records": []}))
        if "rollback-transaction" in args:
            return _FakeCompleted(stdout=json.dumps({"transactionStatus": "RolledBack"}))
        raise AssertionError(f"unexpected aws call: {args}")

    monkeypatch.setattr(ex.subprocess, "run", _fake_run)

    ex.run_export("fieldsight_test", out_dir=tmp_path)

    # The owner row's company id must have reached at least one of the
    # alias-lookup queries (name_aliases/users/companies/sites/tasks).
    assert seen_company_id_queries
