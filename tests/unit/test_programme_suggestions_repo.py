"""
Tests for src/repositories/programme_suggestions.py — Task 1 of the
programme<->item feedback plan (TDD):

  docs/superpowers/specs/2026-07-12-programme-item-feedback-design.md (S4, D3)
  docs/superpowers/plans/2026-07-12-programme-item-feedback.md (Task 1)

  - _dedupe_key: sha256(site_id|task_id|report_date|norm(topic_title)),
    norm = lowercase + collapse whitespace, so re-processing the same
    topic/task/day never re-suggests under a different key.
  - upsert_suggestion: INSERT ... ON CONFLICT (dedupe_key) DO UPDATE SET
    topic_id=EXCLUDED.topic_id, updated_at=now() WHERE state='pending' --
    decided (confirmed/rejected/stale) rows are immutable to the pipeline.
  - list_for_site / get / decide / mark_stale.

A FakeConn/FakeCursor double records every execute() call's SQL text +
params so behaviour can be asserted without a real Postgres (mirrors the
FakeConn style of tests/unit/test_topics_repo.py /
tests/unit/test_repositories_identity.py).
"""
from psycopg.types.json import Jsonb

from repositories import programme_suggestions as repo


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop_result()
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """`results` is consumed in call order: one entry per execute() call.
    Each entry is a list of row dicts (fetchall) or a single row dict
    (fetchone via first element; empty list -> None, mirroring a real
    RETURNING/SELECT that matched zero rows)."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


SUGGESTION_ROW = {
    "id": "sugg-1", "site_id": "site-1", "task_id": "T-004", "topic_id": "topic-1",
    "topic_title": "Floor inserts", "topic_summary": "s", "topic_user_id": "u-1",
    "report_date": "2026-07-12", "source_s3_key": "extractions/x/2026-07-12/y.json",
    "task_name": "Floor Inserts", "task_status_before": "in_progress",
    "task_progress_before": 40, "suggested_status": "in_progress",
    "suggested_progress": 60, "confidence": 0.82, "match_evidence": {"cosine": 0.12},
    "dedupe_key": "abc123", "state": "pending", "decided_by": None, "decided_at": None,
    "applied_status": None, "applied_progress": None,
    "created_at": "2026-07-12T00:00:00Z", "updated_at": "2026-07-12T00:00:00Z",
}


def _upsert_kwargs(**overrides):
    kwargs = dict(
        site_id="site-1", task_id="T-004", topic_id="topic-1",
        topic_title="Floor  Inserts ", topic_summary="s", topic_user_id="u-1",
        report_date="2026-07-12", source_s3_key="extractions/x/2026-07-12/y.json",
        task_name="Floor Inserts", task_status_before="in_progress",
        task_progress_before=40, suggested_status="in_progress",
        suggested_progress=60, confidence=0.82, match_evidence={"cosine": 0.12},
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# _dedupe_key
# ---------------------------------------------------------------------------

def test_dedupe_key_stable_across_title_whitespace():
    s, t, d = "site-1", "T-004", "2026-07-12"

    assert (repo._dedupe_key(s, t, d, "Floor  Inserts ")
            == repo._dedupe_key(s, t, d, "floor inserts"))


def test_dedupe_key_differs_on_task_id():
    s, d, title = "site-1", "2026-07-12", "Floor Inserts"

    assert repo._dedupe_key(s, "T-004", d, title) != repo._dedupe_key(s, "T-005", d, title)


# ---------------------------------------------------------------------------
# upsert_suggestion
# ---------------------------------------------------------------------------

def test_upsert_conflict_only_updates_pending():
    conn = FakeConn(results=[[SUGGESTION_ROW]])

    row = repo.upsert_suggestion(conn, **_upsert_kwargs())

    assert row == SUGGESTION_ROW
    assert len(conn.calls) == 1
    sql = conn.calls[0]["sql"]
    assert "ON CONFLICT (dedupe_key) DO UPDATE SET" in sql
    assert "topic_id = EXCLUDED.topic_id" in sql
    assert "updated_at = now()" in sql
    # decided rows (confirmed/rejected/stale) are immutable to the pipeline
    assert "WHERE programme_progress_suggestions.state = 'pending'" in sql


def test_upsert_computes_dedupe_key_and_wraps_evidence_as_jsonb():
    conn = FakeConn(results=[[SUGGESTION_ROW]])
    kwargs = _upsert_kwargs()

    repo.upsert_suggestion(conn, **kwargs)

    params = conn.calls[0]["params"]
    expected_key = repo._dedupe_key(
        kwargs["site_id"], kwargs["task_id"], kwargs["report_date"], kwargs["topic_title"])
    assert expected_key in params

    jsonb_params = [p for p in params if isinstance(p, Jsonb)]
    assert len(jsonb_params) == 1
    assert jsonb_params[0].obj == {"cosine": 0.12}


def test_upsert_conflict_no_matching_pending_row_returns_none():
    # ON CONFLICT DO UPDATE ... WHERE state='pending' that finds no eligible
    # row returns nothing from RETURNING -- decided rows stay untouched.
    conn = FakeConn(results=[[]])

    assert repo.upsert_suggestion(conn, **_upsert_kwargs()) is None


# ---------------------------------------------------------------------------
# list_for_site
# ---------------------------------------------------------------------------

def test_list_for_site_defaults_to_pending():
    conn = FakeConn(results=[[SUGGESTION_ROW]])

    rows = repo.list_for_site(conn, "site-1")

    assert rows == [SUGGESTION_ROW]
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "site_id=%s" in sql
    assert "ORDER BY report_date DESC, created_at DESC" in sql
    assert params == ("site-1", "pending", "pending")


def test_list_for_site_state_none_returns_all():
    conn = FakeConn(results=[[SUGGESTION_ROW]])

    repo.list_for_site(conn, "site-1", state=None)

    params = conn.calls[0]["params"]
    assert params == ("site-1", None, None)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def test_get_returns_row():
    conn = FakeConn(results=[[SUGGESTION_ROW]])

    assert repo.get(conn, "sugg-1") == SUGGESTION_ROW
    assert conn.calls[0]["params"] == ("sugg-1",)


def test_get_missing_returns_none():
    conn = FakeConn(results=[[]])

    assert repo.get(conn, "ghost") is None


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------

def test_decide_guards_pending():
    # A row that isn't pending (already confirmed/rejected/stale) yields
    # zero RETURNING rows -- the fake models that with an empty result.
    conn = FakeConn(results=[[]])

    result = repo.decide(conn, "sugg-1", "confirmed", "user-1",
                         applied_status="completed", applied_progress=100)

    assert result is None
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "WHERE id=%s AND state='pending'" in sql
    assert params == ("confirmed", "user-1", "completed", 100, "sugg-1")


def test_decide_pending_row_is_confirmed():
    confirmed_row = dict(SUGGESTION_ROW, state="confirmed", decided_by="user-1")
    conn = FakeConn(results=[[confirmed_row]])

    result = repo.decide(conn, "sugg-1", "confirmed", "user-1")

    assert result == confirmed_row


# ---------------------------------------------------------------------------
# mark_stale
# ---------------------------------------------------------------------------

def test_mark_stale_guards_pending():
    conn = FakeConn(results=[[]])

    assert repo.mark_stale(conn, "sugg-1") is None
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "state='stale'" in sql
    assert "WHERE id=%s AND state='pending'" in sql
    assert params == ("sugg-1",)


def test_mark_stale_pending_row_transitions():
    stale_row = dict(SUGGESTION_ROW, state="stale")
    conn = FakeConn(results=[[stale_row]])

    assert repo.mark_stale(conn, "sugg-1") == stale_row
