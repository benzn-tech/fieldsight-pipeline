"""
Tests for src/repositories/findings.py -- Task 1 of the programme-impact-link
plan (TDD):

  docs/superpowers/plans/2026-07-13-programme-impact-link.md (Task 1)
  docs/superpowers/specs/2026-07-13-unified-extraction-labeling-design.md (S4/S5)

  - insert_findings: batch-insert per-topic findings, flattening the
    extractor's nested entity{name,trade} dict into entity_name/entity_trade
    columns; domain/severity values outside the CHECK enum are passed as
    NULL rather than raising (fail-open -- this is Claude output, never
    trust its shape). Impact columns are left NULL at insert time
    (apply_impact fills them later, downstream of the matcher). Empty
    findings list short-circuits to [] with no query.
  - apply_impact: UPDATE ... WHERE id=%s RETURNING; rowcount 0 (row gone --
    a normal race with nightly supersession/re-extraction, D4/D5 of the
    plan) returns None, never raises. impact_evidence is wrapped in
    psycopg's Jsonb (chunks.py convention).
  - list_for_topics: ANY(%s) batched read, mirrors topics.py:143-147 (no
    N+1 -- one query regardless of how many topic_ids are passed).

A FakeConn/FakeCursor double records every execute() call's SQL text +
params so behaviour can be asserted without a real Postgres (mirrors the
FakeConn style of tests/unit/test_programme_suggestions_repo.py).
"""
from psycopg.types.json import Jsonb

from repositories import findings as repo


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


def _finding_row(**overrides):
    row = {
        "id": "f-1", "topic_id": "topic-1", "site_id": "site-1",
        "observation": "Missing guardrail on level 3", "domain": "safety",
        "severity": "major", "entity_name": "ABC Scaffolding",
        "entity_trade": "scaffolder", "recommended_action": "Install guardrail",
        "programme_task_id": None, "impact_severity": None, "impact_note": None,
        "impact_task_name": None, "impact_evidence": None, "impact_matched_at": None,
        "status": "open", "created_at": "2026-07-13T00:00:00Z",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# insert_findings
# ---------------------------------------------------------------------------

def test_insert_findings_empty_list_returns_empty_no_query():
    conn = FakeConn()

    assert repo.insert_findings(conn, "topic-1", "site-1", []) == []
    assert conn.calls == []


def test_insert_flattens_entity_and_nulls_bad_enum():
    row1 = _finding_row()
    row2 = _finding_row(id="f-2", observation="Bad enum test", domain=None,
                        severity=None, entity_name=None, entity_trade=None,
                        recommended_action=None)
    conn = FakeConn(results=[[row1], [row2]])

    extractor_findings = [
        {
            "observation": "Missing guardrail on level 3",
            "domain": "safety",
            "severity": "major",
            "entity": {"name": "ABC Scaffolding", "trade": "scaffolder"},
            "recommended_action": "Install guardrail",
        },
        {
            "observation": "Bad enum test",
            "domain": "not-a-real-domain",  # not in the CHECK enum
            "severity": "extreme",           # not in the CHECK enum
            "entity": {},
            "recommended_action": None,
        },
    ]

    result = repo.insert_findings(conn, "topic-1", "site-1", extractor_findings)

    assert result == [row1, row2]
    assert len(conn.calls) == 2

    sql1, params1 = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "INSERT INTO findings" in sql1
    assert "RETURNING" in sql1
    assert params1 == ("topic-1", "site-1", "Missing guardrail on level 3",
                       "safety", "major", "ABC Scaffolding", "scaffolder",
                       "Install guardrail")

    # invalid enum values pass through as NULL, never raise
    params2 = conn.calls[1]["params"]
    assert params2 == ("topic-1", "site-1", "Bad enum test", None, None,
                       None, None, None)


def test_insert_findings_missing_entity_key_defaults_to_null():
    conn = FakeConn(results=[[_finding_row()]])

    repo.insert_findings(conn, "topic-1", "site-1", [
        {"observation": "No entity key at all"}
    ])

    params = conn.calls[0]["params"]
    # entity_name, entity_trade are positions 5, 6 in the VALUES tuple
    assert params[5] is None
    assert params[6] is None


# ---------------------------------------------------------------------------
# apply_impact
# ---------------------------------------------------------------------------

def test_apply_impact_returns_none_when_row_gone():
    conn = FakeConn(results=[[]])

    result = repo.apply_impact(
        conn, "f-1", task_id="T-004", impact_severity="major",
        impact_note="Blocking pour", impact_task_name="Concrete Pour",
        impact_evidence={"cosine": 0.1})

    assert result is None
    sql = conn.calls[0]["sql"]
    assert "UPDATE findings SET" in sql
    assert "WHERE id=%s" in sql


def test_apply_impact_uses_jsonb_wrapper():
    updated_row = _finding_row(programme_task_id="T-004", impact_severity="major")
    conn = FakeConn(results=[[updated_row]])

    result = repo.apply_impact(
        conn, "f-1", task_id="T-004", impact_severity="major",
        impact_note="Blocking pour", impact_task_name="Concrete Pour",
        impact_evidence={"cosine": 0.1})

    assert result == updated_row
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "impact_matched_at=now()" in sql
    assert "programme_task_id=%s" in sql
    assert "impact_severity=%s" in sql
    assert "impact_note=%s" in sql
    assert "impact_task_name=%s" in sql
    assert "impact_evidence=%s" in sql

    jsonb_params = [p for p in params if isinstance(p, Jsonb)]
    assert len(jsonb_params) == 1
    assert jsonb_params[0].obj == {"cosine": 0.1}
    assert params[-1] == "f-1"  # finding_id is the WHERE param, last in tuple


# ---------------------------------------------------------------------------
# list_for_topics
# ---------------------------------------------------------------------------

def test_list_for_topics_batches():
    row1 = _finding_row()
    row2 = _finding_row(id="f-2", topic_id="topic-2")
    conn = FakeConn(results=[[row1, row2]])

    rows = repo.list_for_topics(conn, ["topic-1", "topic-2"])

    assert rows == [row1, row2]
    assert len(conn.calls) == 1  # ONE query, no N+1
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "topic_id = ANY(%s)" in sql
    assert "ORDER BY created_at" in sql
    assert params == (["topic-1", "topic-2"],)
