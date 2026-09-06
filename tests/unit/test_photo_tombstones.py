"""Photos become something a customer can actually delete.

Until now a photo could only disappear as a SIDE EFFECT: photos are visible
solely as `related_photos` on a topic, so hiding the topic hid them. Any surface
that lists photos from `recordings` directly -- which is what the day view needs
in order to survive an extraction outage -- bypasses the only mechanism that
hides them and would put deleted photos back on screen.

The link between a photo and a session is the clock and nothing else. A photo
key carries no session id: `users/{folder}/pictures/{date}/IMG.jpg` and the
session tombstone `extractions/{folder}/{date}/sid{hex}` share a folder and a
day, and that is all. So the span rule is EVIDENCE, not proof, and its two
limits are asserted here rather than left to be discovered:

  * photos taken between sessions are not covered by any session deletion;
  * a session with no `recordings` rows has no span and covers nothing -- and
    must say so out loud.
"""
import logging

import pytest

redactions = pytest.importorskip("repositories.redactions")


class _Cur:
    def __init__(self, rows, sink):
        self._rows, self._sink = rows, sink

    def execute(self, sql, params=None):
        self._sink.append((sql, params))
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows=()):
        self.rows, self.sql = list(rows), []

    def cursor(self, **kw):
        return _Cur(self.rows, self.sql)

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        return _Cur(self.rows, self.sql)


# --------------------------------------------------------------------------
# The tombstone
# --------------------------------------------------------------------------

def test_a_photo_is_tombstoned_by_its_exact_key(monkeypatch):
    """Not a prefix. A recording tombstone names an extraction prefix matched
    with LIKE; a photo names one object. Folding them into one target_type
    would make every reader guess which comparison it wanted."""
    seen = {}
    monkeypatch.setattr(redactions, "create_redaction",
                        lambda *a, **k: seen.update(args=a, kw=k) or True)
    redactions.create_photo_tombstone(
        _Conn(), "c-1", "users/Ada_L/pictures/2026-09-02/IMG_1.jpg",
        "deleted by the user", "u-1", "pm", batch_id="b-1")
    assert seen["kw"]["target_type"] == "photo"
    assert seen["kw"]["target_key"] == "users/Ada_L/pictures/2026-09-02/IMG_1.jpg"
    assert seen["kw"]["batch_id"] == "b-1"


def test_the_target_id_is_deterministic(monkeypatch):
    """A retry must decline the conflict, not raise it. Same key -> same uuid,
    which is what lets the partial unique index do its job."""
    ids = []
    monkeypatch.setattr(redactions, "create_redaction",
                        lambda conn, cid, tid, *a, **k: ids.append(tid) or True)
    for _ in range(2):
        redactions.create_photo_tombstone(_Conn(), "c-1", "users/A/pictures/d/x.jpg",
                                          "r", "u", "pm", batch_id="b")
    assert ids[0] == ids[1]


def test_it_joins_the_delete_batch_so_revert_can_undo_it(monkeypatch):
    """`revert_batch` restores by batch_id. A photo tombstone written outside
    the batch is one no revert can bring back -- the opposite failure, created
    while fixing this one."""
    seen = {}
    monkeypatch.setattr(redactions, "create_redaction",
                        lambda *a, **k: seen.update(k) or True)
    redactions.create_photo_tombstone(_Conn(), "c", "k", "r", "u", "pm", batch_id="batch-9")
    assert seen["batch_id"] == "batch-9"


# --------------------------------------------------------------------------
# Reading them back
# --------------------------------------------------------------------------

def test_an_empty_candidate_list_never_touches_the_database():
    """`= ANY('{}')` is silently false for every row, which reads as "nothing
    is deleted" rather than "nothing was asked". A day with no photos must not
    pay for the query either."""
    conn = _Conn()
    assert redactions.deleted_photo_keys(conn, "c-1", keys=[]) == set()
    assert conn.sql == []


def test_it_asks_only_about_the_keys_it_was_given():
    conn = _Conn(rows=[{"target_key": "k1"}])
    out = redactions.deleted_photo_keys(conn, "c-1", keys=["k1", "k2"])
    sql, params = conn.sql[0]
    assert out == {"k1"}
    assert "target_type = 'photo'" in sql
    assert "reverted_at IS NULL" in sql          # a reverted delete is not a delete
    assert ["k1", "k2"] in params


def test_no_company_pin_means_no_company_clause():
    """None must mean 'unrestricted', not 'company IS NULL'. An empty-list-style
    overload of one value for two meanings is how this repo has leaked before."""
    conn = _Conn(rows=[])
    redactions.deleted_photo_keys(conn, None, keys=["k"])
    sql, _ = conn.sql[0]
    assert "company_id" not in sql


# --------------------------------------------------------------------------
# The span rule, and both of its limits
# --------------------------------------------------------------------------

recordings = pytest.importorskip("repositories.recordings")


def test_a_session_with_no_rows_has_no_span():
    """RealPTT, days predating migration 0009, lake-fed files. Returning a
    guessed span here would tombstone photos on evidence that does not exist."""
    assert recordings.session_span(_Conn(rows=[{"lo": None, "hi": None}]),
                                   "c", "Ada_L", "2026-09-02", "sidabc") is None


def test_a_span_is_returned_when_the_rows_are_there():
    lo, hi = "2026-09-02T10:00:00Z", "2026-09-02T10:40:00Z"
    assert recordings.session_span(_Conn(rows=[{"lo": lo, "hi": hi}]),
                                   "c", "Ada_L", "2026-09-02", "sidabc") == (lo, hi)


def test_the_span_query_folds_a_null_ended_at_into_started_at():
    """Every row of a session can carry a NULL `ended_at`; MAX(ended_at) alone
    would return NULL and silently turn a real session into 'no span'."""
    conn = _Conn(rows=[{"lo": 1, "hi": 2}])
    recordings.session_span(conn, "c", "Ada_L", "2026-09-02", "sidabc")
    sql, _ = conn.sql[0]
    assert "COALESCE(ended_at, started_at)" in sql


def test_photo_lookup_is_scoped_to_the_folder_the_day_and_the_span():
    conn = _Conn(rows=[{"s3_key": "users/Ada_L/pictures/2026-09-02/a.jpg"}])
    out = recordings.photo_keys_in_span(conn, "c-1", "Ada_L", "2026-09-02", "lo", "hi")
    sql, params = conn.sql[0]
    assert out == ["users/Ada_L/pictures/2026-09-02/a.jpg"]
    assert "kind = 'photo'" in sql
    assert "started_at BETWEEN" in sql
    assert "c-1" in params
    # ESCAPED. `_` is a single-character wildcard in LIKE, so an unescaped
    # `Ada_L` also matches `AdaXL` -- a folder belonging to somebody else.
    # Asserting the escaped form is the point, not an artefact of it.
    assert "users/Ada\\_L/%/2026-09-02/%" in params


def test_a_photo_with_no_timestamp_is_not_swept_in():
    """`started_at IS NOT NULL` is in the query on purpose. A NULL compared with
    BETWEEN is not true, but relying on that would leave the next reader unable
    to tell whether the exclusion was intended."""
    conn = _Conn(rows=[])
    recordings.photo_keys_in_span(conn, "c", "Ada_L", "2026-09-02", "lo", "hi")
    sql, _ = conn.sql[0]
    assert "started_at IS NOT NULL" in sql
