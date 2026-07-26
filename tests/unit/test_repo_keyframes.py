"""Tests for repositories/keyframes.py (video-keyframe Q7). FakeConn records the
executed SQL/params, style of test_repo_content_edits.py."""
import pytest

kf = pytest.importorskip("repositories.keyframes",
                         reason="requires psycopg (installed in CI)")


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """`execute(...)` (default cursor path) and `cursor(...).execute(...)`
    (dict_row path) both hand back the same recording cursor. `rows` governs
    what RETURNING/SELECT yields; None -> empty (ON CONFLICT DO NOTHING)."""

    def __init__(self, rows=None):
        self.cur = FakeCursor(rows or [])
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self.cur.execute(sql, params)

    def cursor(self, *a, **k):
        return self.cur


def test_add_tombstone_is_idempotent_on_conflict():
    # first insert: RETURNING yields the key -> True (new row)
    conn = FakeConn(rows=[{"s3_key": "k"}])
    assert kf.add_tombstone(conn, "k", "co-1", "t-1", "u-1") is True
    assert "on conflict (s3_key) do nothing" in conn.cur.sql.lower()
    assert conn.cur.params == ("k", "co-1", "t-1", "u-1")
    # second insert: conflict -> RETURNING empty -> False (no second row)
    conn2 = FakeConn(rows=[])
    assert kf.add_tombstone(conn2, "k", "co-1", "t-1", "u-1") is False


def test_tombstoned_subset_empty_input_skips_db():
    conn = FakeConn(rows=[{"s3_key": "k"}])
    assert kf.tombstoned_subset(conn, []) == set()
    assert conn.calls == []          # empty input never touches the DB


def test_tombstoned_subset_returns_membership():
    conn = FakeConn(rows=[{"s3_key": "a"}, {"s3_key": "c"}])
    got = kf.tombstoned_subset(conn, ["a", "b", "c"])
    assert got == {"a", "c"}
    assert "any(%s::text[])" in conn.cur.sql.lower()
    assert conn.cur.params == (["a", "b", "c"],)


def test_record_event_maps_columns():
    conn = FakeConn(rows=[{"id": "e-1", "event": "generated"}])
    kf.record_event(conn, "generated", company_id="co-1", site_id="s-1",
                    topic_category="safety", work_class="work", duration_min=6,
                    n_frames_generated=3, frame_index=1)
    # exactly the 0024 structural columns, event last, in INSERT order
    assert conn.cur.params == ("co-1", "s-1", "safety", "work", 6, 3, 1, "generated")
    sql = conn.cur.sql.lower()
    # no image/text/caption/s3_key leaked into telemetry
    for banned in ("s3_key", "caption", "transcript", "image"):
        assert banned not in sql


def test_record_event_defaults_are_all_nullable():
    conn = FakeConn(rows=[{"id": "e-1"}])
    kf.record_event(conn, "deleted")
    assert conn.cur.params == (None, None, None, None, None, None, None, "deleted")
