"""The confirmation email waits for the session's final extraction.

The sweep used to enqueue the email in the same breath as requesting that
extraction, so the email went out first and its rows came from a second
summariser run only for it. Item-writer enqueues it now, once the rows are
durable in Aurora (previous commit). This is the other half: the sweep stops
enqueueing at claim time, and keeps a BACKSTOP for the sessions whose final
extraction never arrives at all — no turns, no API key, a raise that exhausted
its S3 retries, a login with no folder.

THE ANCHOR IS THE CLAIM, NOT THE CLOSE, and that is the whole reason
`finalizing_at` exists. `infer_idle_closes` stores the session's LAST ACTIVITY
as `closed_at`, so an idle-inferred close is already SESSION_GAP_MINUTES old
when it is claimed: a deadline measured from the close fires on the very next
tick, before any extraction can land, for exactly the offline and crashed
recordings the backstop is meant to protect.

TWO PRODUCERS, ONE KEY. Item-writer and this backstop both write
`session_finalize_requests/{sid}.json`, and S3 notifies on every put including
an overwrite, so the loser must write nothing. The put itself decides
(IfNoneMatch): this role cannot read that prefix, and AccessDenied reads as
absent, so a check-then-write would let both through.
"""
import datetime
import json

import pytest

fc = pytest.importorskip("lambda_finalize_claim", reason="requires psycopg (installed in CI)")

SID = "f" * 32
NOW = datetime.datetime(2026, 9, 16, 21, 0, tzinfo=datetime.timezone.utc)
CTX = {"recipient": "ben@example.nz", "folder": "Ben_UCPK2", "date": "2026-09-16",
       "siteName": "Ellesmere", "timeRange": "09:00–09:28"}


def _row(session_id=SID, waited_s=None, **kw):
    anchor = None if waited_s is None else NOW - datetime.timedelta(seconds=waited_s)
    return {"session_id": session_id, "version": 3, "finalizing_at": anchor, **kw}


def _backstop(rows, rolling=None, enqueue=None):
    written = []

    def _enqueue(artifact):
        written.append(artifact)
        return True if enqueue is None else enqueue(artifact)

    mailed = fc.backstop(
        "CONN",
        list_waiting=lambda conn: rows,
        resolve_context=lambda conn, row: dict(CTX),
        read_rolling=lambda folder, date, sid: rolling or {},
        enqueue=_enqueue,
        now=NOW)
    return mailed, written


def test_THE_test_a_session_still_waiting_is_not_emailed_yet():
    """Four minutes in, the final extraction may still be coming — the measured
    worst case is ~250 s."""
    mailed, written = _backstop([_row(waited_s=240)])
    assert mailed == [] and written == []


def test_a_session_whose_final_never_came_is_still_emailed():
    mailed, written = _backstop([_row(waited_s=301)],
                                rolling={"summary": "S", "open_todos": [{"text": "t"}]})
    assert mailed == [SID]
    assert written[0]["kind"] == "rolling"
    # The rolling to-dos, plus the rolling summary as one topic row -- otherwise a
    # backstopped session with no to-dos is an email that says nothing, and the
    # two paths send visibly different emails for the same kind of recording.
    assert written[0]["openTodos"] == [
        {"text": "t"},
        {"text": "S", "responsible": None, "due": None, "kind": "topic"}]


def test_the_backstop_email_is_not_dressed_as_the_merged_one():
    """`updated` rewrites the subject and moves the result to the -updated key,
    where reconcile never sees it and the session stays finalizing for good."""
    _mailed, written = _backstop([_row(waited_s=600)])
    assert written[0]["kind"] != "updated"


def test_an_idle_close_is_measured_from_the_claim_not_the_close():
    """closed_at for an idle close is the last ACTIVITY — already ten minutes old
    at claim time. Measured from there, every such session would be emailed
    immediately, ahead of its extraction."""
    row = _row(waited_s=30)
    row["closed_at"] = NOW - datetime.timedelta(minutes=45)
    mailed, written = _backstop([row])
    assert mailed == [] and written == []


