"""Unit: migration 0056 has the shape the writer and reader rely on.

Text checks only -- the SQL is applied for real by tests/integration (CI Postgres).
The version guard matters because two version collisions have already shipped
(0041, 0044) and the runner orders ties by filename, so a second 0056 from a
parallel branch would apply in an order no one chose.
"""
import os

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
NAME = "0056_day_recording_segments.sql"


def _sql():
    with open(os.path.join(MIGRATIONS, NAME), encoding="utf-8") as fh:
        return fh.read()


def test_no_other_migration_uses_version_0056():
    assert [f for f in os.listdir(MIGRATIONS) if f.startswith("0056_")] == [NAME]


def test_it_keys_one_row_per_user_and_day_and_references_users():
    sql = " ".join(_sql().split())
    assert "CREATE TABLE IF NOT EXISTS day_recording_segments" in sql
    assert "user_id uuid NOT NULL REFERENCES users(id)" in sql
    assert "PRIMARY KEY (user_id, report_date)" in sql


def test_it_stores_segments_and_the_debounce_state_not_blocks():
    sql = " ".join(_sql().split())
    for column in ("folder_name text NOT NULL", "segments jsonb NOT NULL",
                   "source_object_count int NOT NULL",
                   "dirty boolean NOT NULL DEFAULT false",
                   "computed_at timestamptz NOT NULL DEFAULT now()"):
        assert column in sql, column
    assert "gap_seconds" not in sql and "blocks jsonb" not in sql
