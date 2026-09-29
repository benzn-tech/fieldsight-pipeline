"""Track B Task 5: decisions and open questions become rows with stable ids, dual-written
alongside the existing `topics.decisions`/`topics.open_questions` jsonb.

Four groups of tests, at the granularity used elsewhere in this track:

  1. Repo SQL shape (topic_decisions.py / topic_questions.py) -- FakeConn/FakeCursor doubles,
     mirrors tests/unit/test_findings_repo.py exactly (no real Postgres; that lives in
     tests/integration/test_question_answered_survives.py, CLAUDE.md "Run the SQL against a
     real database before trusting it").
  2. The writer (lambda_item_writer.write_extraction_items) inserts both tables' rows in the
     SAME transaction right after findings.insert_findings, mirrors
     tests/unit/test_lambda_item_writer.py's own `test_writes_findings_rows_per_topic`.
  3. Blank-dropping parity -- the row insert must drop EXACTLY the entries
     upsert_topic's own `decisions=`/`open_questions=` kwargs (lambda_item_writer.py, the
     jsonb path) drop, for the SAME raw extractor input in ONE real (unmocked) write.
  4. The org-api handler `patch_question` (PATCH /api/org/questions/{stable_id}) via
     `org.lambda_handler(make_event(...))`, mirroring test_lambda_org_api.py's own
     `patch_action_item` test block: role gate, bad status, the four refusal shapes (cross-
     company / non-member site / superseded-only / unknown stable_id -- all 404, per the
     task brief's explicit "match action item's checks, but 404 not 403"), content_edits
     written, and the success response shape.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

import psycopg  # noqa: E402  (after sys.path insert, matches sibling tests)
from psycopg.rows import dict_row  # noqa: E402

from repositories import topic_decisions as td_repo  # noqa: E402
from repositories import topic_questions as tq_repo  # noqa: E402

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

from tests.unit.test_lambda_org_api import (  # noqa: E402
    CALLER, OTHER_SITE_ID, SITE_ID, TxnConn, body_of, make_event,
)
from tests.unit.test_lambda_org_api import wired  # noqa: E402,F401  (pytest fixture)

iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")
from tests.unit.test_lambda_item_writer import (  # noqa: E402
    EXTRACTION_KEY, FakeConn as IWFakeConn, FakeS3, make_extraction,
)
from tests.unit.test_lambda_item_writer import wired as iw_wired  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Shared FakeConn/FakeCursor (mirrors test_findings_repo.py verbatim, plus a
# rollback() hook for get_live_by_stable_id's malformed-uuid path)
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        result = self.conn._pop_result()
        if isinstance(result, Exception):
            raise result
        self._rows = result
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """`results` is consumed in call order: one entry per execute() call -- a list of row
    dicts (fetchall), or an Exception instance to RAISE (get_live_by_stable_id's malformed-
    uuid path)."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])
        self.rolled_back = False

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        assert row_factory is dict_row
        return FakeCursor(self)

    def execute(self, sql, params=None):
        # carry_identity's UPDATE uses conn.execute(...) directly (bare, not
        # conn.cursor().execute(...)) -- same convention findings.carry_identity uses.
        self.calls.append({"sql": sql, "params": params})
        return None

    def rollback(self):
        self.rolled_back = True


# ---------------------------------------------------------------------------
# topic_decisions -- insert_decisions / list_for_topics / carry-forward
# ---------------------------------------------------------------------------

def _decision_row(**over):
    row = {"id": "d-1", "topic_id": "t-1", "site_id": "s-1", "stable_id": "stable-d1",
           "carried_from": None, "decision": "Use precast panels", "rationale": "Faster",
           "decided_by": "Ben", "audience": "internal", "created_at": "2026-09-24T00:00:00Z"}
    row.update(over)
    return row


def test_insert_decisions_empty_list_returns_empty_no_query():
    conn = FakeConn()
    assert td_repo.insert_decisions(conn, "t-1", "s-1", []) == []
    assert conn.calls == []