def test_a_session_claimed_before_the_column_existed_is_left_alone():
    """finalizing_at NULL: no anchor, so no invented deadline."""
    mailed, written = _backstop([_row(waited_s=None)])
    assert mailed == [] and written == []


def test_the_loser_of_the_race_reports_nothing_mailed():
    """Item-writer got there first: the conditional put refuses, and this pass
    must not claim to have emailed anyone."""
    mailed, written = _backstop([_row(waited_s=900)], enqueue=lambda a: False)
    assert written and mailed == []


def test_a_session_with_nobody_to_email_is_skipped():
    written = []
    mailed = fc.backstop(
        "CONN",
        list_waiting=lambda conn: [_row(waited_s=900)],
        resolve_context=lambda conn, row: {**CTX, "recipient": "   "},
        read_rolling=lambda *a: {},
        enqueue=lambda a: written.append(a) or True,
        now=NOW)
    assert mailed == [] and written == []


# ---- the claim no longer enqueues -----------------------------------------

def test_the_claim_does_not_enqueue_the_email(monkeypatch):
    monkeypatch.setattr(fc.meeting_session, "claim_finalize",
                        lambda conn, sid, v: {"session_id": sid, "user_id": "u-1", "version": v})
    asked = []
    out = fc.finalize_claim("CONN", SID, 3,
                            resolve_context=lambda conn, row: dict(CTX),
                            read_rolling=lambda *a: {"summary": "S"},
                            request_extraction=lambda *a: asked.append(a))
    assert out["status"] == "waiting"
    assert asked, "the final extraction is still requested at claim time"


def test_a_claim_with_no_recipient_still_fails_the_session(monkeypatch):
    monkeypatch.setattr(fc.meeting_session, "claim_finalize",
                        lambda conn, sid, v: {"session_id": sid, "user_id": "u-1", "version": v})
    failed = []
    monkeypatch.setattr(fc.meeting_session, "mark_failed", lambda conn, sid: failed.append(sid))
    out = fc.finalize_claim("CONN", SID, 3,
                            resolve_context=lambda conn, row: {**CTX, "recipient": ""},
                            read_rolling=lambda *a: {},
                            request_extraction=lambda *a: None)
    assert out["status"] == "no_recipient" and failed == [SID]


# ---- the put ---------------------------------------------------------------

class _S3:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    def put_object(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error


def _client_error(code, status):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject")


def _with_s3(monkeypatch, s3):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)


def test_the_sweep_writes_the_request_only_if_absent(monkeypatch):
    s3 = _S3()
    _with_s3(monkeypatch, s3)
    assert fc._enqueue({"sessionId": SID}) is True
    assert s3.calls[0]["IfNoneMatch"] == "*"


def test_the_sweep_stops_when_item_writer_got_there_first(monkeypatch):
    _with_s3(monkeypatch, _S3(error=_client_error("PreconditionFailed", 412)))
    assert fc._enqueue({"sessionId": SID}) is False


def test_any_other_put_failure_is_raised(monkeypatch):
    """A lost email must not look like a deduplicated one."""
    _with_s3(monkeypatch, _S3(error=_client_error("AccessDenied", 403)))
    with pytest.raises(Exception):
        fc._enqueue({"sessionId": SID})


# ---- §2.1: the brief request rides alongside the final-extraction request --
# lambda_finalize_claim._request_extraction now ALSO enqueues
# session_finalize_requests/brief-<sessionId>.json, concurrently with the
# final extraction request, on the same conditional-put contract as every
# other producer of that prefix.

def test_requesting_the_final_extraction_also_enqueues_a_brief_request(monkeypatch):
    s3 = _S3()
    _with_s3(monkeypatch, s3)
    fc._request_extraction(SID, "Ben_Lin", "2026-09-16")
    assert len(s3.calls) == 2
    extraction_call, brief_call = s3.calls
    assert extraction_call["Key"] == f"extraction_requests/{SID}.json"
    assert brief_call["Key"] == f"session_finalize_requests/brief-{SID}.json"
    body = json.loads(brief_call["Body"])
    assert body == {"kind": "brief", "sessionId": SID, "folder": "Ben_Lin",
                    "date": "2026-09-16"}
    assert brief_call["IfNoneMatch"] == "*"


