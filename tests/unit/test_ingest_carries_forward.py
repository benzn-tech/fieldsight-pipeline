"""Unit: Track B final wave (Ruling R19) -- lambda_ingest's report path carries a human's
tick/answer/edit forward across its own re-ingest, the same way lambda_item_writer's
extraction path has since Task 4/5.

Final review Important #1: `ingest_report` supersedes topics on every re-ingest (both the
report-key clear and, off the authority flip, the extraction-prefix supersede) but had no
carry-forward, no decisions/questions dual-write and no orphan metric -- a tick on a
report-sourced item was silently orphaned. The fix moved the carry-forward APPLICATION
(SAVEPOINT posture, crash-fallback count, EMF/WARNING reporting) into carry_forward_apply.py
so both writers share it unchanged; this file proves only lambda_ingest's WIRING to that
shared function -- the function's own matching/degrade behaviour is proven once, at the
carry_forward_apply level (tests/unit/test_orphaned_human_edits_reported.py), and end-to-end
against real Postgres in tests/integration/test_ingest_carries_forward.py.
"""
import json

import pytest

import carry_forward_apply
from tests.unit.test_lambda_ingest import FakeConn, FakeS3, REPORT_KEY, ing, make_report
from tests.unit.test_lambda_ingest import wired  # noqa: F401  (fixture)

OLD_1 = {"id": "old-report-topic-1", "title": "t1", "summary": "s1"}
OLD_2 = {"id": "old-extraction-topic-2", "title": "t2", "summary": "s2"}


def test_ingest_collects_retired_rows_from_both_supersede_calls_and_carries_forward(
        monkeypatch, wired):
    """AUTHORITY_FLIP off (the default in every deployed env today) -> both supersede calls
    run, and their RETURNING rows -- from the report-key clear AND the extraction-prefix
    supersession -- must both reach carry_forward_apply as ONE old pool, matched against
    every topic THIS pass wrote (new_topic_ids), scoped to this site and reported under the
    report key (Ruling R9/R19)."""
    monkeypatch.setattr(ing.topics, "supersede_topics_for_source", lambda *a, **k: [OLD_1])
    monkeypatch.setattr(ing.topics, "supersede_topics_for_source_prefix",
                        lambda *a, **k: [OLD_2])

    captured = []
    monkeypatch.setattr(
        ing.carry_forward_apply, "_carry_forward_children",
        lambda conn, old_ids, new_ids, site_id, key: captured.append(
            (sorted(old_ids), sorted(new_ids), site_id, key)))

    result = ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert result["skipped"] is False
    assert len(captured) == 1, captured
    old_ids, new_ids, site_id, key = captured[0]
    assert old_ids == sorted([OLD_1["id"], OLD_2["id"]])
    assert new_ids == ["topic-uuid-0"], "the report's own newly-written topic, this pass"
    assert site_id == "site-1"
    assert key == REPORT_KEY


def test_ingest_makes_no_carry_forward_call_when_nothing_was_retired(monkeypatch, wired):
    """The common case (`wired`'s own default: both supersede stubs answer []) -- a pass that
    retired nothing has no 'old' pool and nothing to report, so carry_forward_apply must not
    even be asked (mirrors lambda_item_writer's own `if retired_topics:` guard)."""
    calls = []
    monkeypatch.setattr(ing.carry_forward_apply, "_carry_forward_children",
                        lambda *a, **k: calls.append(a))

    ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert calls == []


