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


def test_marks_failed_and_skips_when_no_recipient(monkeypatch):
    monkeypatch.setattr(fc.meeting_session, "claim_finalize",
                        lambda c, s, v: {"session_id": s, "user_id": "u1"})
    failed = []
    monkeypatch.setattr(fc.meeting_session, "mark_failed", lambda c, s: failed.append(s))
    enq = []
    out = fc.finalize_claim("CONN", "abc", 5,
                            resolve_context=lambda c, r: {"recipient": None},
                            read_rolling=lambda *a: {"summary": "S"}, enqueue=enq.append)
    assert out["status"] == "no_recipient" and enq == [] and failed == ["abc"]


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
