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


# ---------------------------------------------------------------------------
# Cross-session status reconcile r0, Task 3: status_suggestions + pick_cards
# ---------------------------------------------------------------------------

class FakeTxConn(FakeConn):
    """FakeConn plus a no-op conn.transaction() -- the status/pick inserts each run in their
    own savepoint. A fake cannot show a psycopg aborted transaction; the real savepoint is
    proven in tests/integration/test_status_suggestions_writer.py."""

    def __init__(self):
        self.transactions = 0

    def transaction(self):
        self.transactions += 1
        return FakeConn()


ST_SITE = "0b5e7c1a-2f3d-4c6b-9a8e-7d6c5b4a3f21"   # site ids are compared as uuids


def _status(**overrides):
    s = dict(stable_id="sid-1", site_id=ST_SITE, suggested_status="done",
             quote="that one is done", segment_key="seg-1", offset_sec=12.0, run="final:x")
    s.update(overrides)
    return s


def _live(stable_id="sid-1", site_id=ST_SITE, status="open", company_id="co-1"):
    return {"stable_id": stable_id, "site_id": site_id, "status": status,
            "company_id": company_id}


def test_status_only_payload_opens_connection(monkeypatch):
    """suggestions/impacts/verdicts all absent, status_suggestions non-empty: the guard must
    still let it through to the DB."""
    opened = []

    def fake_get_connection(*a, **k):
        conn = FakeTxConn()
        opened.append(conn)
        return conn

    monkeypatch.setattr(sw, "get_connection", fake_get_connection)
    monkeypatch.setattr(sw.action_items, "get_live_by_stable_id", lambda conn, sid: _live(sid))
    inserted = []

    def fake_insert(conn, company_id, site_id, stable_id, status, quote, segment_key,
                    offset_sec, run):
        inserted.append((company_id, site_id, stable_id, status))
        return {"id": "s-1"}

    monkeypatch.setattr(sw.status_suggestions, "insert_suggestion", fake_insert)

    result = sw.lambda_handler({"status_suggestions": [_status()]}, None)

    assert len(opened) == 1
    assert inserted == [("co-1", ST_SITE, "sid-1", "done")]
    assert opened[0].transactions == 1, "each status insert runs in its own savepoint"
    assert result == {"written": 0, "status_written": 1,
                      "status_skipped": {"gone": 0, "moved_site": 0, "terminal": 0,
                                         "same": 0, "duplicate": 0, "failed": 0}}


def test_pick_only_payload_opens_connection(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeTxConn())
    monkeypatch.setattr(sw.action_items, "get_live_by_stable_id", lambda conn, sid: _live(sid))
    monkeypatch.setattr(sw.status_pick_cards, "insert_pick_card", lambda conn, *a: {"id": "p"})

    result = sw.lambda_handler({"pick_cards": [{
        "site_id": ST_SITE, "suggested_status": "done", "quote": "q", "segment_key": "k",
        "offset_sec": 1.0, "candidates": ["a", "b"], "run": "final:x"}]}, None)

    assert result == {"written": 0, "picks_written": 1,
                      "picks_skipped": _picks_skipped()}


def test_old_payload_response_unchanged(monkeypatch):
    """An old matcher's payload (and one with the new keys present but EMPTY) answers with
    exactly today's keys: no status_* / picks_* key appears, and neither new repo is
    touched."""
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion",
                        lambda conn, **kw: {"id": "sugg-1"})
    monkeypatch.setattr(sw.findings, "apply_impact",
                        lambda conn, finding_id, **kw: {"id": finding_id})

    def _boom(*a, **k):
        raise AssertionError("the new repos must not be touched for an old payload")

    monkeypatch.setattr(sw.action_items, "get_live_by_stable_id", _boom)
    monkeypatch.setattr(sw.status_suggestions, "insert_suggestion", _boom)
    monkeypatch.setattr(sw.status_pick_cards, "insert_pick_card", _boom)

    old = {"suggestions": [_suggestion()], "impacts": [_impact()]}
    assert sw.lambda_handler(old, None) == {"written": 1, "impacts_applied": 1}
    assert sw.lambda_handler(dict(old, status_suggestions=[], pick_cards=[]), None) == \
        {"written": 1, "impacts_applied": 1}
    assert sw.lambda_handler({"status_suggestions": [], "pick_cards": None}, None) == \
        {"written": 0}


