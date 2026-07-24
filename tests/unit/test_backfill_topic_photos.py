"""Unit tests for scripts/backfill_topic_photos.py OWN logic.

The photo->topic MATCHER (photo_binding.photos_for_topics) is already unit
tested elsewhere and is NOT retested here. These tests cover only what this
script adds: (1) folder derivation from source_s3_key, (2) the
"skip days that already have topic_photos rows" idempotency gate, and (3) the
dry-run-vs-apply branch (no insert call in dry-run). The Data API + S3 clients
are mocked -- no AWS, no network.
"""
import os
import sys
from unittest.mock import Mock

import pytest

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
# (2) skip-days-with-rows idempotency gate + folder scoping
# ---------------------------------------------------------------------------
def _row(src, date, tid, tr=None, has_photos=False):
    return {"id": tid, "source_s3_key": src, "report_date": date,
            "time_range": tr, "has_photos": has_photos}


def test_day_with_existing_photos_is_skipped_whole():
    # Two topics on the same (folder, date); ONE already has a photo -> the
    # whole day is skipped (the live writer bound it).
    rows = [
        _row("extractions/Ben_UCPK/2026-07-23/s.json", "2026-07-23", "t1",
             tr="10:39 - 10:39", has_photos=True),
        _row("extractions/Ben_UCPK/2026-07-23/s.json", "2026-07-23", "t2",
             tr="12:12 - 12:13", has_photos=False),
    ]
    candidates, stats = bf.plan_candidates(rows)
    assert candidates == []
    assert stats == {"days_total": 1, "days_skipped_had_rows": 1}


def test_day_without_photos_becomes_a_candidate_in_order():
    rows = [
        _row("extractions/Ben_UCPK/2026-07-23/s.json", "2026-07-23", "t1", tr="10:39 - 10:39"),
        _row("extractions/Ben_UCPK/2026-07-23/s.json", "2026-07-23", "t2", tr="12:12 - 12:13"),
    ]
    candidates, stats = bf.plan_candidates(rows)
    assert stats == {"days_total": 1, "days_skipped_had_rows": 0}
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["folder"] == "Ben_UCPK"
    assert cand["date"] == "2026-07-23"
    # Input order preserved -> stable index order for photos_for_topics.
    assert [t["id"] for t in cand["topics"]] == ["t1", "t2"]


def test_has_photos_accepts_string_boolean_from_data_api():
    # Data API JSON may render booleans as 't'/'true' strings.
    rows = [_row("extractions/F/2026-07-23/s.json", "2026-07-23", "t1", has_photos="t")]
    candidates, stats = bf.plan_candidates(rows)
    assert candidates == []
    assert stats["days_skipped_had_rows"] == 1


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
    assert stats == {"days_total": 0, "days_skipped_had_rows": 0}


# ---------------------------------------------------------------------------
# (3) dry-run vs apply branch
# ---------------------------------------------------------------------------
def _s3_with_one_photo(key):
    """A fake S3 client whose paginator yields one picture object -- shaped for
    photo_binding.list_pictures (which uses get_paginator('list_objects_v2'))."""
    s3 = Mock()
    paginator = Mock()
    paginator.paginate.return_value = [{"Contents": [{"Key": key}]}]
    s3.get_paginator.return_value = paginator
    return s3


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
