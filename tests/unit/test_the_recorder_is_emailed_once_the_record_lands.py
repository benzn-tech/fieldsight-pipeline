"""The recorder's confirmation email is enqueued by the step that lands the record.

The sweep enqueued it at CLAIM time, in the same breath as asking for the
session's final extraction. So the email routinely went out before that
extraction existed -- measured on prod, a 28-minute session was emailed 44 s
after its last segment while the final landed at +253 s -- and its rows had to
come from a second summariser run only for the email. One meeting, two accounts
of it, and the one on the site is the one that lasts.

Item-writer is the step that knows the rows are durable, so it enqueues.

WHAT MAKES IT SAFE TO DO FROM HERE:

  tier == "final"        a live pass is not the session's last word
  status == "finalizing" the re-run chain writes a final again (up to 3
                         generations), and org-api's regenerate writes one
                         MONTHS later for a session that was sent long ago --
                         each would otherwise mail the recorder about an old
                         meeting
  IfNoneMatch="*"        the sweep's backstop can write the same key; S3
                         notifies on overwrites, so the loser must not write at
                         all. NOT check-then-write: this role cannot read the
                         prefix, and AccessDenied reads as absent (403 != 404),
                         so both writers would pass a check

A session that produces NO action items still gets its email. Most do not: 11 of
16 measured prod sessions produced none. Withholding would push them to the
backstop, which quotes the other summariser -- the disagreement this removes.
"""
import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")
fc = pytest.importorskip("lambda_finalize_claim", reason="requires psycopg (installed in CI)")

SID = "d" * 32
BASE = "sid" + SID
DATE = "2026-09-16"
CTX = {"recipient": "ben@example.nz", "folder": "Ben_UCPK2", "date": DATE,
       "siteName": "Ellesmere", "timeRange": "09:00–09:28"}

EXTRACTION = {
    "tier": "final",
    "topics": [{"topic_title": "Cylinder testing",
                "action_items": [{"action": "Re-inspect the cylinder",
                                  "responsible": "Neil", "deadline": "Tomorrow 08:00"}]}],
}


@pytest.fixture
def wired(monkeypatch):
    state = {"status": "finalizing", "ctx": dict(CTX)}
    monkeypatch.setattr(iw.meeting_session, "get",
                        lambda conn, sid: {"session_id": sid, "status": state["status"],
                                           "user_id": "u-1"})
    monkeypatch.setattr(fc, "_resolve_context", lambda conn, row: dict(state["ctx"]))
    return state


def test_THE_test_the_email_is_enqueued_when_the_record_lands(wired):
    ctx = iw._final_email_context("CONN", BASE, EXTRACTION, DATE)
    assert ctx["kind"] == "final" and ctx["sessionId"] == SID
    assert ctx["recipient"] == "ben@example.nz"
    assert [r["text"] for r in ctx["openTodos"]] == ["Re-inspect the cylinder"]


def test_the_folder_rides_along_for_the_brief_poll(wired):
    """handoff-sync plan §2.2: the worker needs the folder to poll
    session_brief/<folder>/<date>/sid<sessionId>/latest.json -- nothing else in
    the artifact carries it."""
    ctx = iw._final_email_context("CONN", BASE, EXTRACTION, DATE)
    assert ctx["folder"] == "Ben_UCPK2"


def test_the_due_date_is_the_one_the_record_holds(wired):
    """Aurora stores the RESOLVED date; the email used to show the spoken text,
    so the same item read differently in the two places."""
    ctx = iw._final_email_context("CONN", BASE, EXTRACTION, DATE)
    assert ctx["openTodos"][0]["due"] == "2026-09-17"


def test_an_unresolvable_deadline_keeps_what_was_said(wired):
    e = {"tier": "final", "topics": [{"action_items": [
        {"action": "Chase the supplier", "responsible": None, "deadline": "when he calls back"}]}]}
    ctx = iw._final_email_context("CONN", BASE, e, DATE)
    assert ctx["openTodos"][0]["due"] == "when he calls back"


def test_a_session_with_no_action_items_is_still_emailed(wired):
    """Most sessions produce none. The renderer says so explicitly; the backstop
    would quote the other summariser instead."""
    ctx = iw._final_email_context("CONN", BASE, {"tier": "final", "topics": []}, DATE)
    assert ctx is not None and ctx["openTodos"] == []


def test_a_session_that_is_not_waiting_is_not_emailed(wired, caplog):
    """The re-run chain and org-api's regenerate both write a final for a
    session that was sent long ago."""
    wired["status"] = "sent"
    assert iw._final_email_context("CONN", BASE, EXTRACTION, DATE) is None


def test_a_session_with_nobody_to_email_is_left_to_the_claim_step(wired):
    wired["ctx"] = {**CTX, "recipient": "  "}
    assert iw._final_email_context("CONN", BASE, EXTRACTION, DATE) is None


def test_a_merged_base_is_not_a_device_session(wired):
    assert iw._final_email_context("CONN", "grp" + "e" * 32, EXTRACTION, DATE) is None


# ---- the put ---------------------------------------------------------------

def test_the_request_is_written_to_the_sessions_own_key():
    seen = {}

    def put(key, body, only_if_absent=False):
        seen.update(key=key, body=body, cond=only_if_absent)
        return True

    assert iw._enqueue_final_email({"sessionId": SID, "kind": "final"}, put=put) is True
    assert seen["key"] == "session_finalize_requests/%s.json" % SID
    assert seen["cond"] is True, "the backstop writes this key too; the loser must not write"


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


def test_the_first_writer_wins(monkeypatch):
    s3 = _S3()
    _with_s3(monkeypatch, s3)
    assert iw._put_finalize_request("k.json", {"a": 1}, only_if_absent=True) is True
    assert s3.calls[0]["IfNoneMatch"] == "*"


def test_the_second_writer_stops_without_sending(monkeypatch):
    _with_s3(monkeypatch, _S3(error=_client_error("PreconditionFailed", 412)))
    assert iw._put_finalize_request("k.json", {"a": 1}, only_if_absent=True) is False


def test_any_other_failure_is_raised(monkeypatch):
    """A lost email must not look like a deduplicated one."""
    _with_s3(monkeypatch, _S3(error=_client_error("AccessDenied", 403)))
    with pytest.raises(Exception):
        iw._put_finalize_request("k.json", {"a": 1}, only_if_absent=True)


def test_the_group_path_still_writes_unconditionally(monkeypatch):
    """`{sid}-updated` has one producer; changing it is a separate decision."""
    s3 = _S3()
    _with_s3(monkeypatch, s3)
    iw._put_finalize_request("k-updated.json", {"a": 1})
    assert "IfNoneMatch" not in s3.calls[0]
