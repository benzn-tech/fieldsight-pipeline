"""
Tests for src/lambda_suggestion_writer.py's `verdicts` handling -- Track B
Task 6a: EVERY gated matcher verdict (accepted AND rejected) becomes a
decision_records row, inserted in the SAME transaction as the
suggestions/impacts writes that ride alongside it.

Style mirrors tests/unit/test_lambda_suggestion_writer.py (FakeConn +
monkeypatch on get_connection and on the repository modules).
"""
import pytest

sw = pytest.importorskip("lambda_suggestion_writer", reason="requires psycopg (installed in CI)")


class _FakeTransaction:
    """psycopg's nested transaction (a SAVEPOINT when one is already open).
    Modelled only as far as _record_verdict relies on it: enter, and let an
    exception propagate so _record_verdict's own try/except sees it -- same
    minimal double as tests/unit/test_lambda_item_writer.py's FakeConn."""
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False          # never swallow -- the caller decides


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def transaction(self):
        return _FakeTransaction()


def _verdict(**overrides):
    v = dict(
        kind="programme_match", subject_type="topic", subject="topic-1",
        object_ref="T-1", site_id="site-1", provider="anthropic",
        model="claude-sonnet-4-6", model_version=None,
        question_set="programme_match:abc123", input_key="match_requests/site-1/x.json",
        input_hash="deadbeef", output={"task_id": "T-1", "confidence": 0.9},
        score=0.9, threshold=0.7, auto_outcome="accepted",
    )
    v.update(overrides)
    return v


def _wire_sites(monkeypatch, company_id="co-1"):
    monkeypatch.setattr(sw.sites, "get_site",
                        lambda conn, site_id: {"company_id": company_id} if site_id else None)


# ---------------------------------------------------------------------------
# One decision_records row per verdict, whatever its auto_outcome
# ---------------------------------------------------------------------------

def test_rejected_verdict_still_gets_a_decision_record(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    _wire_sites(monkeypatch)
    captured = []
    monkeypatch.setattr(sw.decision_records, "insert",
                        lambda conn, **kw: captured.append(kw) or {"id": "dr-1"})

    verdict = _verdict(auto_outcome="rejected", score=0.5)
    result = sw.lambda_handler({"verdicts": [verdict]}, None)

    assert result["verdicts_recorded"] == 1
    assert len(captured) == 1
    assert captured[0]["auto_outcome"] == "rejected"
    assert captured[0]["score"] == 0.5
    assert captured[0]["company_id"] == "co-1"
    assert captured[0]["subject_stable_id"] == "topic-1"
    assert captured[0]["object_ref"] == "T-1"


def test_accepted_verdict_gets_a_decision_record_too(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    _wire_sites(monkeypatch)
    captured = []
    monkeypatch.setattr(sw.decision_records, "insert",
                        lambda conn, **kw: captured.append(kw) or {"id": "dr-1"})

    result = sw.lambda_handler({"verdicts": [_verdict(auto_outcome="accepted")]}, None)

    assert result["verdicts_recorded"] == 1
    assert captured[0]["auto_outcome"] == "accepted"


def test_event_without_verdicts_key_works_exactly_as_before(monkeypatch):
    """Old matcher deploy mid-rollout: no `verdicts` key at all. Must not
    touch decision_records.insert, and the response carries no
    verdicts_recorded key -- pinning `lambda_suggestion_writer`'s backward
    compatibility promise."""
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())

    def _boom(*a, **k):
        raise AssertionError("decision_records.insert must not be called")

    monkeypatch.setattr(sw.decision_records, "insert", _boom)
    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion",
                        lambda conn, **kw: {"id": "sugg-1"})

    suggestion = dict(
        site_id="site-1", task_id="T-004", topic_id="topic-1",
        topic_title="Floor Inserts", topic_summary="s", topic_user_id="u-1",
        report_date="2026-07-12", source_s3_key="extractions/x/2026-07-12/y.json",
        task_name="Floor Inserts", task_status_before="in_progress",
        task_progress_before=40, suggested_status="in_progress",
        suggested_progress=60, confidence=0.82, match_evidence={"cosine": 0.12},
    )
    result = sw.lambda_handler({"suggestions": [suggestion]}, None)

    assert result == {"written": 1}
    assert "verdicts_recorded" not in result


