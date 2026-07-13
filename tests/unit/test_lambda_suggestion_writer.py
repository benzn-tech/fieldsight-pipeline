"""
Tests for src/lambda_suggestion_writer.py — Task 2 of the programme<->item
feedback plan (TDD):

  docs/superpowers/specs/2026-07-12-programme-item-feedback-design.md
  docs/superpowers/plans/2026-07-12-programme-item-feedback.md (Task 2)

In-VPC writer: the non-VPC matcher (Task 3, later) Lambda-invokes this with
a batch of suggestions to insert. Connection-mocking style mirrors
tests/unit/test_lambda_item_writer.py (FakeConn + monkeypatch on
get_connection and on the repository module).
"""
import datetime

import pytest

sw = pytest.importorskip("lambda_suggestion_writer", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _suggestion(**overrides):
    s = dict(
        site_id="site-1", task_id="T-004", topic_id="topic-1",
        topic_title="Floor Inserts", topic_summary="s", topic_user_id="u-1",
        report_date="2026-07-12", source_s3_key="extractions/x/2026-07-12/y.json",
        task_name="Floor Inserts", task_status_before="in_progress",
        task_progress_before=40, suggested_status="in_progress",
        suggested_progress=60, confidence=0.82, match_evidence={"cosine": 0.12},
    )
    s.update(overrides)
    return s


# ---------------------------------------------------------------------------
# Empty batch — guard before opening a DB connection
# ---------------------------------------------------------------------------

def test_empty_list_no_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("get_connection must not be called for an empty batch")

    monkeypatch.setattr(sw, "get_connection", _boom)

    result = sw.lambda_handler({"suggestions": []}, None)

    assert result == {"written": 0}


def test_missing_suggestions_key_no_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("get_connection must not be called for an empty batch")

    monkeypatch.setattr(sw, "get_connection", _boom)

    result = sw.lambda_handler({}, None)

    assert result == {"written": 0}


# ---------------------------------------------------------------------------
# Writes each suggestion, counts only non-None returns
# ---------------------------------------------------------------------------

def test_writes_each_and_counts(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    calls = []

    def fake_upsert(conn, **kwargs):
        calls.append(kwargs)
        return {"id": f"sugg-{len(calls)}"}

    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion", fake_upsert)

    suggestions = [_suggestion(task_id="T-001"), _suggestion(task_id="T-002")]
    result = sw.lambda_handler({"suggestions": suggestions}, None)

    assert result == {"written": 2}
    assert len(calls) == 2
    assert calls[0]["task_id"] == "T-001"
    assert calls[1]["task_id"] == "T-002"
    # every other field passed through unchanged
    assert calls[0]["site_id"] == "site-1"
    assert calls[0]["suggested_progress"] == 60


def test_none_return_not_counted(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    returns = [None, {"id": "sugg-2"}]

    def fake_upsert(conn, **kwargs):
        return returns.pop(0)

    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion", fake_upsert)

    suggestions = [_suggestion(task_id="T-001"), _suggestion(task_id="T-002")]
    result = sw.lambda_handler({"suggestions": suggestions}, None)

    assert result == {"written": 1}


# ---------------------------------------------------------------------------
# report_date coercion — JSON gives strings, the column is `date`
# ---------------------------------------------------------------------------

def test_report_date_coerced_to_date(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    captured = []

    def fake_upsert(conn, **kwargs):
        captured.append(kwargs)
        return {"id": "sugg-1"}

    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion", fake_upsert)

    suggestions = [_suggestion(report_date="2026-03-06")]
    sw.lambda_handler({"suggestions": suggestions}, None)

    assert captured[0]["report_date"] == datetime.date(2026, 3, 6)


def test_report_date_already_date_left_asis(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    captured = []

    def fake_upsert(conn, **kwargs):
        captured.append(kwargs)
        return {"id": "sugg-1"}

    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion", fake_upsert)

    d = datetime.date(2026, 3, 6)
    suggestions = [_suggestion(report_date=d)]
    sw.lambda_handler({"suggestions": suggestions}, None)

    assert captured[0]["report_date"] == d


# ---------------------------------------------------------------------------
# Per-row exceptions propagate (fail-closed; S3 event will retry) — must
# not be swallowed into a false success.
# ---------------------------------------------------------------------------

def test_per_row_exception_propagates(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())

    def fake_upsert(conn, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion", fake_upsert)

    with pytest.raises(RuntimeError, match="db exploded"):
        sw.lambda_handler({"suggestions": [_suggestion()]}, None)


# ---------------------------------------------------------------------------
# Impacts — programme-impact-link plan, Task 3: the writer also applies
# matcher-produced impact verdicts as finding-row UPDATEs via
# repositories.findings.apply_impact, inside the SAME single
# `with get_connection() as conn:` transaction as the suggestion writes
# above. A None return from apply_impact means the finding row vanished
# under supersession/re-extraction (D4/D5 of the plan) -- a NORMAL skip,
# not an error.
# ---------------------------------------------------------------------------

def _impact(**overrides):
    i = dict(
        finding_id="finding-1", task_id="T-004", impact_severity="major",
        impact_note="blocking pour", impact_task_name="Floor Inserts",
        impact_evidence={"cosine_survivor_ids": ["T-004"], "llm_confidence": 0.9},
    )
    i.update(overrides)
    return i


def test_impacts_applied_via_repo(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    calls = []

    def fake_apply_impact(conn, finding_id, **kwargs):
        calls.append((finding_id, kwargs))
        return {"id": finding_id}

    monkeypatch.setattr(sw.findings, "apply_impact", fake_apply_impact)

    impacts = [_impact(finding_id="f-1"), _impact(finding_id="f-2")]
    result = sw.lambda_handler({"suggestions": [], "impacts": impacts}, None)

    assert result == {"written": 0, "impacts_applied": 2}
    assert len(calls) == 2
    assert calls[0][0] == "f-1"
    assert calls[0][1]["task_id"] == "T-004"
    assert calls[0][1]["impact_severity"] == "major"
    assert calls[0][1]["impact_note"] == "blocking pour"
    assert calls[0][1]["impact_task_name"] == "Floor Inserts"
    assert calls[0][1]["impact_evidence"] == {"cosine_survivor_ids": ["T-004"], "llm_confidence": 0.9}


def test_impact_optional_fields_default(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    captured = []

    def fake_apply_impact(conn, finding_id, **kwargs):
        captured.append(kwargs)
        return {"id": finding_id}

    monkeypatch.setattr(sw.findings, "apply_impact", fake_apply_impact)

    minimal = {"finding_id": "f-1", "task_id": "T-004", "impact_severity": "minor"}
    sw.lambda_handler({"suggestions": [], "impacts": [minimal]}, None)

    assert captured[0]["impact_note"] is None
    assert captured[0]["impact_task_name"] is None
    assert captured[0]["impact_evidence"] == {}


def test_vanished_finding_counts_zero_not_error(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())

    def fake_apply_impact(conn, finding_id, **kwargs):
        return None  # row gone -- supersession/re-extraction raced in

    monkeypatch.setattr(sw.findings, "apply_impact", fake_apply_impact)

    result = sw.lambda_handler({"suggestions": [], "impacts": [_impact()]}, None)

    assert result == {"written": 0, "impacts_applied": 0}


def test_impact_per_row_exception_propagates(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())

    def fake_apply_impact(conn, finding_id, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(sw.findings, "apply_impact", fake_apply_impact)

    with pytest.raises(RuntimeError, match="db exploded"):
        sw.lambda_handler({"suggestions": [], "impacts": [_impact()]}, None)


def test_suggestions_only_payload_unchanged(monkeypatch):
    """No `impacts` key at all -- behaves EXACTLY as today: apply_impact is
    never called and the response has no impacts_applied key."""
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())

    def fake_upsert(conn, **kwargs):
        return {"id": "sugg-1"}

    def _boom_apply_impact(*a, **k):
        raise AssertionError("apply_impact must not be called with no impacts key")

    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion", fake_upsert)
    monkeypatch.setattr(sw.findings, "apply_impact", _boom_apply_impact)

    result = sw.lambda_handler({"suggestions": [_suggestion()]}, None)

    assert result == {"written": 1}
    assert "impacts_applied" not in result


def test_empty_both_never_opens_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("get_connection must not be called when both lists are empty")

    monkeypatch.setattr(sw, "get_connection", _boom)

    result = sw.lambda_handler({"suggestions": [], "impacts": []}, None)

    assert result == {"written": 0}


def test_impacts_only_payload_opens_connection(monkeypatch):
    """suggestions=[] but impacts non-empty must still open the connection
    -- the both-empty guard checks BOTH lists, not just suggestions."""
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())

    def fake_apply_impact(conn, finding_id, **kwargs):
        return {"id": finding_id}

    monkeypatch.setattr(sw.findings, "apply_impact", fake_apply_impact)

    result = sw.lambda_handler({"suggestions": [], "impacts": [_impact()]}, None)

    assert result == {"written": 0, "impacts_applied": 1}


def test_suggestions_and_impacts_same_transaction(monkeypatch):
    """Both loops run inside the SAME single get_connection() call -- no
    second connection is opened for the impacts loop."""
    connections = []

    def fake_get_connection(*a, **k):
        conn = FakeConn()
        connections.append(conn)
        return conn

    monkeypatch.setattr(sw, "get_connection", fake_get_connection)
    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion", lambda conn, **kw: {"id": "sugg-1"})
    monkeypatch.setattr(sw.findings, "apply_impact", lambda conn, finding_id, **kw: {"id": finding_id})

    result = sw.lambda_handler({"suggestions": [_suggestion()], "impacts": [_impact()]}, None)

    assert result == {"written": 1, "impacts_applied": 1}
    assert len(connections) == 1
