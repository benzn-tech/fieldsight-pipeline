"""The scripted day the decision-loop replay judge and mutation gate run (plan Task 7).

Not a test module: `run_scripted_day(conn, mutate=None)` seeds two companies and three sites,
runs both scans and `process` several times with time advanced, and returns the ids the
judge should be scoped to. It contains NO asserts and must survive every planted fault, so
a mutation test can only fail through the judge (replay / invariants), never because the
driver crashed first.

Every patch -- the forced handler failure and the caller's mutation -- lives inside ONE
`pytest.MonkeyPatch.context()` that is closed before this function returns, so
`lambda_decision_scan` is pristine again when the judge runs.

What the day contains (each is something a planted fault has to show up on):
  * an external recorder (home company A) recording on company B's site -> gap events under
    B, never A (`wrong_company`);
  * a gap report dated 8 days back -> 'late' (`skip_late`);
  * a STALE overdue item on A1 (due 7 days before NOW's NZ day) -> its todo.overdue event is
    'late' and A1 gets one todo.overdue_backlog digest card (spec 6.5 rev 6); skipping the
    late check cards the stale item itself (`skip_late`, caught on the todo.overdue row);
  * the overdue scan run twice at the same instant -> 'duplicate' rows (`skip_dedupe`,
    `duplicate_event`);
  * one overdue event whose handler is forced to fail until it stops (5 attempts, fault card
    at 3, bumped at 5) (`skip_attempt_counter`);
  * a gap re-run that drops a gap -> a 'superseded' card;
  * a second NZ day: overdue events that find their open card ('nothing'), a merged pair, an
    unmatched type and a too-deep chain.
"""
import datetime as dt
import uuid

import pytest

import decision_hub
import lambda_decision_scan as lds
from repositories import memberships
from repositories import site_events as se
from tests.integration.test_decision_scan import (NOW, STALE_DEADLINE, FakeS3, _company,
                                                  _done, _gaps, _overdue_item, _reader,
                                                  _record_on, _report, _site, _tag, _user)

DAY2 = NOW + dt.timedelta(days=1)
OLD_DATE = "2026-11-02"                        # 8 days before NOW's NZ day (late_after 7)


def _recording_by(conn, recorder_co, site, folder, session, date):
    """A recording of `session` on `site` by a user of `recorder_co` (which may be another
    company than the site's: an external recorder)."""
    u = _user(conn, recorder_co)
    conn.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, "
        "started_at) VALUES (%s,%s,%s,'audio',%s,%s,now())",
        (recorder_co["id"], u["id"], site["id"],
         f"users/{folder}/audio/{date}/{session}_{uuid.uuid4().hex[:6]}.m4a",
         uuid.uuid4().hex))


def _direct(conn, co, site, type_, subject, received_at, **over):
    """A queued event inserted directly, with the dedupe_key and bucket the hub would give
    it (so the judge must find nothing wrong with it)."""
    ev = decision_hub.Event(event_id=uuid.uuid4(), company_id=co["id"], site_id=site["id"],
                            source="test", type=type_, subject_ref=subject,
                            occurred_at=received_at, received_at=received_at,
                            payload={"text": "Direct", "deadline": "2026-11-01"},
                            chain_id=over.get("chain_id"),
                            chain_depth=over.get("chain_depth", 0))
    today = decision_hub.today_nz_of(ev)
    rule = decision_hub.match(ev)
    row = {"event_id": ev.event_id, "company_id": ev.company_id, "site_id": ev.site_id,
           "source": ev.source, "type": ev.type, "subject_ref": ev.subject_ref,
           "bucket": rule.bucket(ev, today) if rule else "", "occurred_at": ev.occurred_at,
           "received_at": ev.received_at, "payload": ev.payload, "chain_id": ev.chain_id,
           "chain_depth": ev.chain_depth, "dedupe_key": decision_hub.dedupe_key(ev, today),
           "status": "queued", "not_before": over.get("not_before")}
    return se.insert(conn, row)[0]


def _drain(conn, t, company_ids, rounds=6):
    """process() at t, then at each next due not_before of a queued event in scope, so a
    failing event is retried until it stops. Returns the last time used."""
    for _ in range(rounds):
        lds.process(conn, t)
        nb = conn.execute(
            "SELECT min(not_before) FROM site_events WHERE status = 'queued' "
            "AND not_before IS NOT NULL AND company_id = ANY(%s::uuid[])",
            ([str(c) for c in company_ids],)).fetchone()[0]
        if nb is None or nb > t + dt.timedelta(hours=6):
            break
        t = max(t, nb)
    return t


