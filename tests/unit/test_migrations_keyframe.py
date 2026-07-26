import os
import re

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                   "0024_keyframe_tombstones_events.sql")


def _sql():
    return open(MIG, encoding="utf-8").read().lower()


def _ddl():
    """SQL with `-- ...` comment lines stripped, so privacy assertions test the
    actual column definitions and not the explanatory prose."""
    lines = [ln for ln in _sql().splitlines() if not ln.strip().startswith("--")]
    return "\n".join(lines)


def test_keyframe_migration_creates_both_tables():
    sql = _sql()
    assert "create table keyframe_tombstones" in sql
    assert "create table keyframe_events" in sql
    # tombstone natural key is the durable s3_key (Section 1)
    assert "s3_key        text primary key" in sql or "s3_key text primary key" in sql
    # events enum guard
    assert "check (event in" in sql
    for col in ("topic_category", "work_class", "duration_min",
                "n_frames_generated", "frame_index"):
        assert col in sql, col
    # ratio-query indexes (Section 2)
    assert "idx_keyframe_events_slice" in sql
    assert "idx_keyframe_events_company" in sql


def test_keyframe_migration_is_additive_and_privacy_preserving():
    ddl = _ddl()
    # additive only -- never destructive on the shared cluster (BUG-38)
    assert not re.search(r"\bdrop\b|\balter\b", ddl)
    # no image/text/caption/transcript/url columns leaked into telemetry
    assert not re.search(r"\bcaption\b|\btranscript\b|\bimage\b|_url\b|text_\w", ddl)
    # THE no-FK invariant (Section 1): topic_id on tombstones must NOT reference
    # topics, or a re-extraction cascade would delete the very tombstone.
    assert not re.search(r"topic_id\s+uuid[^,]*references\s+topics", ddl)
