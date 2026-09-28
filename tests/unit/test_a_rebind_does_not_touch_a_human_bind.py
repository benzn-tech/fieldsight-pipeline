"""What the day-wide rebind is NOT allowed to remove, and what it locks.

`replace_day_photo_bindings` deletes a whole day's rows before inserting the
new ones. That is the right shape -- the thing being repaired is a row that
should no longer exist, and an insert-only writer is exactly what produced 22
multi-bound photos on prod. It is also the shape that can destroy work nobody
can get back, so the bound on the DELETE is the part worth pinning:

  * 'binding'  -- derived. Recomputed from the day's photo list on every run,
                  so deleting it costs nothing.
  * 'keyframe' -- a frame pulled out of a video. The FILE is the evidence and
                  there is no time window to re-derive the bind from, so a
                  sweep would delete the only record that it belongs there.
  * 'human'    -- somebody chose this. Never derived, never re-derivable.

And the lock. Two sessions of one day could not race before this change --
each cleared and re-wrote only its own `source_s3_key` -- and a day-wide
replace can. An advisory lock has no observable effect other than the
statement that takes it, so "the lock is missing" and "the lock is held" look
identical from outside until the day two writers interleave.
"""
import pytest

topics = pytest.importorskip("repositories.topics", reason="requires psycopg")
photo_rebind = pytest.importorskip("photo_rebind", reason="requires psycopg")


class _Cursor:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class RecordingConn:
    """Records every statement. `rows` is what a topics read returns."""

    def __init__(self, rows=()):
        self.executed = []
        self._rows = list(rows)

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return _Cursor(rowcount=0)

    def cursor(self, row_factory=None):
        return _RecordingCursor(self)

    def sql(self):
        return [s for s, _ in self.executed]


class _RecordingCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        return _Cursor(rows=self._conn._rows)


# ---------------------------------------------------------------------------
# The DELETE's bound
# ---------------------------------------------------------------------------

def test_the_replace_deletes_only_machine_bindings():
    conn = RecordingConn()
    topics.replace_day_photo_bindings(conn, "Ben_UCPK2", "2026-09-22", [])
    deletes = [s for s in conn.sql() if s.startswith("DELETE")]
    assert len(deletes) == 1
    assert "source = 'binding'" in deletes[0], (
        "the delete is unbounded -- it would take keyframe and human rows with "
        "it: " + deletes[0])


def test_a_human_bind_is_not_deletable_by_the_rebind():
    """Stated as the property rather than as the string: whatever the SQL
    spells, it must not be able to match a row whose source is 'human'."""
    conn = RecordingConn()
    topics.replace_day_photo_bindings(conn, "Ben_UCPK2", "2026-09-22", [])
    delete = next(s for s in conn.sql() if s.startswith("DELETE"))
    assert "'human'" not in delete
    assert "'keyframe'" not in delete
    assert "source =" in delete, "an unfiltered delete matches every source"


def test_every_row_the_rebind_writes_is_marked_as_machine_bound():
    """Or the next rebind cannot tell its own rows from a human's, and the
    bound on the DELETE above protects nothing."""
    conn = RecordingConn()
    topics.replace_day_photo_bindings(conn, "Ben_UCPK2", "2026-09-22", [
        {"topic_id": "t-1", "s3_key": "users/B/pictures/2026-09-22/a.jpg",
         "caption_text": None},
    ])
    inserts = [s for s in conn.sql() if s.startswith("INSERT")]
    assert len(inserts) == 1
    assert "'binding'" in inserts[0]


def test_the_delete_is_scoped_to_one_folder_and_day():
    """Through `topics`, and bound on both source shapes. A delete that
    reached further would clear another day -- or another person -- every time
    a session was re-driven."""
    conn = RecordingConn()
    topics.replace_day_photo_bindings(conn, "Ben_UCPK2", "2026-09-22", [])
    _, params = next((s, p) for s, p in conn.executed if s.startswith("DELETE"))
    # The backslash is not noise. `_` is a LIKE wildcard, and every folder name
    # in this product contains one -- unescaped, `Ben_UCPK2` also matches
    # `BenXUCPK2`, so one person's rebind would clear another person's day.
    assert params == (r"extractions/Ben\_UCPK2/2026-09-22/%",
                      "reports/2026-09-22/Ben_UCPK2/daily_report.json")


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------