def run_scripted_day(conn, mutate=None):
    """Seed and run the day; `mutate(mp)` applies a planted fault through the same
    MonkeyPatch (undone before return). Returns {"companies": [A, B], ...ids}."""
    home = _company(conn)                                        # company A
    a1 = _site(conn, home)
    co = _company(conn)                                          # company B
    b1 = _site(conn, co)
    b2 = _site(conn, co)
    for s, c in ((a1, home), (b1, co), (b2, co)):
        pm = _user(conn, c)
        memberships.add_membership(conn, pm["id"], s["id"], "pm")
    _user(conn, co, "admin")
    _user(conn, home, "admin")

    # overdue items (deadline before NOW's NZ day)
    _overdue_item(conn, a1, "Order the rebar")
    _overdue_item(conn, a1, "Old pour plan", deadline=STALE_DEADLINE)   # stale: late + digest
    _overdue_item(conn, b1, "Book the pump")
    fence = _overdue_item(conn, b2, "Fix the site fence")         # forced to fail on day 1
    _overdue_item(conn, b2, "Order the forms")
    fence_ref = str(fence["stable_id"])
    day1 = NOW.date()

    # checklist reports
    fx, sx = f"FX_{_tag()}", f"sx{_tag()}"                       # external recorder on B1
    _recording_by(conn, home, b1, fx, sx, NOW.date().isoformat())
    fo, so = f"FO_{_tag()}", f"so{_tag()}"                       # old report on A1
    _record_on(conn, home, a1, fo, so, date=OLD_DATE)
    fb, sb = f"FB_{_tag()}", f"sb{_tag()}"                       # B2 report, re-run later
    _record_on(conn, co, b2, fb, sb)
    fake = FakeS3({
        "kx": _done(**{"Pre-pour": _gaps(nc=[(3, "Cover to steel checked")],
                                        failed=[(2, "Edge protection installed")]),
                       "Safety walk": _gaps(failed=[(1, "Hazard board updated")])}),
        "ko": _done(**{"Pre-pour": _gaps(nc=[(1, "Old gap")])}),
        "kb1": _done(**{"Housekeeping": _gaps(nc=[(1, "Bins emptied"), (2, "Walkways clear")])}),
        "kb2": _done(**{"Housekeeping": _gaps(nc=[(2, "Walkways clear")])}),
    })
    _report(conn, home["id"], fx, sx, "kx")                      # row under the RECORDER's A
    _report(conn, home["id"], fo, so, "ko", date=OLD_DATE)
    rb = _report(conn, co["id"], fb, sb, "kb1")
    scope = [home["id"], co["id"]]

    with pytest.MonkeyPatch.context() as mp:
        real_handle = lds._handle

        def forced(c, event, rule, now):
            if (event.subject_ref == fence_ref
                    and decision_hub.today_nz_of(event) == day1):
                raise RuntimeError("forced handler failure (scripted day)")
            return real_handle(c, event, rule, now)
        mp.setattr(lds, "_handle", forced)
        if mutate is not None:
            mutate(mp)

        # day 1
        lds.emit_todo_overdue(conn, NOW)
        lds.emit_todo_overdue(conn, NOW)                         # same instant: duplicates
        lds.emit_checklist_gaps(conn, _reader(fake), NOW)
        t = _drain(conn, NOW, scope)
        # the B2 report is re-run and one gap is gone -> its card is superseded
        conn.execute("UPDATE checklist_reports SET result_key = 'kb2', created_at = now() "
                     "WHERE id = %s", (rb,))
        lds.emit_checklist_gaps(conn, _reader(fake), t)
        _drain(conn, t, scope)

        # day 2: a merged pair, an unmatched type, a too-deep chain, the overdue scan again
        subject = str(uuid.uuid4())
        _direct(conn, co, b1, "todo.overdue", subject, NOW,
                not_before=DAY2 + dt.timedelta(days=1))          # backing off: merged
        _direct(conn, co, b1, "todo.overdue", subject, DAY2)
        _direct(conn, home, a1, "something.else", str(uuid.uuid4()), DAY2)
        _direct(conn, co, b2, "todo.overdue", str(uuid.uuid4()), DAY2,
                chain_id=uuid.uuid4(), chain_depth=4)
        lds.emit_todo_overdue(conn, DAY2)
        _drain(conn, DAY2, scope)
    return {"companies": scope, "home": home, "co": co, "sites": [a1, b1, b2],
            "fence_ref": fence_ref}
