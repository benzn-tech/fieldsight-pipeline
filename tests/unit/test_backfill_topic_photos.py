"""Unit tests for scripts/backfill_topic_photos.py OWN logic.

The photo->topic MATCHER (photo_binding.photos_for_topics) is already unit
tested elsewhere and is NOT retested here. These tests cover only what this
script adds: (1) folder derivation from source_s3_key, (2) candidate grouping
(no day-level skip), (3) PHOTO-LEVEL idempotency exclusion + the dry-run-vs-
apply branch (no insert call in dry-run). The Data API + S3 clients are mocked
-- no AWS, no network.
"""
import os
import sys
from unittest.mock import Mock

# scripts/ is not on the pytest pythonpath (only src/ is) -- add it.
_SCRIPTS = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import backfill_topic_photos as bf  # noqa: E402


# ---------------------------------------------------------------------------
# (1) folder derivation
# ---------------------------------------------------------------------------
def test_derive_folder_extractions_is_part_index_1():
    assert bf.derive_folder("extractions/Ben_UCPK/2026-07-23/sess.json") == "Ben_UCPK"


def test_derive_folder_reports_is_part_index_2():
    assert bf.derive_folder("reports/2026-07-23/Ben_UCPK/daily_report.json") == "Ben_UCPK"


def test_derive_folder_underscored_name_kept_verbatim():
    assert bf.derive_folder("extractions/Jarley_Trainor/2026-03-02/s.json") == "Jarley_Trainor"


def test_derive_folder_unknown_prefix_is_none():
    assert bf.derive_folder("config/user_mapping.json") is None
    assert bf.derive_folder("") is None
    assert bf.derive_folder(None) is None


# ---------------------------------------------------------------------------
# (2) candidate grouping -- NO day-level skip (idempotency is photo-level now)
# ---------------------------------------------------------------------------
def _row(src, date, tid, tr=None):
    return {"id": tid, "source_s3_key": src, "report_date": date, "time_range": tr}


def test_partially_bound_day_is_still_a_candidate():
    # Regression: a day where the live writer already bound SOME photos must
    # NOT be skipped whole -- the unbound photo (Ben_UCPK/2026-07-23's 10:40)
    # would be stranded forever. Photo-level exclusion (not day-skip) handles
    # already-bound photos, so plan_candidates keeps every day with topics.
    rows = [
        _row("extractions/Ben_UCPK/2026-07-23/s.json", "2026-07-23", "t1", tr="10:39 - 10:39"),
        _row("extractions/Ben_UCPK/2026-07-23/s.json", "2026-07-23", "t2", tr="12:12 - 12:13"),
    ]
    candidates, stats = bf.plan_candidates(rows)
    assert stats == {"days_total": 1}
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["folder"] == "Ben_UCPK"
    assert cand["date"] == "2026-07-23"
    # Input order preserved -> stable index order for photos_for_topics.
    assert [t["id"] for t in cand["topics"]] == ["t1", "t2"]


def test_folder_scope_filters_other_folders():
    rows = [
        _row("extractions/Ben_UCPK/2026-07-23/s.json", "2026-07-23", "t1"),
        _row("extractions/Ada_L/2026-07-23/s.json", "2026-07-23", "t2"),
    ]
    candidates, _ = bf.plan_candidates(rows, folder="Ben_UCPK")
    assert len(candidates) == 1
    assert candidates[0]["folder"] == "Ben_UCPK"


def test_unknown_prefix_rows_are_ignored():
    rows = [_row("weird/key/shape.json", "2026-07-23", "t1")]
    candidates, stats = bf.plan_candidates(rows)
    assert candidates == []
    assert stats == {"days_total": 0}


# ---------------------------------------------------------------------------
# (3) photo-level idempotency exclusion + dry-run vs apply branch
# ---------------------------------------------------------------------------
def _s3_with_photos(*keys):
    """A fake S3 client whose paginator yields the given picture objects --
    shaped for photo_binding.list_pictures (get_paginator('list_objects_v2'))."""
    s3 = Mock()
    paginator = Mock()
    paginator.paginate.return_value = [{"Contents": [{"Key": k} for k in keys]}]
    s3.get_paginator.return_value = paginator
    return s3


def _s3_with_one_photo(key):
    return _s3_with_photos(key)


def _candidate_that_binds():
    # Topic window 10:39-10:39; photo at 10:40 is 1 min out -> within the
    # ±2 min tolerance rule -> photos_for_topics binds it to topic index 0.
    return {
        "folder": "Ben_UCPK",
        "date": "2026-07-23",
        "topics": [{"id": "t-10", "time_range": "10:39 - 10:39"}],
    }


PHOTO_KEY = "users/Ben_UCPK/pictures/2026-07-23/Benl1_2026-07-23_10-40-00.jpg"


def test_dry_run_makes_no_insert_call():
    s3 = _s3_with_one_photo(PHOTO_KEY)
    execute = Mock()  # the Data API executor -- must NOT be touched in dry-run

    result = bf.process_candidate(_candidate_that_binds(), s3, "bucket",
                                  execute, apply=False)

    execute.assert_not_called()          # <-- the core safety assertion
    assert result["inserted"] == 0
    # It still REPORTS the would-be binding (dry-run is informative).
    assert len(result["bindings"]) == 1
    assert result["bindings"][0]["s3_key"] == PHOTO_KEY
    assert result["bindings"][0]["topic_id"] == "t-10"
    assert result["bindings"][0]["distance_min"] == 1