def test_insert_decisions_dict_entry_keeps_rationale_and_decided_by():
    conn = FakeConn(results=[[_decision_row()]])
    result = td_repo.insert_decisions(conn, "t-1", "s-1", [
        {"decision": "Use precast panels", "rationale": "Faster", "decided_by": "Ben"},
    ])
    assert result == [_decision_row()]
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "INSERT INTO topic_decisions" in sql and "RETURNING" in sql
    assert params == ("t-1", "s-1", "Use precast panels", "Faster", "Ben")


def test_insert_decisions_tolerates_plain_string_entry():
    conn = FakeConn(results=[[_decision_row(rationale=None, decided_by=None)]])
    td_repo.insert_decisions(conn, "t-1", "s-1", ["Use precast panels"])
    params = conn.calls[0]["params"]
    assert params == ("t-1", "s-1", "Use precast panels", None, None)


def test_insert_decisions_drops_blank_dict_and_blank_string_entries():
    """Same drop rule as upsert_topic's `decisions=` kwarg (lambda_item_writer.py ~1152):
    a dict with a falsy `decision`, or a falsy bare string, is skipped -- it would render
    as a bullet with nothing in it."""
    conn = FakeConn(results=[[_decision_row()]])
    result = td_repo.insert_decisions(conn, "t-1", "s-1", [
        {"decision": ""},                    # blank dict -- dropped
        {"rationale": "no decision key"},    # missing key -- dropped
        "",                                  # blank string -- dropped
        None,                                # falsy -- dropped
        {"decision": "Use precast panels", "rationale": "Faster", "decided_by": "Ben"},
    ])
    assert len(conn.calls) == 1              # only the one real entry issued a query
    assert result == [_decision_row()]


def test_decisions_list_for_topics_batches():
    row = _decision_row()
    conn = FakeConn(results=[[row]])
    rows = td_repo.list_for_topics(conn, ["t-1", "t-2"])
    assert rows == [row]
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "topic_id = ANY(%s)" in sql
    assert params == (["t-1", "t-2"],)


def test_decisions_list_for_carry_forward_empty_topic_ids_short_circuits():
    conn = FakeConn()
    assert td_repo.list_for_carry_forward(conn, [], "s-1") == []
    assert conn.calls == []


def test_decisions_list_for_carry_forward_scopes_to_site_and_computes_human_touched():
    conn = FakeConn(results=[[{"id": "d-1", "topic_id": "t-1", "text": "x",
                              "stable_id": "s1", "audience": "owner",
                              "human_touched": True}]])
    rows = td_repo.list_for_carry_forward(conn, ["t-1"], "site-1")
    assert rows[0]["human_touched"] is True
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "audience <> 'internal'" in sql
    assert "site_id = %s" in sql
    assert params == (["t-1"], "site-1")


def test_decisions_carry_identity_human_touched_copies_audience():
    conn = FakeConn()
    old_row = {"id": "old-d1", "stable_id": "stable-d1", "audience": "owner",
              "human_touched": True}
    td_repo.carry_identity(conn, "new-d1", old_row)
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "audience=%s" in sql
    assert params == ("stable-d1", "old-d1", "owner", "new-d1")


def test_decisions_carry_identity_untouched_only_moves_identity():
    conn = FakeConn()
    old_row = {"id": "old-d1", "stable_id": "stable-d1", "audience": "internal",
              "human_touched": False}
    td_repo.carry_identity(conn, "new-d1", old_row)
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "audience" not in sql
    assert params == ("stable-d1", "old-d1", "new-d1")


# ---------------------------------------------------------------------------
# topic_questions -- insert_questions / list_for_topics / carry-forward / PATCH plumbing
# ---------------------------------------------------------------------------

def _question_row(**over):
    row = {"id": "q-1", "topic_id": "t-1", "site_id": "s-1", "stable_id": "stable-q1",
           "carried_from": None, "question": "When does the pour start?",
           "status": "open", "answered_by": None, "answered_at": None,
           "audience": "internal", "created_at": "2026-09-24T00:00:00Z"}
    row.update(over)
    return row


def test_insert_questions_empty_list_returns_empty_no_query():
    conn = FakeConn()
    assert tq_repo.insert_questions(conn, "t-1", "s-1", []) == []
    assert conn.calls == []


