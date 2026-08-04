"""
Unit: the non-work auto-retention sweep (life-conversation separation phase 2).

The behaviours worth pinning are the ones whose failure mode is irreversible
or invisible:

  * it refuses to run without an explicit start floor (otherwise the first run
    tombstones every historical personal topic — the policy is new-topics-only);
  * it tombstones rather than deletes, and never touches transcript/audio;
  * a reindex failure does not abort the batch or undo the tombstone;
  * the query excludes already-redacted topics, so the sweep is idempotent.

The SQL-level guards (work_class, buffer cutoff, NOT EXISTS, folder_name NOT
NULL) are asserted on the emitted statement; real matching runs against a live
DB in the integration suite.
"""
import datetime

import pytest

import lambda_nonwork_expiry as nx
from repositories import topics


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop()
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def cursor(self, **kwargs):
        return FakeCursor(self)

    def _pop(self):
        return self._results.pop(0) if self._results else []


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
SINCE = datetime.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _topic(tid="t-1"):
    return {"id": tid, "company_id": "co-1", "report_date": "2026-08-03",
            "folder_name": "Ben_UCPK2"}


# ---- the floor is mandatory ------------------------------------------------

def test_parse_since_accepts_a_date_and_a_timestamp():
    assert nx.parse_since("2026-08-04") == datetime.datetime(2026, 8, 4, tzinfo=UTC)
    assert nx.parse_since("2026-08-04T06:30:00Z") == datetime.datetime(
        2026, 8, 4, 6, 30, tzinfo=UTC)


def test_parse_since_returns_none_when_unset_or_malformed():
    """None is what makes the handler refuse to run — a missing floor must
    never be silently treated as 'from the beginning of time'."""
    for raw in ("", None, "not-a-date", "2026-13-99"):
        assert nx.parse_since(raw) is None


def test_handler_refuses_to_run_without_a_floor(monkeypatch):
    monkeypatch.setattr(nx, "ENABLED", True)
    monkeypatch.setattr(nx, "SINCE", "")
    # No DB import happens on this path; a connection attempt would fail the test.
    assert nx.lambda_handler({}, None) == {"status": "no_floor"}


def test_handler_is_inert_when_disabled(monkeypatch):
    monkeypatch.setattr(nx, "ENABLED", False)
    assert nx.lambda_handler({}, None) == {"status": "disabled"}


# ---- what the sweep does to a due topic ------------------------------------

def test_due_topic_is_tombstoned_and_deindexed(monkeypatch):
    conn = FakeConn(results=[[_topic()]])
    created, enqueued = [], []
    monkeypatch.setattr(nx.redactions, "create_redaction",
                        lambda *a, **k: created.append((a, k)))
    monkeypatch.setattr(nx.reindex, "enqueue_topic_reindex",
                        lambda *a: enqueued.append(a))
    monkeypatch.setattr(nx, "s3", lambda: "s3")

    counts = nx.expire_batch(conn, now=NOW, since=SINCE, buffer_hours=24, limit=500)

    assert counts == {"scanned": 1, "tombstoned": 1, "reindex_failed": 0}
    (args, kwargs) = created[0]
    assert args[1] == "co-1" and args[2] == "t-1"
    assert args[3] == "non_work_auto_expiry", "must be distinguishable from a human redaction"
    assert args[4] is None and args[5] == "system", "policy acted, not a person"
    assert enqueued[0][3:] == ("t-1", "Ben_UCPK2", "2026-08-03")


def test_buffer_cutoff_is_now_minus_the_window(monkeypatch):
    conn = FakeConn(results=[[]])
    monkeypatch.setattr(nx, "s3", lambda: "s3")
    nx.expire_batch(conn, now=NOW, since=SINCE, buffer_hours=24, limit=500)

    older_than, created_since, _limit = conn.calls[0]["params"]
    assert older_than == NOW - datetime.timedelta(hours=24)
    assert created_since == SINCE


def test_reindex_failure_keeps_the_tombstone_and_continues(monkeypatch):
    """Losing RAG removal on one topic must not abort the batch: the remaining
    topics would stay exposed, which is worse than one stale vector."""
    conn = FakeConn(results=[[_topic("t-1"), _topic("t-2")]])
    created = []
    monkeypatch.setattr(nx.redactions, "create_redaction",
                        lambda *a, **k: created.append(a[2]))

    def boom(s3c, bucket, conn_, tid, folder, date):
        if tid == "t-1":
            raise RuntimeError("s3 down")

    monkeypatch.setattr(nx.reindex, "enqueue_topic_reindex", boom)
    monkeypatch.setattr(nx, "s3", lambda: "s3")

    counts = nx.expire_batch(conn, now=NOW, since=SINCE, buffer_hours=24, limit=500)

    assert counts == {"scanned": 2, "tombstoned": 2, "reindex_failed": 1}
    assert created == ["t-1", "t-2"], "both tombstoned despite the failure"


def test_nothing_due_is_a_clean_no_op(monkeypatch):
    conn = FakeConn(results=[[]])
    monkeypatch.setattr(nx.redactions, "create_redaction",
                        lambda *a, **k: pytest.fail("must not redact"))
    monkeypatch.setattr(nx, "s3", lambda: "s3")

    assert nx.expire_batch(conn, now=NOW, since=SINCE, buffer_hours=24, limit=500) == {
        "scanned": 0, "tombstoned": 0, "reindex_failed": 0}


# ---- the query's own guards ------------------------------------------------

def test_query_selects_only_unredacted_non_work_with_a_folder():
    conn = FakeConn(results=[[]])
    topics.list_expired_non_work(conn, older_than=NOW, created_since=SINCE, limit=10)
    sql = conn.calls[0]["sql"]

    assert "work_class = 'non_work'" in sql
    assert "t.created_at < %s" in sql and "t.created_at >= %s" in sql
    assert "NOT EXISTS" in sql and "reverted_at IS NULL" in sql, "idempotent: skip redacted"
    assert "u.folder_name IS NOT NULL" in sql, "unattributed cannot be de-indexed"
    # It selects the keys it needs to act, never the personal text itself.
    selected = sql.split("FROM")[0]
    for content_col in ("summary", "title", "t.participants"):
        assert content_col not in selected


def test_query_requires_a_floor_as_a_keyword():
    """created_since has no default: omitting it is a TypeError, not a silent
    sweep of all history."""
    with pytest.raises(TypeError):
        topics.list_expired_non_work(FakeConn(), older_than=NOW, limit=10)


def test_reindex_import_chain_survives_this_function_s_env(monkeypatch):
    """enqueue_topic_reindex does `from lambda_org_api import render_report_shape`
    INSIDE the call, so the whole org-api module loads at runtime — under this
    lambda's env, not org-api's. A module-level lookup org-api has but this
    function does not set would fail only in production, on the first topic
    that ever expires. Import it here under exactly the vars the template
    grants (see NonWorkExpiryFunction.Environment)."""
    import importlib
    for k in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD", "S3_BUCKET"):
        monkeypatch.setenv(k, "x")
    for k in ("DATA_BUCKET", "ORG_USER_POOL_ID", "QR_CODES_TABLE", "LAKE_BUCKET"):
        monkeypatch.delenv(k, raising=False)

    importlib.import_module("lambda_org_api")
