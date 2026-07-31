"""Tier-0 finalize CLAIM step (in-VPC grace-timer target). CAS-claims the session
at the scheduled version (the idempotency guard — a mis-touch stop->resume bumps
version, so a stale one-shot no-ops), then gathers recipient/folder/date/site + the
rolling summary and enqueues a request for the non-VPC send worker. Collaborators are
injected; this tests the ORCHESTRATION, not the SQL (claim_finalize itself lives in
repositories.meeting_session). Importing the module pulls repositories (psycopg)."""
import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

import lambda_finalize_claim as fc


def test_noop_when_version_superseded(monkeypatch):
    # claim_finalize returns None -> a resume bumped version (or it already moved on)
    monkeypatch.setattr(fc.meeting_session, "claim_finalize", lambda c, s, v: None)
    enq = []
    out = fc.finalize_claim("CONN", "abc", 3,
                            resolve_context=lambda c, r: {"recipient": "x@y.com"},
                            read_rolling=lambda *a: {}, enqueue=enq.append)
    assert out["status"] == "noop" and enq == []


def test_enqueues_full_artifact_when_claimed(monkeypatch):
    monkeypatch.setattr(fc.meeting_session, "claim_finalize",
                        lambda c, s, v: {"session_id": s, "user_id": "u1", "version": v})
    enq = []
    out = fc.finalize_claim(
        "CONN", "abc", 5,
        resolve_context=lambda c, row: {"recipient": "bob@site.com", "folder": "Ada_L",
                                        "date": "2026-07-25", "siteName": "UC PK"},
        read_rolling=lambda folder, date, sid: {"summary": "Poured slab.",
                                                "open_todos": [{"text": "fix rebar", "responsible": "Neil"}]},
        enqueue=enq.append)
    assert out["status"] == "enqueued" and out["recipient"] == "bob@site.com"
    assert len(enq) == 1
    art = enq[0]
    assert art["sessionId"] == "abc" and art["version"] == 5
    assert art["folder"] == "Ada_L" and art["date"] == "2026-07-25" and art["siteName"] == "UC PK"
    assert art["summary"] == "Poured slab."
    assert art["openTodos"] == [{"text": "fix rebar", "responsible": "Neil"}]


def test_marks_failed_and_skips_when_no_recipient(monkeypatch, caplog):
    monkeypatch.setattr(fc.meeting_session, "claim_finalize",
                        lambda c, s, v: {"session_id": s, "user_id": "u1"})
    failed = []
    monkeypatch.setattr(fc.meeting_session, "mark_failed", lambda c, s: failed.append(s))
    enq = []
    with caplog.at_level("WARNING"):
        out = fc.finalize_claim("CONN", "abc", 5,
                                resolve_context=lambda c, r: {"recipient": None, "folder": "MPI2"},
                                read_rolling=lambda *a: {"summary": "S"}, enqueue=enq.append)
    assert out["status"] == "no_recipient" and enq == [] and failed == ["abc"]
    # named so prod ops can see WHICH recorder had no email (not a silent count)
    assert "abc" in caplog.text and "MPI2" in caplog.text


def test_real_resolvers_exist_for_the_handler_to_wire():
    assert callable(fc._resolve_context) and callable(fc._read_rolling) and callable(fc._enqueue)


# ---- sweep (the scheduled grace sweeper's loop) -------------------------

def test_sweep_finalizes_each_due_session(monkeypatch):
    monkeypatch.setattr(fc.meeting_session, "list_due_finalize",
                        lambda conn, g: [{"session_id": "s1", "version": 2},
                                         {"session_id": "s2", "version": 5}])
    seen = []
    monkeypatch.setattr(fc, "finalize_claim",
                        lambda conn, sid, ver, **kw: (seen.append((sid, ver)),
                                                      {"status": "enqueued", "sessionId": sid})[1])
    out = fc.sweep("CONN")
    assert seen == [("s1", 2), ("s2", 5)]                 # each due (id, version) claimed
    assert len(out) == 2 and all(r["status"] == "enqueued" for r in out)


def test_sweep_is_a_noop_when_nothing_is_due(monkeypatch):
    monkeypatch.setattr(fc.meeting_session, "list_due_finalize", lambda conn, g: [])
    assert fc.sweep("CONN") == []


# ---- inferred idle close (§8.4: no /close -> close after SESSION_GAP_MINUTES) ----

def test_sweep_does_not_infer_idle_closes_by_default(monkeypatch):
    # gate OFF (default) -> list_idle_open must never be consulted (would prematurely
    # close active /open sessions until SessionActivityFunction keeps them touched).
    called = []
    monkeypatch.setattr(fc.meeting_session, "list_idle_open",
                        lambda conn, s: called.append(s) or [])
    monkeypatch.setattr(fc.meeting_session, "list_due_finalize", lambda conn, g: [])
    fc.sweep("CONN")
    assert called == []