def _picks_skipped(**over):
    d = {"gone": 0, "moved_site": 0, "terminal": 0, "same": 0, "too_many": 0,
         "duplicate": 0, "failed": 0}
    d.update(over)
    return d


def _card(candidates, site_id="site-1"):
    return {"site_id": site_id, "suggested_status": "done", "quote": "q",
            "segment_key": "k", "offset_sec": 1.0, "candidates": candidates, "run": "final:x"}


SITE = "6f1c2b9e-3d4a-4e5f-8a7b-1c2d3e4f5a6b"


def test_pick_card_with_more_than_four_survivors_is_skipped_not_truncated(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeTxConn())
    monkeypatch.setattr(sw.action_items, "get_live_by_stable_id",
                        lambda conn, sid: _live(sid, site_id=SITE))

    def _boom(*a, **k):
        raise AssertionError("a card with >4 survivors must never be inserted (or truncated)")

    monkeypatch.setattr(sw.status_pick_cards, "insert_pick_card", _boom)
    result = sw.lambda_handler({"pick_cards": [_card(list("abcde"), SITE)]}, None)
    assert result["picks_written"] == 0
    assert result["picks_skipped"] == _picks_skipped(too_many=1)


def test_site_ids_compare_canonically(monkeypatch):
    """An uppercase site uuid on the event matches the row's lowercase one, on both paths."""
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeTxConn())
    monkeypatch.setattr(sw.action_items, "get_live_by_stable_id",
                        lambda conn, sid: _live(sid, site_id=SITE))
    monkeypatch.setattr(sw.status_suggestions, "insert_suggestion", lambda conn, *a: {"id": 1})
    monkeypatch.setattr(sw.status_pick_cards, "insert_pick_card", lambda conn, *a: {"id": 2})
    result = sw.lambda_handler({
        "status_suggestions": [_status(site_id=SITE.upper())],
        "pick_cards": [_card(["a", "b"], SITE.upper())]}, None)
    assert result["status_written"] == 1
    assert result["picks_written"] == 1


def test_malformed_event_site_is_moved_site_not_an_exception(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeTxConn())
    monkeypatch.setattr(sw.action_items, "get_live_by_stable_id",
                        lambda conn, sid: _live(sid, site_id=SITE))

    def _boom(*a, **k):
        raise AssertionError("nothing may be inserted for a malformed site id")

    monkeypatch.setattr(sw.status_suggestions, "insert_suggestion", _boom)
    monkeypatch.setattr(sw.status_pick_cards, "insert_pick_card", _boom)
    result = sw.lambda_handler({
        "status_suggestions": [_status(site_id="not-a-uuid"), _status(site_id=None)],
        "pick_cards": [_card(["a", "b"], "not-a-uuid")]}, None)
    assert result["status_skipped"]["moved_site"] == 2
    assert result["picks_skipped"] == _picks_skipped(moved_site=1)


def test_pick_card_insert_failure_counts_failed(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeTxConn())
    monkeypatch.setattr(sw.action_items, "get_live_by_stable_id",
                        lambda conn, sid: _live(sid, site_id=SITE))

    def _raise(*a, **k):
        raise RuntimeError("check violation")

    monkeypatch.setattr(sw.status_pick_cards, "insert_pick_card", _raise)
    result = sw.lambda_handler({"pick_cards": [_card(["a", "b"], SITE)]}, None)
    assert result == {"written": 0, "picks_written": 0,
                      "picks_skipped": _picks_skipped(failed=1)}


def test_verdicts_only_payload_response_unchanged(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeTxConn())
    monkeypatch.setattr(sw, "_record_verdict", lambda conn, entry, cache: True)
    result = sw.lambda_handler({"verdicts": [{"kind": "programme_match"}]}, None)
    assert result == {"written": 0, "verdicts_recorded": 1, "verdicts_failed": 0}