def test_verdicts_only_payload_opens_connection(monkeypatch):
    """A verdicts-only event -- every verdict from the matcher's pass was
    rejected, so `suggestions`/`impacts` are both empty -- must still open
    the connection and write the records."""
    opened = []
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: opened.append(1) or FakeConn())
    _wire_sites(monkeypatch)
    monkeypatch.setattr(sw.decision_records, "insert", lambda conn, **kw: {"id": "dr-1"})

    result = sw.lambda_handler({"verdicts": [_verdict(auto_outcome="rejected")]}, None)

    assert opened == [1]
    assert result["verdicts_recorded"] == 1


# ---------------------------------------------------------------------------
# Same-transaction proof -- one get_connection() call covers suggestions,
# impacts AND verdicts.
# ---------------------------------------------------------------------------

def test_suggestions_impacts_and_verdicts_share_one_connection(monkeypatch):
    connections = []

    def fake_get_connection(*a, **k):
        conn = FakeConn()
        connections.append(conn)
        return conn

    monkeypatch.setattr(sw, "get_connection", fake_get_connection)
    _wire_sites(monkeypatch)
    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion",
                        lambda conn, **kw: {"id": "sugg-1"})
    monkeypatch.setattr(sw.findings, "apply_impact",
                        lambda conn, finding_id, **kw: {"id": finding_id})
    seen_conns = []
    monkeypatch.setattr(sw.decision_records, "insert",
                        lambda conn, **kw: seen_conns.append(conn) or {"id": "dr-1"})

    suggestion = dict(
        site_id="site-1", task_id="T-004", topic_id="topic-1",
        topic_title="Floor Inserts", topic_summary="s", topic_user_id="u-1",
        report_date="2026-07-12", source_s3_key="extractions/x/2026-07-12/y.json",
        task_name="Floor Inserts", task_status_before="in_progress",
        task_progress_before=40, suggested_status="in_progress",
        suggested_progress=60, confidence=0.82, match_evidence={"cosine": 0.12},
    )
    impact = dict(finding_id="F-1", task_id="T-1", impact_severity="major",
                  impact_note="n", impact_task_name="Floor Inserts",
                  impact_evidence={"cosine": 0.1})

    result = sw.lambda_handler(
        {"suggestions": [suggestion], "impacts": [impact], "verdicts": [_verdict()]}, None)

    assert result == {"written": 1, "impacts_applied": 1, "verdicts_recorded": 1, "verdicts_failed": 0}
    # ONE connection for the whole batch -- proves the verdicts loop rides
    # inside the SAME `with get_connection()` block as the other two.
    assert len(connections) == 1
    assert seen_conns == [connections[0]]


# ---------------------------------------------------------------------------
# Ruling R14: a decision_records.insert exception must never take the
# suggestions/impacts it shares a transaction with down too -- the INSERT
# runs inside its own SAVEPOINT (same posture as item-writer's
# _record_work_class_decision, Ruling R10).
# ---------------------------------------------------------------------------