def test_sweep_infers_idle_closes_when_enabled(monkeypatch):
    idle = [{"session_id": "s1", "version": 2, "last_activity": "2026-07-28T14:05:00"}]
    monkeypatch.setattr(fc.meeting_session, "list_idle_open", lambda conn, s: idle)
    closed = []
    monkeypatch.setattr(fc.meeting_session, "mark_pending_close",
                        lambda conn, sid, at, intent: closed.append((sid, at, intent)) or {"ok": 1})
    # after the inferred close, the due-check picks it up -> finalized this same tick
    monkeypatch.setattr(fc.meeting_session, "list_due_finalize",
                        lambda conn, g: [{"session_id": "s1", "version": 3}])
    monkeypatch.setattr(fc, "finalize_claim",
                        lambda conn, sid, ver, **kw: {"status": "enqueued", "sessionId": sid})
    out = fc.sweep("CONN", infer_idle=True)
    assert closed == [("s1", "2026-07-28T14:05:00", "idle")]   # anchored at last activity, intent idle
    assert len(out) == 1 and out[0]["status"] == "enqueued"


def test_infer_idle_closes_counts_only_the_ones_it_moved(monkeypatch):
    idle = [{"session_id": "s1", "version": 2, "last_activity": "t1"},
            {"session_id": "s2", "version": 4, "last_activity": "t2"}]
    monkeypatch.setattr(fc.meeting_session, "list_idle_open", lambda conn, s: idle)
    # s2 already moved on (a resume raced the sweep) -> mark_pending_close returns None
    monkeypatch.setattr(fc.meeting_session, "mark_pending_close",
                        lambda conn, sid, at, intent: {"ok": 1} if sid == "s1" else None)
    assert fc.infer_idle_closes("CONN", 900) == 1


def test_to_nz_converts_utc_to_nz_local_dst_aware():
    import datetime as _dt
    # July = NZ winter (NZST +12): 23:03 UTC 07-30 -> 11:03 NZ 07-31 (the NEXT day —
    # the exact case that mis-keyed morning recordings to "No summary").
    nz = fc._to_nz(_dt.datetime(2026, 7, 30, 23, 3))
    assert nz.date().isoformat() == "2026-07-31" and nz.strftime("%H:%M") == "11:03"
    # January = NZ summer (NZDT +13, daylight saving): 02:00 UTC -> 15:00 NZ
    assert fc._to_nz(_dt.datetime(2026, 1, 15, 2, 0)).strftime("%H:%M") == "15:00"
    assert fc._to_nz(None) is None


def test_resolve_context_date_and_time_range_are_nz(monkeypatch):
    import datetime as _dt
    monkeypatch.setattr(fc.users, "get_by_id",
                        lambda conn, uid: {"email": "b@x.com", "folder_name": "Ben_UCPK"})
    monkeypatch.setattr(fc.sites, "get_site", lambda conn, sid: {"name": "UC PK"})
    # opened/closed are UTC; the DATE must be the NZ day (matches the transcript S3
    # keys) and the subject time range must be NZ wall-clock.
    row = {"user_id": "u1", "site_id": "site-1",
           "opened_at": _dt.datetime(2026, 7, 30, 23, 3),    # -> NZ 07-31 11:03
           "closed_at": _dt.datetime(2026, 7, 30, 23, 6)}    # -> NZ 07-31 11:06
    ctx = fc._resolve_context("CONN", row)
    assert ctx["date"] == "2026-07-31"
    assert ctx["timeRange"] == "11:03–11:06"


# ---- reconcile (finalizing -> sent/failed once the worker records an outcome) ----

def test_reconcile_marks_sent_and_failed_by_the_worker_result(monkeypatch):
    monkeypatch.setattr(fc.meeting_session, "list_finalizing",
                        lambda conn: [{"session_id": "s1"}, {"session_id": "s2"}, {"session_id": "s3"}])
    sent, failed = [], []
    monkeypatch.setattr(fc.meeting_session, "mark_sent", lambda conn, sid: sent.append(sid))
    monkeypatch.setattr(fc.meeting_session, "mark_failed", lambda conn, sid: failed.append(sid))
    outcomes = {"s1": {"status": "sent"}, "s2": {"status": "error"}, "s3": None}  # s3: worker not run yet
    fc.reconcile("CONN", read_result=lambda sid: outcomes.get(sid))
    assert sent == ["s1"] and failed == ["s2"]        # s3 stays finalizing for a later tick


def test_reconcile_is_a_noop_when_no_sessions_are_finalizing(monkeypatch):
    monkeypatch.setattr(fc.meeting_session, "list_finalizing", lambda conn: [])
    assert fc.reconcile("CONN", read_result=lambda sid: {"status": "sent"}) == []
