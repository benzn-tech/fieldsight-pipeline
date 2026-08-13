"""Unit: the schema a user-facing delete stands on.

Plan: docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md phase 1.
Spec: docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md.

The spec claimed `create_redaction` already took a `scope`, so adding `'deleted'` would be
a parameter change. It is not: 0022 pins `scope` and `target_type` with CHECK constraints
and `target_id` is `uuid NOT NULL`, which cannot hold an S3 key. The INSERT the feature
needs is rejected by the database as it stands today.

Three columns carry the weight, and each exists because a review found the alternative
broken:

* **`target_key`** — the tombstone is keyed on the SOURCE, not the topic id. Topics are
  deleted and re-inserted with NEW uuids every time the pipeline supersedes a day
  (`lambda_ingest`), so a topic-keyed tombstone stops matching within a day and the
  deleted content comes back overnight.
* **`batch_id`** — "one revert restores exactly what one delete hid" is unimplementable
  without it.
* **the partial unique index** — `create_redaction` has no uniqueness, so a retried
  request stacks duplicate tombstones and the revert count stops matching the delete.
"""
import os
import re

MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src", "migrations", "0041_user_deletion.sql")


def _sql():
    with open(MIG, encoding="utf-8") as f:
        return f.read()


def _statements():
    """The SQL with `--` comments stripped.

    The first version of the destructive-statement check scanned the whole file and failed
    on a COMMENT explaining that no destructive statement belongs here. A guard that reads
    prose is a guard that fires on the explanation of itself."""
    return " ".join(ln.split("--")[0] for ln in _sql().splitlines())


def test_the_migration_exists():
    assert os.path.exists(MIG), "0041 is the whole of phase 1"


def test_scope_check_is_widened_by_name_not_recreated():
    """The constraint names were read off the LIVE database before this was written --
    0022 wrote inline CHECKs and Postgres auto-named them. A migration guessing the name
    fails at deploy, after the code that depends on it has already merged."""
    sql = _sql()
    assert re.search(r"DROP CONSTRAINT (IF EXISTS )?redactions_scope_check", sql)
    assert re.search(r"scope IN \([^)]*'deleted'", sql), "no 'deleted' in the new CHECK"


def test_target_type_check_admits_a_recording():
    """The tombstone points at a recording/session, not a topic -- 0022 pins target_type
    too, so widening scope alone still rejects the INSERT."""
    sql = _sql()
    assert re.search(r"DROP CONSTRAINT (IF EXISTS )?redactions_target_type_check", sql)
    assert re.search(r"target_type IN \([^)]*'recording'", sql)


def test_the_three_columns_the_feature_cannot_work_without():
    sql = _sql()
    assert "ADD COLUMN" in sql and "batch_id" in sql, "no batch = no per-batch revert"
    assert "target_key" in sql, "no source key = the tombstone dies on re-ingest"


def test_a_retry_cannot_stack_two_active_tombstones():
    """Without this a retried delete writes a second tombstone, and then the revert count
    no longer matches the delete count -- which is the only evidence the feature works."""
    sql = _sql()
    assert re.search(r"CREATE UNIQUE INDEX[^;]*reverted_at IS NULL", sql, re.S)


def test_nothing_in_the_migration_deletes_anything():
    """This feature's whole premise is that nothing is destroyed. A migration that drops a
    column or a row would be the one irreversible step in a design built to be reversed."""
    sql = _statements().upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "DELETE FROM", "TRUNCATE"):
        assert forbidden not in sql, f"{forbidden} has no place in this migration"


# ---- the repository half ----

class _FakeCur:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append((sql, params))
        return self

    def fetchone(self):
        return {"id": "r-1"}


class _FakeConn:
    def __init__(self):
        self.calls = []

    def cursor(self, row_factory=None):
        return _FakeCur(self.calls)


def test_create_redaction_binds_batch_and_target_key():
    """The three columns have to reach the INSERT, not just exist on the table.

    A column added by a migration and never bound is the shape of every
    'the switch exists but nothing sets it' defect in this repo.
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "src"))
    import repositories.redactions as red

    conn = _FakeConn()
    red.create_redaction(conn, "c-1", "00000000-0000-0000-0000-000000000001",
                         "user deleted the recording", "u-1", "pm",
                         scope="deleted", batch_id="b-1",
                         target_key="extractions/Ben/2026-08-13/",
                         target_type="recording")
    sql, params = conn.calls[-1]
    assert "batch_id" in sql and "target_key" in sql, f"not bound: {sql}"
    assert "b-1" in params and "extractions/Ben/2026-08-13/" in params
    assert "deleted" in params and "recording" in params


def test_the_existing_callers_are_untouched():
    """`analysis` redactions must keep working with the old signature -- the
    life-conversation feature is live and this is the same table."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "src"))
    import repositories.redactions as red

    conn = _FakeConn()
    red.create_redaction(conn, "c-1", "00000000-0000-0000-0000-000000000002",
                         "non-work", "u-1", "pm")
    sql, params = conn.calls[-1]
    assert "analysis" in params and None in params, "batch_id/target_key default to NULL"