def test_a_brief_request_already_enqueued_is_not_an_error(monkeypatch):
    """A 412 means item-writer or an earlier tick already asked -- fine, must
    not raise (the final extraction's own put already succeeded)."""
    calls = []

    class _MixedS3:
        def put_object(self, **kw):
            calls.append(kw)
            if kw["Key"].startswith("session_finalize_requests/brief-"):
                raise _client_error("PreconditionFailed", 412)

    _with_s3(monkeypatch, _MixedS3())
    fc._request_extraction(SID, "Ben_Lin", "2026-09-16")   # must not raise
    assert len(calls) == 2


def test_a_brief_request_failure_other_than_412_is_raised(monkeypatch):
    class _MixedS3:
        def put_object(self, **kw):
            if kw["Key"].startswith("session_finalize_requests/brief-"):
                raise _client_error("AccessDenied", 403)

    _with_s3(monkeypatch, _MixedS3())
    with pytest.raises(Exception):
        fc._request_extraction(SID, "Ben_Lin", "2026-09-16")


def test_a_failed_brief_request_does_not_block_finalize(monkeypatch, caplog):
    """finalize_claim's own best-effort wrapper catches it -- proven here at the
    level that matters: the session still ends up `waiting`, not stuck."""
    monkeypatch.setattr(fc.meeting_session, "claim_finalize",
                        lambda c, s, v: {"session_id": s, "user_id": "u1", "version": v})

    class _BoomOnBrief:
        def put_object(self, **kw):
            if kw["Key"].startswith("session_finalize_requests/brief-"):
                raise _client_error("AccessDenied", 403)

    _with_s3(monkeypatch, _BoomOnBrief())
    with caplog.at_level("ERROR"):
        out = fc.finalize_claim(
            "CONN", SID, 3,
            resolve_context=lambda c, r: dict(CTX),
            read_rolling=lambda *a: {},
            request_extraction=fc._request_extraction)
    assert out["status"] == "waiting"


# ---- wiring ---------------------------------------------------------------

def test_the_handler_runs_the_backstop(monkeypatch):
    """The claim no longer enqueues, so if nothing calls this on the tick, a
    session whose final extraction never lands is never emailed at all — and
    that silence would look exactly like the feature working. This repo has
    shipped working functions that no production code called before."""
    monkeypatch.setattr(fc, "sweep", lambda conn: [])
    monkeypatch.setattr(fc, "reconcile", lambda conn, r: [])
    monkeypatch.setattr(fc, "_sweep_groups_contained", lambda conn: [])
    called = []
    monkeypatch.setattr(fc, "backstop", lambda conn, **kw: called.append(1) or [])

    class _Conn:
        def __enter__(self): return object()

        def __exit__(self, *a): return False

    monkeypatch.setitem(__import__("sys").modules, "db.connection",
                        type("M", (), {"get_connection": staticmethod(lambda: _Conn())}))

    fc.lambda_handler({}, None)
    assert called == [1], "the backstop is not wired into the sweep"


def test_the_backstop_runs_after_reconcile(monkeypatch):
    """A session the worker settled this tick is no longer `finalizing`, so
    reconcile first keeps the backstop from mailing one that just completed."""
    order = []
    monkeypatch.setattr(fc, "sweep", lambda conn: order.append("sweep") or [])
    monkeypatch.setattr(fc, "reconcile", lambda conn, r: order.append("reconcile") or [])
    monkeypatch.setattr(fc, "backstop", lambda conn, **kw: order.append("backstop") or [])
    monkeypatch.setattr(fc, "_sweep_groups_contained", lambda conn: order.append("groups") or [])

    class _Conn:
        def __enter__(self): return object()

        def __exit__(self, *a): return False

    monkeypatch.setitem(__import__("sys").modules, "db.connection",
                        type("M", (), {"get_connection": staticmethod(lambda: _Conn())}))

    fc.lambda_handler({}, None)
    assert order == ["sweep", "reconcile", "backstop", "groups"]