def test_apply_issues_one_insert_only_statement():
    s3 = _s3_with_one_photo(PHOTO_KEY)
    execute = Mock()
    execute.return_value = {"records": [[{"stringValue": "new-id"}]]}

    result = bf.process_candidate(_candidate_that_binds(), s3, "bucket",
                                  execute, apply=True)

    assert execute.call_count == 1
    sql = execute.call_args.args[0]
    # The ONLY statement is the NOT-EXISTS-guarded INSERT -- never DELETE/UPDATE.
    assert sql.strip().upper().startswith("INSERT INTO TOPIC_PHOTOS")
    assert "NOT EXISTS" in sql.upper()
    assert "DELETE" not in sql.upper()
    assert "UPDATE" not in sql.upper()
    assert result["inserted"] == 1


def test_apply_idempotent_insert_reports_zero_when_guard_absorbs():
    # NOT-EXISTS guard hit -> RETURNING yields no row -> counted as not inserted.
    s3 = _s3_with_one_photo(PHOTO_KEY)
    execute = Mock()
    execute.return_value = {"records": []}

    result = bf.process_candidate(_candidate_that_binds(), s3, "bucket",
                                  execute, apply=True)

    assert execute.call_count == 1
    assert result["inserted"] == 0


def test_insert_sql_constant_is_insert_only():
    # Guard against a future edit sneaking a destructive verb into the one
    # statement this script can write.
    sql = bf.INSERT_TOPIC_PHOTO_SQL.upper()
    assert sql.strip().startswith("INSERT INTO TOPIC_PHOTOS")
    assert "NOT EXISTS" in sql
    for forbidden in ("DELETE", "UPDATE", "DROP", "TRUNCATE"):
        assert forbidden not in sql


# ---- photo-level exclusion: the partial-day regression the coordinator hit ----
PHOTO_A = "users/Ben_UCPK/pictures/2026-07-23/Benl1_2026-07-23_10-40-00.jpg"  # already bound
PHOTO_B = "users/Ben_UCPK/pictures/2026-07-23/Benl1_2026-07-23_12-16-00.jpg"  # unbound, in tol


def _partial_day_candidate():
    # topic tA (10:39 window) already owns PHOTO_A; topic tB (12:15 window)
    # is the home for PHOTO_B (12:16 = 1 min out -> within ±2 min tolerance).
    return {
        "folder": "Ben_UCPK",
        "date": "2026-07-23",
        "topics": [{"id": "tA", "time_range": "10:39 - 10:39"},
                   {"id": "tB", "time_range": "12:15 - 12:15"}],
    }


def test_already_bound_photo_excluded_unbound_photo_still_binds():
    # Day carries BOTH photos on S3; PHOTO_A is already bound (in bound_keys).
    # (a) PHOTO_A must NOT be re-matched/attempted; (b) PHOTO_B (in tolerance)
    # DOES bind. Proves partial-day rescue + no double-bind of A.
    s3 = _s3_with_photos(PHOTO_A, PHOTO_B)
    execute = Mock()
    execute.return_value = {"records": [[{"stringValue": "id-b"}]]}

    result = bf.process_candidate(_partial_day_candidate(), s3, "bucket",
                                  execute, apply=True, bound_keys={PHOTO_A})

    # (a) A never re-fed to the matcher / never inserted.
    assert result["already_bound"] == 1
    assert all(b["s3_key"] != PHOTO_A for b in result["bindings"])
    # (b) B bound to its topic tB.
    assert len(result["bindings"]) == 1
    assert result["bindings"][0]["s3_key"] == PHOTO_B
    assert result["bindings"][0]["topic_id"] == "tB"
    # Exactly one INSERT, for B only.
    assert execute.call_count == 1
    inserted_key = execute.call_args.kwargs["params"][1]["value"]["stringValue"]
    assert inserted_key == PHOTO_B
    assert result["inserted"] == 1


def test_all_photos_already_bound_is_a_noop():
    s3 = _s3_with_photos(PHOTO_A, PHOTO_B)
    execute = Mock()
    result = bf.process_candidate(_partial_day_candidate(), s3, "bucket",
                                  execute, apply=True, bound_keys={PHOTO_A, PHOTO_B})
    assert result["already_bound"] == 2
    assert result["bindings"] == []
    execute.assert_not_called()


def test_fetch_bound_photo_keys_parses_and_shortcircuits():
    execute = Mock()
    # No topic ids -> no round-trip.
    assert bf.fetch_bound_photo_keys(execute, []) == set()
    execute.assert_not_called()

    execute.return_value = {"formattedRecords": '[{"s3_key": "k1"}, {"s3_key": "k2"}]'}
    assert bf.fetch_bound_photo_keys(execute, ["tA", "tB"]) == {"k1", "k2"}
    # array param passed for the ANY(:ids::uuid[]) exclusion query.
    params = execute.call_args.kwargs["params"]
    assert params[0]["value"]["arrayValue"]["stringValues"] == ["tA", "tB"]
