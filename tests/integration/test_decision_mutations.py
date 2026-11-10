"""Integration: the decision-loop mutation gate (plan Task 7, spec §12).

Each planted fault patches `lambda_decision_scan.<name>` (hub functions are imported there
BY NAME and called unqualified), reruns the scripted day, and the patch is undone BEFORE the
judge runs -- the judge (tools/decision_replay.py) imports only the pure hub, never the
mutant. Every fault must be reported by replay() or invariants(); `caught_by` pins the check
that is expected to see it, so a judge that goes quiet on the intended check fails here even
if some unrelated finding happens to appear.

If a fault is not caught, strengthen the judge -- never delete or weaken the mutation.
Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import dataclasses
import os
import sys
import types
import uuid

import pytest

import decision_hub
import lambda_decision_scan as lds
from repositories import recordings as real_recordings
from repositories import site_events as real_site_events
from tests.integration.decision_scripted_day import run_scripted_day

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "tools"))
import decision_replay  # noqa: E402

pytestmark = pytest.mark.integration


def wrong_company(mp):
    """emit_checklist_gaps uses checklist_reports.company_id (the recorder's home company)
    instead of the site's: the site lookup is made to answer with the report's company."""
    def session_site_companies(conn, folder, date, session_base):
        rows = real_recordings.session_site_companies(conn, folder, date, session_base)
        rec = conn.execute(
            "SELECT company_id FROM checklist_reports WHERE user_folder = %s "
            "AND report_date = %s AND session = %s LIMIT 1",
            (folder, date, session_base)).fetchone()
        if rec is None:
            return rows
        return [(site_id, str(rec[0])) for site_id, _ in rows]
    mp.setattr(lds, "recordings", types.SimpleNamespace(
        session_site_companies=session_site_companies))


def skip_dedupe(mp):
    mp.setattr(lds, "dedupe_key", lambda event, today_nz: str(uuid.uuid4()))


def skip_late(mp):
    mp.setattr(lds, "is_late", lambda event, rule, today_nz: False)


def duplicate_event(mp):
    """Each overdue event is emitted twice, the second under a fresh event_id and key."""
    real = lds._insert_event

    def insert(conn, ev, today_nz):
        is_new = real(conn, ev, today_nz)
        if ev.type == lds.OVERDUE_TYPE:
            twin = dataclasses.replace(ev, event_id=uuid.uuid4())
            rule = decision_hub.match(twin)
            real_site_events.insert(conn, {
                "event_id": twin.event_id, "company_id": twin.company_id,
                "site_id": twin.site_id, "source": twin.source, "type": twin.type,
                "subject_ref": twin.subject_ref, "bucket": rule.bucket(twin, today_nz),
                "occurred_at": twin.occurred_at, "received_at": twin.received_at,
                "payload": twin.payload, "chain_id": None, "chain_depth": 0,
                "dedupe_key": f"{decision_hub.dedupe_key(twin, today_nz)}|{uuid.uuid4()}",
                "status": "queued"})
        return is_new
    mp.setattr(lds, "_insert_event", insert)


def skip_attempt_counter(mp):
    """site_events.set_status never writes attempts (everything else unchanged)."""
    def set_status(conn, event_id, company_id, site_id, status, **fields):
        fields.pop("attempts", None)
        return real_site_events.set_status(conn, event_id, company_id, site_id, status,
                                           **fields)
    proxy = types.SimpleNamespace(**{n: getattr(real_site_events, n)
                                     for n in ("insert", "claim_due", "queued_same_subject")})
    proxy.set_status = set_status
    mp.setattr(lds, "site_events", proxy)


MUTATIONS = {
    # name: (patch, substring of the finding that must catch it)
    "wrong_company": (wrong_company, "site belongs to another company"),
    "skip_dedupe": (skip_dedupe, "dedupe_key"),
    "skip_late": (skip_late, "hub says late"),
    "duplicate_event": (duplicate_event, "dedupe_key"),
    "skip_attempt_counter": (skip_attempt_counter, "attempts"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_mutations_are_all_caught(db, name):
    patch, caught_by = MUTATIONS[name]
    day = run_scripted_day(db, mutate=patch)
    # the patch is undone: the module is the real one again before the judge runs
    assert lds._handle.__module__ == "lambda_decision_scan"
    assert lds.dedupe_key is decision_hub.dedupe_key
    assert lds.is_late is decision_hub.is_late
    assert lds.recordings is real_recordings and lds.site_events is real_site_events
    findings = (decision_replay.real(decision_replay.replay(db, company_id=day["companies"]))
                + decision_replay.invariants(db, company_ids=day["companies"]))
    assert findings, f"mutation {name} was NOT caught"
    assert any(caught_by in f for f in findings), (name, caught_by, findings)


def test_unmutated_day_has_no_findings(db):
    """The control for the parametrised gate: the same day with no fault is clean, so each
    finding above is the fault's."""
    day = run_scripted_day(db)
    assert decision_replay.replay(db, company_id=day["companies"]) == []
    assert decision_replay.invariants(db, company_ids=day["companies"]) == []