def test_decision_records_insert_exception_does_not_lose_suggestions_or_impacts(
        monkeypatch, caplog):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    _wire_sites(monkeypatch)
    monkeypatch.setattr(sw.programme_suggestions, "upsert_suggestion",
                        lambda conn, **kw: {"id": "sugg-1"})
    monkeypatch.setattr(sw.findings, "apply_impact",
                        lambda conn, finding_id, **kw: {"id": finding_id})

    def boom(conn, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(sw.decision_records, "insert", boom)

    suggestion = dict(
        site_id="site-1", task_id="T-004", topic_id="topic-1",
        topic_title="Floor Inserts", topic_summary="s", topic_user_id="u-1",
        report_date="2026-07-12", source_s3_key="extractions/x/2026-07-12/y.json",
        task_name="Floor Inserts", task_status_before="in_progress",
        task_progress_before=40, suggested_status="in_progress",
        suggested_progress=60, confidence=0.82, match_evidence={"cosine": 0.12},
    )
    impact = dict(finding_id="F-1", task_id="T-1", impact_severity="major",
                  impact_note="n", impact_task_name="Floor Inserts",
                  impact_evidence={"cosine": 0.1})

    with caplog.at_level("WARNING"):
        result = sw.lambda_handler(
            {"suggestions": [suggestion], "impacts": [impact], "verdicts": [_verdict()]}, None)

    # The suggestion and the impact still landed -- the verdict-record
    # failure did NOT abort the transaction they share.
    assert result["written"] == 1
    assert result["impacts_applied"] == 1
    assert result["verdicts_recorded"] == 0
    assert result["verdicts_failed"] == 1
    assert "decision record insert failed" in caplog.text


def test_multiple_verdicts_one_bad_does_not_block_the_others(monkeypatch):
    """The SAVEPOINT is per-verdict, not per-batch: one bad verdict entry
    must not also lose every OTHER verdict in the same call."""
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    _wire_sites(monkeypatch)
    calls = []

    def maybe_boom(conn, **kw):
        calls.append(kw)
        if kw["object_ref"] == "T-bad":
            raise RuntimeError("boom")
        return {"id": "dr-ok"}

    monkeypatch.setattr(sw.decision_records, "insert", maybe_boom)

    verdicts = [_verdict(object_ref="T-1"), _verdict(object_ref="T-bad"),
               _verdict(object_ref="T-2")]
    result = sw.lambda_handler({"verdicts": verdicts}, None)

    assert result["verdicts_recorded"] == 2
    assert result["verdicts_failed"] == 1
    assert len(calls) == 3  # all three were attempted


# ---------------------------------------------------------------------------
# programme_impact verdicts: subject is a findings ROW id, resolved to
# stable_id here in SQL (the matcher cannot do it -- non-VPC, BUG-36).
# ---------------------------------------------------------------------------

def test_impact_verdict_subject_resolved_from_row_id_to_stable_id(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    _wire_sites(monkeypatch)
    monkeypatch.setattr(sw.findings, "get_stable_id",
                        lambda conn, finding_id: "stable-xyz" if finding_id == "F-1" else None)
    captured = []
    monkeypatch.setattr(sw.decision_records, "insert",
                        lambda conn, **kw: captured.append(kw) or {"id": "dr-1"})

    verdict = _verdict(kind="programme_impact", subject_type="finding",
                       subject="F-1", subject_is_row_id=True)
    result = sw.lambda_handler({"verdicts": [verdict]}, None)

    assert result["verdicts_recorded"] == 1
    assert captured[0]["subject_stable_id"] == "stable-xyz"
    # subject_is_row_id itself is not a decision_records column -- must not
    # be forwarded to the repo insert.
    assert "subject_is_row_id" not in captured[0]


def test_impact_verdict_skipped_when_finding_row_vanished(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    _wire_sites(monkeypatch)
    monkeypatch.setattr(sw.findings, "get_stable_id", lambda conn, finding_id: None)
    monkeypatch.setattr(sw.decision_records, "insert",
                        lambda conn, **kw: (_ for _ in ()).throw(
                            AssertionError("must not insert for a vanished finding")))

    verdict = _verdict(kind="programme_impact", subject_type="finding",
                       subject="F-gone", subject_is_row_id=True)
    result = sw.lambda_handler({"verdicts": [verdict]}, None)

    assert result["verdicts_recorded"] == 0


def test_verdict_skipped_when_site_has_no_company(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(sw.sites, "get_site", lambda conn, site_id: None)
    monkeypatch.setattr(sw.decision_records, "insert",
                        lambda conn, **kw: (_ for _ in ()).throw(
                            AssertionError("must not insert without a company_id")))

    result = sw.lambda_handler({"verdicts": [_verdict()]}, None)

    assert result["verdicts_recorded"] == 0


def test_company_id_lookup_cached_per_site(monkeypatch):
    monkeypatch.setattr(sw, "get_connection", lambda *a, **k: FakeConn())
    calls = []

    def fake_get_site(conn, site_id):
        calls.append(site_id)
        return {"company_id": "co-1"}

    monkeypatch.setattr(sw.sites, "get_site", fake_get_site)
    monkeypatch.setattr(sw.decision_records, "insert", lambda conn, **kw: {"id": "dr-1"})

    verdicts = [_verdict(subject="topic-1"), _verdict(subject="topic-2")]
    result = sw.lambda_handler({"verdicts": verdicts}, None)

    assert result["verdicts_recorded"] == 2
    assert calls == ["site-1"]  # ONE lookup for two verdicts on the same site
