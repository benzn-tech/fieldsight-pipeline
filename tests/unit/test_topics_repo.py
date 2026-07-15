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

Authority-flip migration 0011 additions (docs/superpowers/plans/2026-07-14-
authority-flip.md Task 1):
  - upsert_topic gains time_range/participants kwargs (participants via
    Jsonb) and action_items children carry deadline_text.
  - has_topics_for_source_prefix / list_topics_for_source_prefix: new reads
    keyed on source_s3_key prefix (org-api timeline shim), sharing the same
    _escape_like() LIKE-wildcard escaping as delete_topics_for_source_prefix.
"""
from psycopg.types.json import Jsonb

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


# ---------------------------------------------------------------------------
# upsert_topic — time_range/participants/deadline_text (migration 0011)
# ---------------------------------------------------------------------------

def test_upsert_passes_time_range_participants_jsonb():
    """time_range/participants are new display-field kwargs (migration 0011)
    the extraction JSON already carries but the Aurora boundary dropped.
    participants is bound via Jsonb (chunks.py/findings.py convention),
    same as every other jsonb column in this codebase."""
    inserted_topic = {
        "id": "t-1", "site_id": "site-1", "user_id": None, "source_s3_key": None,
        "report_date": "2026-07-06", "occurred_at": None, "category": None,
        "title": "Morning briefing", "summary": None,
        "time_range": "08:00-08:15", "participants": ["Ben", "Sam"], "source": "ai",
        "created_at": "c0",
    }
    conn = FakeConn(results=[[inserted_topic]])

    topic = topics.upsert_topic(
        conn, "site-1", "2026-07-06", "Morning briefing",
        time_range="08:00-08:15", participants=["Ben", "Sam"])

    assert topic == inserted_topic
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "time_range" in sql
    assert "participants" in sql
    assert "08:00-08:15" in params
    jsonb_params = [p for p in params if isinstance(p, Jsonb)]
    assert len(jsonb_params) == 1
    assert jsonb_params[0].obj == ["Ben", "Sam"]


def test_upsert_action_items_carry_deadline_text():
    """action_items[].deadline_text is the raw free-text deadline
    ("Tomorrow 08:00") the date-typed `deadline` column can't hold
    (lambda_ingest._map_action_items nulls it today)."""
    inserted_topic = {
        "id": "t-1", "site_id": "site-1", "user_id": None, "source_s3_key": None,
        "report_date": "2026-07-06", "occurred_at": None, "category": None,
        "title": "T", "summary": None, "time_range": None, "participants": None,
        "source": "ai", "created_at": "c0",
    }
    conn = FakeConn(results=[[inserted_topic], 1])

    topics.upsert_topic(
        conn, "site-1", "2026-07-06", "T",
        action_items=[{"text": "Order tape", "deadline_text": "Tomorrow 08:00"}])

    assert len(conn.calls) == 2
    action_sql, action_params = conn.calls[1]["sql"], conn.calls[1]["params"]
    assert "deadline_text" in action_sql
    assert "Tomorrow 08:00" in action_params


# ---------------------------------------------------------------------------
# has_topics_for_source_prefix / list_topics_for_source_prefix (0011,
# org-api timeline shim reads)
# ---------------------------------------------------------------------------

def test_has_topics_for_source_prefix_escapes_like():
    conn = FakeConn(results=[[{"?column?": 1}]])

    result = topics.has_topics_for_source_prefix(
        conn, "extractions/Jarley_Trainor/2026-07-06/")

    assert result is True
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "LIKE %s" in sql
    assert "ESCAPE '\\'" in sql
    assert "LIMIT 1" in sql
    # same underscore-escaping requirement as delete_topics_for_source_prefix
    assert params == (r"extractions/Jarley\_Trainor/2026-07-06/%",)


def test_list_for_source_prefix_orders_by_time_range_and_batches_four_children():
    """Mirrors list_topics_for_date's JOIN + batched-children pattern, plus a
    FOURTH batched child (photos, from topic_photos) and D3's stable
    ORDER BY time_range NULLS LAST, created_at, id."""
    topic_a = {
        "id": "t-1", "site_id": "site-1", "user_id": "u-1",
        "source_s3_key": "extractions/Jarley_Trainor/2026-07-06/a.json",
        "report_date": "2026-07-06", "occurred_at": None, "category": "safety",
        "title": "T1", "summary": "s", "time_range": "08:00-08:15",
        "participants": ["Ben"], "source": "ai", "created_at": "c0",
        "site_name": "Test Site", "user_name": "Jarley Trainor",
    }
    topic_b = {**topic_a, "id": "t-2", "title": "T2", "time_range": None}
    action_row = {"id": "a-1", "topic_id": "t-1", "text": "Order tape",
                  "responsible": None, "deadline": None, "deadline_text": "Tomorrow 08:00",
                  "priority": None, "status": "open", "created_at": "c1"}
    safety_row = {"id": "s-1", "topic_id": "t-2", "observation": "Missing tape",
                  "risk_level": "medium", "location": None, "status": "open", "created_at": "c2"}
    finding_row = {"id": "f-1", "topic_id": "t-1", "observation": "x"}
    photo_row = {"id": "p-1", "topic_id": "t-2", "s3_key": "k.jpg", "caption_text": None}

    conn = FakeConn(results=[
        [topic_a, topic_b],   # main topics query
        [action_row],         # action_items children
        [safety_row],         # safety_observations children
        [finding_row],        # findings children
        [photo_row],          # photos children -- the fourth batched child
    ])

    rows = topics.list_topics_for_source_prefix(
        conn, "extractions/Jarley_Trainor/2026-07-06/")

    assert len(conn.calls) == 5  # main + 4 batched children, never N+1
    main_sql, main_params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "source_s3_key LIKE %s" in main_sql
    assert "ESCAPE '\\'" in main_sql
    assert "ORDER BY t.time_range NULLS LAST, t.created_at, t.id" in main_sql
    assert main_params == (r"extractions/Jarley\_Trainor/2026-07-06/%",)

    for i in (1, 2, 3, 4):
        assert conn.calls[i]["params"] == (["t-1", "t-2"],)
    assert "action_items" in conn.calls[1]["sql"]
    assert "deadline_text" in conn.calls[1]["sql"]
    assert "safety_observations" in conn.calls[2]["sql"]
    assert "findings" in conn.calls[3]["sql"]
    assert "topic_photos" in conn.calls[4]["sql"]

    by_id = {r["id"]: r for r in rows}
    assert by_id["t-1"]["action_items"] == [action_row]
    assert by_id["t-2"]["action_items"] == []
    assert by_id["t-2"]["safety_observations"] == [safety_row]
    assert by_id["t-1"]["safety_observations"] == []
    assert by_id["t-1"]["findings"] == [finding_row]
    assert by_id["t-2"]["findings"] == []
    assert by_id["t-2"]["photos"] == [photo_row]
    assert by_id["t-1"]["photos"] == []


# ---------------------------------------------------------------------------
# list_extraction_folder_names_for_date (authority-flip Task 4, admin
# disambiguation multi-tenant guard)
# ---------------------------------------------------------------------------

def test_list_extraction_folder_names_for_date_scopes_by_company():
    conn = FakeConn(results=[[{"folder_name": "Jarley_Trainor"}, {"folder_name": "Ada_L"}]])

    folders = topics.list_extraction_folder_names_for_date(conn, "co-1", "2026-07-14")

    assert folders == ["Jarley_Trainor", "Ada_L"]
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "u.company_id=%s" in sql
    assert "JOIN users u ON u.id = t.user_id" in sql
    assert "extractions/%%" in sql  # '%%' escapes the literal '%' for psycopg's %s paramstyle
    assert params == ("2026-07-14", "co-1")


def test_list_extraction_folder_names_for_date_empty():
    conn = FakeConn(results=[[]])

    assert topics.list_extraction_folder_names_for_date(conn, "co-1", "2026-07-14") == []


def test_list_topics_for_date_selects_new_columns():
    """_TOPIC_COLS_JOINED gains time_range/participants/source (additive) --
    live-items consumers get these for free off the existing dashboard read,
    no new query, no new children."""
    conn = FakeConn(results=[[]])  # main query returns zero topics

    topics.list_topics_for_date(conn, ["site-1"], "2026-07-06")

    main_sql = conn.calls[0]["sql"]
    assert "t.time_range" in main_sql
    assert "t.participants" in main_sql
    assert "t.source" in main_sql