def test_insert_questions_dict_and_string_entries_blank_dropped():
    """Same drop rule as upsert_topic's `open_questions=` kwarg (lambda_item_writer.py
    ~1141): dict-with-blank-question, missing key, blank string and falsy entries are
    skipped."""
    conn = FakeConn(results=[[_question_row()]])
    result = tq_repo.insert_questions(conn, "t-1", "s-1", [
        {"question": ""},
        {"not_question": "x"},
        "",
        None,
        {"question": "When does the pour start?"},
    ])
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "INSERT INTO topic_questions" in sql and "RETURNING" in sql
    assert params == ("t-1", "s-1", "When does the pour start?")
    assert result == [_question_row()]


def test_insert_questions_tolerates_plain_string_entry():
    conn = FakeConn(results=[[_question_row()]])
    tq_repo.insert_questions(conn, "t-1", "s-1", ["When does the pour start?"])
    params = conn.calls[0]["params"]
    assert params == ("t-1", "s-1", "When does the pour start?")


def test_questions_list_for_topics_batches():
    row = _question_row()
    conn = FakeConn(results=[[row]])
    rows = tq_repo.list_for_topics(conn, ["t-1", "t-2"])
    assert rows == [row]
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "topic_id = ANY(%s)" in sql
    assert params == (["t-1", "t-2"],)


def test_questions_list_for_carry_forward_human_touched_status_or_audience():
    conn = FakeConn(results=[[{"human_touched": True}]])
    tq_repo.list_for_carry_forward(conn, ["t-1"], "site-1")
    sql = conn.calls[0]["sql"]
    assert "status <> 'open' OR audience <> 'internal'" in sql


def test_questions_list_for_carry_forward_empty_topic_ids_short_circuits():
    conn = FakeConn()
    assert tq_repo.list_for_carry_forward(conn, [], "s-1") == []
    assert conn.calls == []


def test_questions_carry_identity_human_touched_copies_answer_fields():
    conn = FakeConn()
    old_row = {"id": "old-q1", "stable_id": "stable-q1", "status": "answered",
              "answered_by": "u-1", "answered_at": "2026-09-24T00:00:00Z",
              "audience": "internal", "human_touched": True}
    tq_repo.carry_identity(conn, "new-q1", old_row)
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "status=%s" in sql and "answered_by=%s" in sql and "answered_at=%s" in sql
    assert params == ("stable-q1", "old-q1", "answered", "u-1",
                      "2026-09-24T00:00:00Z", "internal", "new-q1")


def test_questions_carry_identity_untouched_only_moves_identity():
    conn = FakeConn()
    old_row = {"id": "old-q1", "stable_id": "stable-q1", "human_touched": False}
    tq_repo.carry_identity(conn, "new-q1", old_row)
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "status" not in sql
    assert params == ("stable-q1", "old-q1", "new-q1")


def test_get_live_by_stable_id_sql_shape():
    conn = FakeConn(results=[[_question_row(company_id="co-1")]])
    row = tq_repo.get_live_by_stable_id(conn, "stable-q1")
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "JOIN topics t ON t.id = q.topic_id" in sql
    assert "JOIN sites s ON s.id = q.site_id" in sql
    assert "superseded_at IS NULL" in sql               # visible_topics_predicate's live arm
    assert "ORDER BY q.created_at DESC LIMIT 1" in sql
    assert params == ("stable-q1",)
    assert row["company_id"] == "co-1"


def test_get_live_by_stable_id_malformed_uuid_rolls_back_and_returns_none():
    conn = FakeConn(results=[psycopg.Error("invalid input syntax for type uuid")])
    result = tq_repo.get_live_by_stable_id(conn, "not-a-uuid")
    assert result is None
    assert conn.rolled_back is True


def test_set_status_open_clears_answered_fields():
    conn = FakeConn(results=[[_question_row(status="open")]])
    tq_repo.set_status(conn, "q-1", "open", "u-1")
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "answered_at=NULL" in sql
    assert params == ("open", None, "q-1")     # answered_by forced NULL regardless of input


def test_set_status_answered_uses_database_now():
    conn = FakeConn(results=[[_question_row(status="answered", answered_by="u-1")]])
    tq_repo.set_status(conn, "q-1", "answered", "u-1")
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "answered_at=now()" in sql
    assert params == ("answered", "u-1", "q-1")