def test_ingest_dual_writes_decisions_and_questions_alongside_the_jsonb_kwargs(
        monkeypatch, wired):
    """The row-table dual-write (Task 5's pattern, extended here to the report path) must
    receive EXACTLY the same values as upsert_topic's own `decisions=`/`open_questions=`
    kwargs -- the row table and the jsonb mirror must never disagree about which entries
    exist, and this is the report path's own key-spelling (`key_decisions`/`open_questions`)
    doing the translation, not a second, possibly-divergent one."""
    report = make_report()
    report["topics"][0]["key_decisions"] = ["Use precast panels for level 3"]
    report["topics"][0]["open_questions"] = ["Confirm the crane booking for Thursday"]
    monkeypatch.setattr(ing, "_s3_client", FakeS3({REPORT_KEY: json.dumps(report)}))

    upsert_kwargs = []
    monkeypatch.setattr(
        ing.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            upsert_kwargs.append(kw) or {"id": "topic-uuid-0"})
    decisions_calls = []
    questions_calls = []
    monkeypatch.setattr(
        ing.topic_decisions, "insert_decisions",
        lambda conn, topic_id, site_id, decisions:
            decisions_calls.append((topic_id, site_id, decisions)))
    monkeypatch.setattr(
        ing.topic_questions, "insert_questions",
        lambda conn, topic_id, site_id, questions:
            questions_calls.append((topic_id, site_id, questions)))

    ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert decisions_calls == [("topic-uuid-0", "site-1", upsert_kwargs[0]["decisions"])]
    assert questions_calls == [("topic-uuid-0", "site-1", upsert_kwargs[0]["open_questions"])]
    assert upsert_kwargs[0]["decisions"] == ["Use precast panels for level 3"]
    assert upsert_kwargs[0]["open_questions"] == ["Confirm the crane booking for Thursday"]


def test_ingest_on_a_defer_day_reports_every_retired_row_as_orphaned(monkeypatch, wired):
    """Under the authority flip, a defer day writes NO report topics (`new_topic_ids` stays
    empty) -- but the report-key supersede still runs (it always does), so a PRIOR non-flip
    report's human-touched rows correctly report as orphans: the day's authoritative item
    store moved to the extraction topics lambda_item_writer already carries forward on its
    own pass, not to a report topic this pass never wrote."""
    monkeypatch.setattr(ing, "AUTHORITY_FLIP", True)
    monkeypatch.setattr(ing, "_should_defer", lambda *a, **k: True)
    monkeypatch.setattr(ing.topics, "supersede_topics_for_source", lambda *a, **k: [OLD_1])
    monkeypatch.setattr(ing, "_match_report_topics_to_extraction", lambda *a, **k: {})

    captured = []
    monkeypatch.setattr(
        ing.carry_forward_apply, "_carry_forward_children",
        lambda conn, old_ids, new_ids, site_id, key: captured.append((old_ids, new_ids)))

    ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert captured == [([OLD_1["id"]], [])], (
        "the retired report-key row must still be handed to carry-forward with an EMPTY "
        "new pool, not skipped just because this pass wrote nothing new")


def test_a_carry_forward_crash_does_not_abort_the_ingest(monkeypatch, wired):
    """Ruling R10/R19: the same degrade-not-abort guarantee lambda_item_writer's carry-
    forward already has must hold on the report path -- a bug inside carry_forward must not
    take a whole report ingest down with it. The SAVEPOINT/degrade control flow itself is
    already proven at the carry_forward_apply level
    (test_carry_forward_children_degrades_instead_of_raising_on_a_matcher_crash in
    test_orphaned_human_edits_reported.py); this proves only that ingest_report's own call
    site reaches the REAL (unmocked) `_carry_forward_children` and survives a crash inside
    it."""
    class _FakeTxn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    conn = FakeConn()
    conn.transaction = lambda: _FakeTxn()
    monkeypatch.setattr(ing, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(ing.topics, "supersede_topics_for_source", lambda *a, **k: [OLD_1])
    monkeypatch.setattr(ing.topics, "supersede_topics_for_source_prefix", lambda *a, **k: [])
    monkeypatch.setattr(carry_forward_apply, "_carry_forward_one_table",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("matcher exploded")))
    monkeypatch.setattr(carry_forward_apply, "_count_human_touched_old", lambda *a, **k: 0)

    result = ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)  # must not raise

    assert result["skipped"] is False
    assert result["topics"] == 1
