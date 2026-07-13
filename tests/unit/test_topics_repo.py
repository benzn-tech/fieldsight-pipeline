"""
Tests for src/repositories/topics.py additions — Phase 4b, Task 3 (TDD):

  - delete_topics_for_source_prefix: LIKE-prefix supersession delete used by
    lambda_ingest's nightly-report supersession of session-sourced (live
    extraction) items. S3 user folders legitimately contain underscores
    (e.g. extractions/Jarley_Trainor/...) and SQL LIKE treats '_' as a
    single-char wildcard (same as '%' for multi-char) -- both must be
    escaped or the DELETE would match unrelated rows.
  - list_topics_for_date: multi-site dashboard read (site_name/user_name
    joins + action_items/safety_observations children + is_live flag).

A FakeConn/FakeCursor double records every execute() call's SQL text +
params so behaviour can be asserted without a real Postgres (mirrors the
FakeConn style of tests/unit/test_lambda_ingest.py, adapted here to capture
SQL since these are repository-layer, not lambda-layer, tests).
"""
from repositories import topics


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


class FakeExecResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeConn:
    """`results` is consumed in call order: one entry per execute() call.
    For conn.execute() (bare DELETE, no cursor()) an entry is a plain int
    rowcount; for conn.cursor().execute() an entry is a list of row dicts."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)

    def execute(self, sql, params=None):
        self.calls.append({"sql": sql, "params": params})
        rowcount = self._results.pop(0) if self._results else 0
        return FakeExecResult(rowcount)


# ---------------------------------------------------------------------------
# delete_topics_for_source_prefix — LIKE wildcard escaping
# ---------------------------------------------------------------------------

def test_delete_topics_for_source_prefix_escapes_underscore():
    conn = FakeConn(results=[3])

    n = topics.delete_topics_for_source_prefix(
        conn, "extractions/Jarley_Trainor/2026-07-06/")

    assert n == 3
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "LIKE %s" in sql
    assert "ESCAPE '\\'" in sql
    # '_' is a LIKE single-char wildcard -- the literal underscores in the
    # user folder must be escaped, else this would match e.g.
    # 'extractions/JarleyXTrainor/...' too.
    assert params == (r"extractions/Jarley\_Trainor/2026-07-06/%",)


def test_delete_topics_for_source_prefix_escapes_percent():
    conn = FakeConn(results=[0])

    topics.delete_topics_for_source_prefix(conn, "extractions/100%_done/2026-07-06/")

    params = conn.calls[0]["params"]
    assert params == (r"extractions/100\%\_done/2026-07-06/%",)


# ---------------------------------------------------------------------------
# list_topics_for_date
# ---------------------------------------------------------------------------

def test_list_topics_for_date_empty_site_ids_returns_empty():
    conn = FakeConn()

    assert topics.list_topics_for_date(conn, [], "2026-07-06") == []
    assert conn.calls == []  # no query executed for an empty ACL scope


def test_list_topics_for_date_joins_and_children_and_is_live():
    topic_report = {
        "id": "t-1", "site_id": "site-1", "user_id": "u-1",
        "source_s3_key": "reports/2026-07-06/Jarley_Trainor/daily_report.json",
        "report_date": "2026-07-06", "occurred_at": None, "category": "safety",
        "title": "Safety Briefing", "summary": "s", "created_at": "2026-07-06T09:00:00Z",
        "site_name": "Test Site", "user_name": "Jarley Trainor",
    }
    topic_live = {
        "id": "t-2", "site_id": "site-1", "user_id": "u-1",
        "source_s3_key": "extractions/Jarley_Trainor/2026-07-06/Benl1_2026-07-06_10-00-00.json",
        "report_date": "2026-07-06", "occurred_at": None, "category": "progress",
        "title": "Block C Pour", "summary": "s2", "created_at": "2026-07-06T10:05:00Z",
        "site_name": "Test Site", "user_name": "Jarley Trainor",
    }
    action_row = {"id": "a-1", "topic_id": "t-2", "text": "Order tape", "responsible": None,
                  "deadline": None, "priority": None, "status": "open", "created_at": "c1"}
    safety_row = {"id": "s-1", "topic_id": "t-1", "observation": "Missing tape",
                  "risk_level": "medium", "location": None, "status": "open", "created_at": "c2"}

    conn = FakeConn(results=[
        [topic_report, topic_live],   # main topics query
        [action_row],                 # action_items children
        [safety_row],                 # safety_observations children
        [],                           # findings children
    ])

    rows = topics.list_topics_for_date(conn, ["site-1", "site-2"], "2026-07-06")

    assert len(conn.calls) == 4
    main_sql, main_params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "site_id = ANY(%s)" in main_sql
    assert "report_date=%s" in main_sql
    assert "LEFT JOIN sites" in main_sql
    assert "LEFT JOIN users" in main_sql
    assert main_params == (["site-1", "site-2"], "2026-07-06")

    # all three children queries scoped to the topic ids the main query returned
    assert conn.calls[1]["params"] == (["t-1", "t-2"],)
    assert conn.calls[2]["params"] == (["t-1", "t-2"],)
    assert conn.calls[3]["params"] == (["t-1", "t-2"],)
    assert "action_items" in conn.calls[1]["sql"]
    assert "safety_observations" in conn.calls[2]["sql"]
    assert "findings" in conn.calls[3]["sql"]

    by_id = {r["id"]: r for r in rows}
    assert by_id["t-2"]["action_items"] == [action_row]
    assert by_id["t-2"]["safety_observations"] == []
    assert by_id["t-1"]["safety_observations"] == [safety_row]
    assert by_id["t-1"]["action_items"] == []
    assert by_id["t-1"]["findings"] == []
    assert by_id["t-2"]["findings"] == []

    assert by_id["t-1"]["is_live"] is False
    assert by_id["t-2"]["is_live"] is True


def test_list_topics_attaches_findings_batched():
    """Task 5 of docs/superpowers/plans/2026-07-13-programme-impact-link.md:
    findings (migration 0010, incl. programme-impact columns) are attached
    to topics as a THIRD batched child query, mirroring action_items/safety.
    ONE query for ALL topics regardless of how many topics are returned --
    no N+1 (the FakeConn queue model makes this assertable: an N+1
    implementation would issue an extra execute() per topic, desyncing the
    results queue and the call count below)."""
    topic_a = {
        "id": "t-1", "site_id": "site-1", "user_id": "u-1",
        "source_s3_key": "extractions/Jarley_Trainor/2026-07-06/x.json",
        "report_date": "2026-07-06", "occurred_at": None, "category": "safety",
        "title": "T1", "summary": "s", "created_at": "c0",
        "site_name": "Test Site", "user_name": "Jarley Trainor",
    }
    topic_b = {**topic_a, "id": "t-2", "title": "T2"}
    topic_c = {**topic_a, "id": "t-3", "title": "T3"}
    finding_row = {
        "id": "f-1", "topic_id": "t-1", "site_id": "site-1",
        "observation": "Missing edge protection", "domain": "safety", "severity": "major",
        "entity_name": "Acme Scaffolding", "entity_trade": "scaffolding",
        "recommended_action": "Install edge protection",
        "programme_task_id": "task-42", "impact_severity": "major",
        "impact_note": "Blocks the Level 3 pour", "impact_task_name": "Level 3 Pour",
        "impact_evidence": {}, "impact_matched_at": "c3",
        "status": "open", "created_at": "c3",
    }

    conn = FakeConn(results=[
        [topic_a, topic_b, topic_c],  # main topics query (THREE topics)
        [],                           # action_items children
        [],                           # safety_observations children
        [finding_row],                # findings children -- ONE query, all topics
    ])

    rows = topics.list_topics_for_date(conn, ["site-1"], "2026-07-06")

    # exactly 4 execute() calls total for 3 topics: main + 3 batched children.
    # An N+1 findings implementation would need 3 extra calls (one per topic) -> 6.
    assert len(conn.calls) == 4
    findings_sql, findings_params = conn.calls[3]["sql"], conn.calls[3]["params"]
    assert "FROM findings" in findings_sql
    assert "topic_id = ANY(%s)" in findings_sql
    assert findings_params == (["t-1", "t-2", "t-3"],)  # one batched call, all topic ids

    by_id = {r["id"]: r for r in rows}
    assert by_id["t-1"]["findings"] == [finding_row]
    assert by_id["t-2"]["findings"] == []
    assert by_id["t-3"]["findings"] == []
    # raw flat columns, incl. programme-impact fields, exposed as-is
    assert by_id["t-1"]["findings"][0]["entity_name"] == "Acme Scaffolding"
    assert by_id["t-1"]["findings"][0]["programme_task_id"] == "task-42"
    assert by_id["t-1"]["findings"][0]["impact_severity"] == "major"


def test_list_topics_for_date_no_topics_skips_children_queries():
    conn = FakeConn(results=[[]])  # main query returns zero topics

    rows = topics.list_topics_for_date(conn, ["site-1"], "2026-07-06")

    assert rows == []
    assert len(conn.calls) == 1  # children queries never fire for an empty result