# ---------------------------------------------------------------------------
# The writer: rows inserted in the same transaction, right after findings
# ---------------------------------------------------------------------------

def test_writer_inserts_decisions_and_questions_after_findings_same_transaction(iw_wired):
    conn_holder = {}
    order = []
    iw_wired.setattr(iw, "get_connection",
                     lambda *a, **k: conn_holder.setdefault("conn", IWFakeConn()))
    iw_wired.setattr(
        iw, "_s3_client",
        FakeS3({EXTRACTION_KEY: json.dumps(
            make_extraction(topics=[{
                **make_extraction()["topics"][0],
                "decisions": [{"decision": "Use precast panels"}],
                "questions": [{"question": "When does the pour start?"}],
            }]))}),
    )
    iw_wired.setattr(iw.findings, "insert_findings",
                     lambda conn, tid, sid, fl: order.append("findings") or [])
    iw_wired.setattr(
        iw.topic_decisions, "insert_decisions",
        lambda conn, tid, sid, dl: order.append(("decisions", conn, tid, sid, dl)) or [])
    iw_wired.setattr(
        iw.topic_questions, "insert_questions",
        lambda conn, tid, sid, ql: order.append(("questions", conn, tid, sid, ql)) or [])

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    kinds = [o if isinstance(o, str) else o[0] for o in order]
    assert kinds == ["findings", "decisions", "questions"]   # SAME transaction, right after

    _, conn, tid, sid, dl = order[1]
    assert conn is conn_holder["conn"]
    assert tid == "topic-uuid-0" and sid == "site-1"
    assert dl == [{"decision": "Use precast panels"}]

    _, conn2, tid2, sid2, ql = order[2]
    assert conn2 is conn_holder["conn"]
    assert ql == [{"question": "When does the pour start?"}]


def test_writer_passes_empty_lists_when_extraction_has_no_decisions_or_questions(iw_wired):
    # make_extraction()'s default topic has neither key -- t.get(...) or [] must reach the
    # repo as [], never crash on a missing key.
    calls = {}
    iw_wired.setattr(
        iw.topic_decisions, "insert_decisions",
        lambda conn, tid, sid, dl: calls.__setitem__("decisions", dl) or [])
    iw_wired.setattr(
        iw.topic_questions, "insert_questions",
        lambda conn, tid, sid, ql: calls.__setitem__("questions", ql) or [])

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert calls == {"decisions": [], "questions": []}


# ---------------------------------------------------------------------------
# Blank-dropping parity: the SAME raw mixed-entry list, run through the REAL
# jsonb path (upsert_topic's kwargs) and the REAL row-insert path in ONE call.
# ---------------------------------------------------------------------------

class _RecordingCur:
    """Answers None/[] to every query EXCEPT the topic_decisions/topic_questions INSERTs
    this test actually cares about -- a generic truthy fetchone() here would make
    redactions.is_source_deleted (a different `conn.cursor().execute().fetchone()` call
    earlier in the same write path) read every row as a tombstone hit and skip the write
    entirely."""

    def __init__(self, conn):
        self.conn = conn
        self._sql = ""

    def execute(self, sql, params=None):
        self._sql = sql
        self.conn.cursor_sql.append((sql, params))
        return self

    def fetchone(self):
        if "INSERT INTO topic_decisions" in self._sql or "INSERT INTO topic_questions" in self._sql:
            return {"id": f"row-{len(self.conn.cursor_sql)}"}
        return None

    def fetchall(self):
        return []


class _RecordingConn(IWFakeConn):
    """IWFakeConn (bare `.execute()`, `.transaction()`) plus a `.cursor()` that records every
    statement -- just enough for the REAL topic_decisions.insert_decisions /
    topic_questions.insert_questions to run against, without a real Postgres."""

    def __init__(self):
        super().__init__()
        self.cursor_sql = []

    def cursor(self, row_factory=None):
        return _RecordingCur(self)


