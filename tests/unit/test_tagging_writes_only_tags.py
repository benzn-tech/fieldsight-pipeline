"""Re-tagging writes TAGS. It does not touch a word of what was written before.

This is an owner-level boundary, not a preference: adding or changing the
taxonomy and re-tagging existing data must never alter a topic's body, an
action's text, a summary or a report. So the constraint is enforced by the
SHAPE of the writer rather than by remembering:

  * `apply_tags` and `rollback_run` take no content argument. There is no
    parameter through which body text could travel, so no caller can pass one
    by mistake and no future edit can add one without being obvious;
  * the test below captures EVERY statement a full run issues and asserts the
    set is exactly the four it is allowed to make. An `UPDATE topics` anywhere
    in the path fails here, including one added by a helper three calls deep.

A test that only checked `apply_tags` in isolation would pass on a run that
rewrote every summary in some other function; capturing the whole run is what
makes the assertion mean what it says.
"""
import pytest

pytest.importorskip("psycopg", reason="the repo layer needs psycopg")
from repositories import tag_writes  # noqa: E402


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class RecordingConn:
    """Every statement, in order, with its params."""

    def __init__(self, rows=()):
        self.executed = []
        self._rows = list(rows)

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return _Cursor(self._rows)

    def cursor(self, row_factory=None):
        return _Cur(self)

    def verbs(self):
        """The distinct statement shapes issued, normalised to
        '<VERB> <table>' so the assertion is about what was touched rather
        than about whitespace."""
        out = set()
        for sql, _ in self.executed:
            parts = sql.split()
            if not parts:
                continue
            verb = parts[0].upper()
            if verb == "INSERT":
                out.add("INSERT " + parts[2])
            elif verb == "UPDATE":
                out.add("UPDATE " + parts[1])
            elif verb == "DELETE":
                out.add("DELETE " + parts[2])
            elif verb == "SELECT":
                out.add("SELECT")
            else:
                out.add(verb)
        return out


class _Cur:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        return _Cursor(self._conn._rows)


ALLOWED = {"INSERT tag_run", "UPDATE tag_run", "INSERT topic_tags",
           "INSERT action_item_tags"}


def _full_run(conn):
    """One complete re-tag: open a run, tag a topic, tag an action, close it.

    BOTH KINDS, because the boundary has to hold on both paths. Action tagging
    was added after this test existed, and a capture that only walked the topic
    path would have gone on passing while the new one wrote whatever it liked.
    """
    run = tag_writes.start_run(conn, company_id="co-1", taxonomy_version=1,
                               method="classifier", created_by="u-1")
    tag_writes.apply_tags(conn, "topic", "t-1", ["tag-a", "tag-b"],
                          source="classifier", run_id=run["id"], confidence=0.9)
    tag_writes.apply_tags(conn, "action_item", "a-1", ["tag-a"],
                          source="classifier", run_id=run["id"])
    tag_writes.finish_run(conn, run["id"], stats={"topics": 1})
    return run


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------

def test_a_whole_run_touches_only_the_four_tag_statements():
    conn = RecordingConn(rows=[{"id": "run-1"}])
    _full_run(conn)
    extra = conn.verbs() - ALLOWED
    assert not extra, f"a re-tag run issued {sorted(extra)}"


def test_nothing_in_the_path_writes_to_topics_or_action_items():
    """Stated separately and positively, because the set assertion above would
    also fail for a harmless new SELECT and this one names the actual danger."""
    conn = RecordingConn(rows=[{"id": "run-1"}])
    _full_run(conn)
    for sql, _ in conn.executed:
        assert not sql.startswith("UPDATE topics"), sql
        assert not sql.startswith("UPDATE action_items"), sql
        assert not sql.startswith("DELETE FROM topics"), sql
        assert not sql.startswith("DELETE FROM action_items"), sql


def test_the_writer_has_no_parameter_content_could_travel_through():
    """The structural half. A run cannot rewrite a summary it was never given,
    so the signature is the guarantee and the SQL assertions above are the
    check on it."""
    import inspect
    for fn in (tag_writes.apply_tags, tag_writes.rollback_run):
        names = set(inspect.signature(fn).parameters)
        for forbidden in ("text", "title", "summary", "body", "content",
                          "topic", "action", "row", "payload"):
            assert forbidden not in names, (fn.__name__, forbidden)


# ---------------------------------------------------------------------------
# Re-entrant, and reversible as a batch
# ---------------------------------------------------------------------------

def test_applying_the_same_tag_twice_is_not_an_error():
    """A re-tag is re-driven routinely -- a retry, a resumed batch, an operator
    running it again. It has to be safe to run twice."""
    conn = RecordingConn(rows=[{"id": "run-1"}])
    tag_writes.apply_tags(conn, "topic", "t-1", ["tag-a"],
                          source="classifier", run_id="run-1")
    insert = next(s for s, _ in conn.executed if s.startswith("INSERT"))
    assert "ON CONFLICT" in insert and "DO NOTHING" in insert, insert


def test_a_run_can_be_rolled_back_by_its_id():
    conn = RecordingConn()
    tag_writes.rollback_run(conn, "run-1")
    deletes = [s for s, _ in conn.executed if s.startswith("DELETE")]
    assert len(deletes) == 2, "both assignment tables must be cleared"
    assert all("run_id = %s" in d for d in deletes), deletes


def test_a_rollback_never_removes_what_a_person_chose():
    """The same rule the photo rebind learned: a machine may replace what a
    machine wrote, never what a human did. A human tag carrying a run_id (they
    corrected one row inside a batch) must survive the batch being undone."""
    conn = RecordingConn()
    tag_writes.rollback_run(conn, "run-1")
    for sql, _ in conn.executed:
        if sql.startswith("DELETE"):
            assert "source <> 'human'" in sql or "source != 'human'" in sql, sql


def test_a_rollback_marks_the_run_rather_than_deleting_it():
    """A run row that vanishes takes the record of what happened with it."""
    conn = RecordingConn()
    tag_writes.rollback_run(conn, "run-1")
    assert any(s.startswith("UPDATE tag_run") for s, _ in conn.executed)
    assert not any(s.startswith("DELETE FROM tag_run") for s, _ in conn.executed)


def test_an_unknown_entity_kind_is_refused_rather_than_guessed():
    """Two tables, chosen by a string. A typo must not silently write nothing
    and report success."""
    conn = RecordingConn()
    with pytest.raises(ValueError):
        tag_writes.apply_tags(conn, "topics", "t-1", ["tag-a"],
                              source="classifier", run_id="run-1")


def test_an_unknown_source_is_refused():
    """`source` decides what a later run may overwrite. A value outside the
    vocabulary would be neither machine nor human and no rebind would ever
    touch it again."""
    conn = RecordingConn()
    with pytest.raises(ValueError):
        tag_writes.apply_tags(conn, "topic", "t-1", ["tag-a"],
                              source="guessed", run_id="run-1")


def test_tagging_nothing_writes_nothing():
    conn = RecordingConn()
    n = tag_writes.apply_tags(conn, "topic", "t-1", [],
                              source="classifier", run_id="run-1")
    assert n == 0
    assert conn.executed == []
