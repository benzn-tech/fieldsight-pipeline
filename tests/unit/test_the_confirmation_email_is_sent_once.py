"""One recording, one confirmation email — however many times the trigger fires.

`lambda_session_finalize` is S3-triggered on `session_finalize_requests/*.json`.
S3 notifies on EVERY put of a key, an overwrite included, and the function sets
no MaximumRetryAttempts, so the Lambda async default of two retries applies on
top of that. The worker sent unconditionally: it never read back its own result,
so any second write of the request — a re-drive, a race between two writers, a
hand-run `aws s3 cp` — was a second email to the recorder, and nothing recorded
that it had happened.

This matters more once the send moves behind the final extraction (spec
2026-09-16): two producers will be able to write that request key, and the
review's first blocking finding was that the guards proposed for them were
check-then-write on S3 by a role with no read grant — where AccessDenied reads
as "absent" and both writers proceed.

THE LENIENT DIRECTION IS DELIBERATE. A read that fails answers "not sent", so a
missing grant costs a duplicate rather than a recorder's only confirmation — but
it is logged, because a swallowed 403 here is exactly how the original 36 failed
sends stayed invisible. `NoSuchKey`, the ordinary "not sent yet", logs nothing.
"""
import json
import logging

import pytest

fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")

SID = "b" * 32
ARTIFACT = {"recipient": "ben@example.nz", "folder": "Ben_UCPK2", "date": "2026-09-16",
            "sessionId": SID, "siteName": "Ellesmere", "openTodos": [{"text": "Re-inspect"}]}


@pytest.fixture
def quiet(monkeypatch):
    """No deletion mirror, no LLM: this file is about the send guard only."""
    monkeypatch.setattr(fin, "_session_was_deleted", lambda artifact: False)
    monkeypatch.setattr(fin, "_complete_summary", lambda artifact: None)


def _run(already_sent, artifact=None):
    sent, written = [], []
    out = fin.process_finalize_request(
        dict(artifact or ARTIFACT),
        send=lambda *a, **k: sent.append(a),
        write_result=lambda sid, res: written.append((sid, res)),
        already_sent=already_sent)
    return out, sent, written


def test_THE_test_a_second_trigger_does_not_send_a_second_email(quiet):
    out, sent, written = _run(lambda rid: True)
    assert out["status"] == "skipped" and out["reason"] == "already sent"
    assert sent == []                      # the recorder is not told twice
    assert written == []                   # and the first result is not overwritten


def test_a_session_not_yet_sent_is_sent(quiet):
    out, sent, written = _run(lambda rid: False)
    assert out["status"] == "sent" and len(sent) == 1
    assert written[0][1]["status"] == "sent"


def test_the_updated_email_is_checked_against_its_own_key(quiet):
    """A member of a merged group gets a second, DIFFERENT email whose result
    lives at `{sid}-updated`. Checking the solo key would suppress it."""
    asked = []
    _run(lambda rid: asked.append(rid) or False,
         {**ARTIFACT, "kind": "updated", "summary": "merged"})
    assert asked == [SID + "-updated"]


def test_the_solo_email_is_checked_against_the_solo_key(quiet):
    asked = []
    _run(lambda rid: asked.append(rid) or False)
    assert asked == [SID]


# ---- the reader itself ----------------------------------------------------

class _S3:
    def __init__(self, body=None, error=None):
        self.body, self.error = body, error
        self.asked = []

    def get_object(self, Bucket=None, Key=None):
        self.asked.append(Key)
        if self.error:
            raise self.error
        return {"Body": _Body(json.dumps(self.body).encode("utf-8"))}


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


def _client_error(code):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


def _with_s3(monkeypatch, s3):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)


def test_a_sent_result_means_sent(monkeypatch):
    _with_s3(monkeypatch, _S3(body={"status": "sent", "sessionId": SID}))
    assert fin._already_sent(SID) is True


def test_an_error_result_does_not_count_as_sent(monkeypatch):
    """A rejected send must stay re-drivable."""
    _with_s3(monkeypatch, _S3(body={"status": "error", "error": "MessageRejected"}))
    assert fin._already_sent(SID) is False


def test_no_result_yet_is_quiet(monkeypatch, caplog):
    _with_s3(monkeypatch, _S3(error=_client_error("NoSuchKey")))
    with caplog.at_level(logging.WARNING):
        assert fin._already_sent(SID) is False
    assert caplog.records == []            # the ordinary case says nothing


def test_a_denied_read_still_sends_and_says_so(monkeypatch, caplog):
    """The 403≠404 trap, in the direction that costs a duplicate rather than a
    lost confirmation — and never silently."""
    _with_s3(monkeypatch, _S3(error=_client_error("AccessDenied")))
    with caplog.at_level(logging.WARNING):
        assert fin._already_sent(SID) is False
    assert any("could not read" in r.getMessage() for r in caplog.records)