MIXED_DECISIONS = [
    {"decision": ""},                     # blank dict -- dropped both paths
    {"rationale": "no decision key"},     # missing key -- dropped both paths
    "",                                   # blank string -- dropped both paths
    None,                                 # falsy -- dropped both paths
    {"decision": "Use precast panels", "rationale": "Faster", "decided_by": "Ben"},
    "Pour on Friday",                     # valid plain string
]
MIXED_QUESTIONS = [
    {"question": ""},
    {"not_question": "x"},
    "",
    None,
    {"question": "When does the pour start?"},
    "Who is the site contact?",
]


def test_blank_dropping_parity_with_the_jsonb_path(iw_wired):
    conn_holder = {}
    iw_wired.setattr(iw, "get_connection",
                     lambda *a, **k: conn_holder.setdefault("conn", _RecordingConn()))
    iw_wired.setattr(
        iw, "_s3_client",
        FakeS3({EXTRACTION_KEY: json.dumps(
            make_extraction(topics=[{
                **make_extraction()["topics"][0],
                "decisions": MIXED_DECISIONS,
                "questions": MIXED_QUESTIONS,
            }]))}),
    )
    upsert_calls = []
    iw_wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            upsert_calls.append(kw) or {"id": "topic-uuid-0"},
    )
    iw_wired.setattr(iw.findings, "insert_findings", lambda *a, **k: [])

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    jsonb_decisions = upsert_calls[0]["decisions"] or []
    jsonb_questions = upsert_calls[0]["open_questions"] or []
    assert len(jsonb_decisions) == 2       # the dict entry + the plain string, kept whole
    assert len(jsonb_questions) == 2       # the dict entry's text + the plain string

    conn = conn_holder["conn"]
    decision_inserts = [c for c in conn.cursor_sql if "INSERT INTO topic_decisions" in c[0]]
    question_inserts = [c for c in conn.cursor_sql if "INSERT INTO topic_questions" in c[0]]
    assert len(decision_inserts) == len(jsonb_decisions) == 2
    assert len(question_inserts) == len(jsonb_questions) == 2


# ---------------------------------------------------------------------------
# org-api: PATCH /api/org/questions/{stable_id}
# ---------------------------------------------------------------------------

QUESTION_ROW = {
    "id": "q-1", "topic_id": "t-1", "site_id": SITE_ID, "stable_id": "stable-abc",
    "carried_from": None, "question": "When is the pour?", "status": "open",
    "answered_by": None, "answered_at": None, "audience": "internal",
    "created_at": "2026-09-24T00:00:00Z", "company_id": CALLER["company_id"],
}


def _wire_question(wired, row=None):
    row = dict(row or QUESTION_ROW)
    wired.setattr(org.topic_questions, "get_live_by_stable_id",
                  lambda conn, sid: dict(row) if sid == row["stable_id"] else None)
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {row["site_id"]})
    seen = {"audits": [], "set_status_calls": []}

    def fake_set_status(conn, qid, status, answered_by):
        seen["set_status_calls"].append((qid, status, answered_by))
        return {**row, "status": status, "answered_by": answered_by,
                "answered_at": None if status == "open" else "2026-09-30T00:00:00Z"}

    wired.setattr(org.topic_questions, "set_status", fake_set_status)
    wired.setattr(org.content_edits, "append_content_edit",
                  lambda conn, *a: seen["audits"].append(a) or {"id": "e-1"})
    return seen, row


def test_patch_question_role_gate_denies_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    _wire_question(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "answered"}), None)
    assert res["statusCode"] == 403


def test_patch_question_bad_status_value_400(wired):
    _wire_question(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "closed"}), None)
    assert res["statusCode"] == 400


def test_patch_question_missing_status_400(wired):
    _wire_question(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc", body={}), None)
    assert res["statusCode"] == 400


def test_patch_question_malformed_body_400(wired):
    _wire_question(wired)
    event = make_event("PATCH", "/api/org/questions/stable-abc")
    event["body"] = "not json"
    res = org.lambda_handler(event, None)
    assert res["statusCode"] == 400


def test_patch_question_answered_success_shape(wired):
    seen, row = _wire_question(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "answered"}), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["status"] == "answered"
    assert b["stable_id"] == "stable-abc"
    assert seen["set_status_calls"] == [(row["id"], "answered", CALLER["id"])]