def _rebind(conn, monkeypatch, day_topics=()):
    monkeypatch.setattr(topics, "list_day_topics_for_binding",
                        lambda c, folder, date: list(day_topics))
    monkeypatch.setattr(topics, "replace_day_photo_bindings",
                        lambda c, folder, date, rows: 0)
    monkeypatch.setattr(photo_rebind.recordings, "session_local_span",
                        lambda *a, **k: None)
    return photo_rebind.rebind_day_photos(
        conn, "co-1", "Ben_UCPK2", "2026-09-22", [])


def test_the_day_is_locked_and_the_key_is_the_day(monkeypatch):
    conn = RecordingConn()
    _rebind(conn, monkeypatch)
    locks = [(s, p) for s, p in conn.executed if "pg_advisory_xact_lock" in s]
    assert len(locks) == 1, "the rebind must take exactly one lock"
    assert locks[0][1] == ("photobind:Ben_UCPK2:2026-09-22",), (
        "the lock key is not the scope the replace covers -- two sessions of "
        "one day would not exclude each other: %r" % (locks[0][1],))


def test_two_sessions_of_one_day_ask_for_the_SAME_lock(monkeypatch):
    """THE CONCURRENCY CASE, stated as the thing that makes it safe.

    Postgres serialises two holders of the same advisory key; it does nothing
    for two different keys. Before this change each writer locked its own
    extraction key, so two sessions of one day never collided -- and they never
    needed to, because each only touched its own rows. A day-wide replace makes
    them collide, and they only exclude each other if the KEY IS EQUAL.

    A real database cannot show this in a unit test (one connection, one
    transaction, and `pg_advisory_xact_lock` is re-entrant for the same
    session), so what is checked is the property that decides it: two different
    sessions of one day derive one key, and a different day derives another.
    """
    keys = []
    for _ in ("session A's run", "session B's run"):
        conn = RecordingConn()
        _rebind(conn, monkeypatch)
        keys += [p for s, p in conn.executed if "pg_advisory_xact_lock" in s]
    assert keys[0] == keys[1], "two runs of one day took different locks"

    other = RecordingConn()
    monkeypatch.setattr(topics, "list_day_topics_for_binding",
                        lambda c, folder, date: [])
    monkeypatch.setattr(topics, "replace_day_photo_bindings",
                        lambda c, folder, date, rows: 0)
    photo_rebind.rebind_day_photos(other, "co-1", "Ben_UCPK2", "2026-09-23", [])
    other_key = [p for s, p in other.executed if "pg_advisory_xact_lock" in s][0]
    assert other_key != keys[0], (
        "a different day takes the same lock -- every day of a backfill would "
        "serialise behind every other")


def test_the_lock_is_taken_before_anything_is_read_or_written(monkeypatch):
    """A lock taken after the read is a lock taken after the race."""
    order = []
    conn = RecordingConn()
    monkeypatch.setattr(topics, "list_day_topics_for_binding",
                        lambda c, folder, date: order.append("read") or [])
    monkeypatch.setattr(topics, "replace_day_photo_bindings",
                        lambda c, folder, date, rows: order.append("write") or 0)

    real_execute = conn.execute

    def spy(sql, params=None):
        if "pg_advisory_xact_lock" in sql:
            order.append("lock")
        return real_execute(sql, params)

    conn.execute = spy
    photo_rebind.rebind_day_photos(conn, "co-1", "Ben_UCPK2", "2026-09-22", [])
    assert order == ["lock", "read", "write"], order


def test_a_day_with_no_topics_still_clears_its_stale_rows(monkeypatch):
    """The branch it would have been easy to skip. A day whose topics were all
    deleted must end with no binding rows, not with yesterday's."""
    calls = []
    conn = RecordingConn()
    monkeypatch.setattr(topics, "list_day_topics_for_binding",
                        lambda c, folder, date: [])
    monkeypatch.setattr(topics, "replace_day_photo_bindings",
                        lambda c, folder, date, rows: calls.append(list(rows)) or 0)
    photo_rebind.rebind_day_photos(conn, "co-1", "Ben_UCPK2", "2026-09-22", [])
    assert calls == [[]], "the replace was skipped, so the stale rows survive"