def test_patch_question_reopen_clears_answered_by(wired):
    seen, row = _wire_question(
        wired, row={**QUESTION_ROW, "status": "answered", "answered_by": "u-2"})
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "open"}), None)
    assert res["statusCode"] == 200
    assert seen["set_status_calls"] == [(row["id"], "open", None)]


def test_patch_question_writes_content_edits_row(wired):
    seen, row = _wire_question(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "dropped"}), None)
    assert res["statusCode"] == 200
    assert len(seen["audits"]) == 1
    company_id, table, row_id, field, before, after, actor, actor_role = seen["audits"][0]
    assert (table, row_id, field) == ("topic_questions", row["id"], "status")
    assert (before, after) == ("open", "dropped")
    assert company_id == row["company_id"]
    assert actor == CALLER["id"] and actor_role == CALLER["global_role"]


def test_patch_question_unknown_stable_id_404(wired):
    wired.setattr(org.topic_questions, "get_live_by_stable_id", lambda conn, sid: None)
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/ghost",
                                        body={"status": "answered"}), None)
    assert res["statusCode"] == 404


def test_patch_question_superseded_only_stable_id_404(wired):
    """A stable_id that exists only on a SUPERSEDED pass has no row in
    get_live_by_stable_id's result -- visible_topics_predicate excludes it at the SQL layer
    (proven by test_get_live_by_stable_id_sql_shape above); from the handler's point of
    view this is indistinguishable from an unknown stable_id, and both must 404. Real
    behaviour against Postgres, across an actual re-extraction, is
    tests/integration/test_question_answered_survives.py."""
    wired.setattr(org.topic_questions, "get_live_by_stable_id", lambda conn, sid: None)
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-superseded",
                                        body={"status": "answered"}), None)
    assert res["statusCode"] == 404


def test_patch_question_cross_company_404(wired):
    _wire_question(wired, row={**QUESTION_ROW, "company_id": "OTHER-CO"})
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "answered"}), None)
    assert res["statusCode"] == 404


def test_patch_question_non_member_site_404_not_403(wired):
    """Task 5 brief: same authorization CHECKS as patch_action_item (company scope, then
    site reach), but a caller who cannot reach this row's site gets 404 -- not the 403
    patch_action_item returns for the same fact about an action item it fetched by a fixed
    id. See patch_question's own docstring for why."""
    _wire_question(wired, row={**QUESTION_ROW, "site_id": OTHER_SITE_ID})
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})  # not OTHER_SITE_ID
    called = []
    wired.setattr(org.topic_questions, "set_status", lambda *a, **k: called.append(1))
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "answered"}), None)
    assert res["statusCode"] == 404
    assert called == []


def test_patch_question_platform_admin_answers_cross_company(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "company_id": "c-platform",
                                     "global_role": "platform_admin"})
    seen, row = _wire_question(wired, row={**QUESTION_ROW, "company_id": "c-south"})
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "answered"}), None)
    assert res["statusCode"] == 200
    assert seen["set_status_calls"] == [(row["id"], "answered", CALLER["id"])]


def test_patch_question_audit_failure_rolls_back_the_update(wired):
    conn = TxnConn()
    wired.setattr(org, "get_connection", lambda *a, **k: conn)
    _wire_question(wired)

    def boom(*a, **k):
        raise RuntimeError("content_edits insert failed")

    wired.setattr(org.content_edits, "append_content_edit", boom)
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "answered"}), None)
    assert res["statusCode"] == 500
    assert conn.txn_log == ["enter", "rollback"]


def test_patch_question_vanished_row_rolls_back_and_writes_no_audit(wired):
    conn = TxnConn()
    wired.setattr(org, "get_connection", lambda *a, **k: conn)
    seen, row = _wire_question(wired)
    wired.setattr(org.topic_questions, "set_status", lambda *a, **k: None)
    res = org.lambda_handler(make_event("PATCH", "/api/org/questions/stable-abc",
                                        body={"status": "answered"}), None)
    assert res["statusCode"] == 404
    assert conn.txn_log == ["enter", "rollback"]
    assert seen["audits"] == []
