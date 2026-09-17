"""handoff-sync plan §2.1/§2.2: the brief request the claim step now enqueues
alongside the final extraction, and the final email's brief poll.

§2.1: `kind == "brief"` is a production request, not a delivery. It produces
and stores a brief (via `_complete_summary`) and touches none of the delivery
machinery: no recipient required, no email, no session_finalize_results/
write, no `_already_sent` check.

§2.2: for `kind == "final"` with SESSION_BRIEF on, the worker polls for the
brief that request's sibling (§2.1) asked for CONCURRENTLY, and -- if one
with at least one task turns up within the wait -- renders its tasks as the
ACTION rows, keeping the request's own TOPIC rows. Otherwise the request's
rows are used unchanged. The reader and the sleep are both injectable so
these tests never sleep and never touch S3.
"""
import pytest

fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")

SID = "abc123"
BRIEF_ARTIFACT = {"kind": "brief", "sessionId": SID, "folder": "Ben_Lin", "date": "2026-09-16"}


# ---- §2.1: process_finalize_request(kind="brief") --------------------------

def test_a_brief_request_needs_no_recipient(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    out = fin.process_finalize_request(
        {**BRIEF_ARTIFACT, "recipient": None},
        complete_summary=lambda a: {"summary": "s", "open_todos": []})
    assert out["status"] == "ok"


def test_a_brief_request_sends_no_email(monkeypatch):
    sent = []
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    out = fin.process_finalize_request(
        BRIEF_ARTIFACT, send=lambda *a: sent.append(a),
        complete_summary=lambda a: {"summary": "s", "open_todos": [], "sections": [1]})
    assert out["status"] == "ok" and sent == []


def test_a_brief_request_writes_no_finalize_result(monkeypatch):
    written = []
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    fin.process_finalize_request(
        BRIEF_ARTIFACT, write_result=lambda *a: written.append(a),
        complete_summary=lambda a: {"summary": "s", "open_todos": []})
    assert written == []


def test_a_brief_request_skips_the_already_sent_check(monkeypatch):
    """`_already_sent` is a delivery guard; a brief request is not a delivery."""
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    called = []
    fin.process_finalize_request(
        BRIEF_ARTIFACT, already_sent=lambda rid: called.append(rid) or True,
        complete_summary=lambda a: {"summary": "s", "open_todos": []})
    assert called == []


def test_a_brief_request_is_skipped_when_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", False, raising=False)
    called = []
    out = fin.process_finalize_request(
        BRIEF_ARTIFACT, complete_summary=lambda a: called.append(1))
    assert out == {"status": "skipped", "reason": "SESSION_BRIEF off", "sessionId": SID}
    assert called == [], "no LLM call when the flag is off"


def test_a_brief_request_is_skipped_for_a_deleted_session(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    monkeypatch.setattr(fin, "_session_was_deleted", lambda a: True)
    called = []
    out = fin.process_finalize_request(
        BRIEF_ARTIFACT, complete_summary=lambda a: called.append(1))
    assert out == {"status": "skipped", "reason": "recording deleted", "sessionId": SID}
    assert called == []


def test_a_brief_request_that_produces_nothing_is_skipped_not_errored(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    out = fin.process_finalize_request(BRIEF_ARTIFACT, complete_summary=lambda a: None)
    assert out == {"status": "skipped", "reason": "no brief produced", "sessionId": SID}


def test_a_brief_request_is_checked_before_the_recipient(monkeypatch):
    """kind == "brief" must win even though `recipient` here would otherwise
    read as 'skip: no recipient' -- proving the ordering, not just the outcome."""
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    out = fin.process_finalize_request(
        {**BRIEF_ARTIFACT, "recipient": ""},
        complete_summary=lambda a: {"summary": "s", "open_todos": []})
    assert out["status"] == "ok"


# ---- §2.2: the reader distinguishes "not ready" from a real failure --------

def _client_error(code, status):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "GetObject")


class _S3Get:
    def __init__(self, error=None, body=None):
        self.error, self.body = error, body

    def get_object(self, **kw):
        if self.error:
            raise self.error
        import io
        return {"Body": io.BytesIO(self.body)}


def _with_s3(monkeypatch, s3):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)


def test_a_missing_brief_reads_as_not_ready_yet(monkeypatch):
    _with_s3(monkeypatch, _S3Get(error=_client_error("NoSuchKey", 404)))
    assert fin._read_brief("Ben_Lin", "2026-09-16", SID) is None


def test_a_404_status_with_no_code_also_reads_as_not_ready_yet(monkeypatch):
    _with_s3(monkeypatch, _S3Get(error=_client_error("", 404)))
    assert fin._read_brief("Ben_Lin", "2026-09-16", SID) is None


def test_an_access_denied_is_a_loud_failure_not_silence(monkeypatch, caplog):
    """CLAUDE.md: a 403 read as 'absent' is exactly how the S3-permission trap
    keeps shipping. This must be LOGGED, at ERROR, distinctly from NoSuchKey."""
    _with_s3(monkeypatch, _S3Get(error=_client_error("AccessDenied", 403)))
    with caplog.at_level("ERROR"):
        result = fin._read_brief("Ben_Lin", "2026-09-16", SID)
    assert result is None       # the poll still treats it as absent THIS attempt
    assert "READ FAILURE" in caplog.text
    assert "AccessDenied" in caplog.text or "403" in caplog.text


def test_a_present_brief_is_returned(monkeypatch):
    import json as _json
    _with_s3(monkeypatch, _S3Get(body=_json.dumps({"tasks": [{"text": "x"}]}).encode()))
    assert fin._read_brief("Ben_Lin", "2026-09-16", SID) == {"tasks": [{"text": "x"}]}


# ---- §2.2: the poll retries then gives up, both injectable -----------------

def test_the_poll_returns_the_first_brief_it_sees():
    calls = []
    reader = lambda *a: {"tasks": []}
    out = fin._poll_for_brief("F", "D", SID, read_brief=reader,
                              sleep=lambda s: calls.append(s))
    assert out == {"tasks": []} and calls == []


def test_the_poll_retries_until_the_brief_appears():
    attempts = {"n": 0}

    def reader(*a):
        attempts["n"] += 1
        return {"tasks": [1]} if attempts["n"] >= 3 else None

    slept = []
    out = fin._poll_for_brief("F", "D", SID, read_brief=reader,
                              sleep=lambda s: slept.append(s),
                              poll_seconds=10, wait_seconds=90)
    assert out == {"tasks": [1]}
    assert attempts["n"] == 3
    assert slept == [10, 10]          # sleeps between attempts, not after the last


def test_the_poll_gives_up_after_the_wait_budget():
    reader = lambda *a: None
    slept = []
    out = fin._poll_for_brief("F", "D", SID, read_brief=reader,
                              sleep=lambda s: slept.append(s),
                              poll_seconds=10, wait_seconds=30)
    assert out is None
    # ceil(30/10) + 1 read-slots -> 4 attempts, 3 sleeps between them
    assert len(slept) == 3


def test_the_poll_never_sleeps_a_real_amount_of_time(monkeypatch):
    """The default `sleep` is real time.sleep -- a test that forgets to inject
    one must not actually wait. Proven by patching module-level time.sleep and
    confirming it is what gets called when the caller supplies nothing."""
    calls = []
    monkeypatch.setattr(fin.time, "sleep", lambda s: calls.append(s))
    fin._poll_for_brief("F", "D", SID, read_brief=lambda *a: None,
                        poll_seconds=1, wait_seconds=1)
    assert calls, "the real time.sleep was called (and patched here so the test is instant)"


# ---- §2.2: which rows the FINAL email renders -------------------------------

REQUEST_TODOS = [
    {"text": "Order steel", "responsible": "Neil", "due": "Friday", "kind": "action"},
    {"text": "Site walk — poured the slab.", "responsible": None, "due": None, "kind": "topic"},
]


def _final_artifact(**over):
    a = {"kind": "final", "sessionId": SID, "folder": "Ben_Lin", "date": "2026-09-16",
         "recipient": "bob@site.com", "openTodos": list(REQUEST_TODOS)}
    a.update(over)
    return a


def test_the_flag_off_keeps_the_requests_rows(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", False, raising=False)
    sent = []
    fin.process_finalize_request(
        _final_artifact(), send=lambda *a: sent.append(a),
        write_result=lambda *a: None,
        poll_brief=lambda *a: pytest.fail("must not poll when the flag is off"))
    _to, _subj, text, _html = sent[0]
    assert "Order steel" in text and "Neil" in text


def test_no_brief_within_the_wait_keeps_the_requests_rows(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    sent = []
    fin.process_finalize_request(
        _final_artifact(), send=lambda *a: sent.append(a),
        write_result=lambda *a: None, poll_brief=lambda *a: None)
    _to, _subj, text, _html = sent[0]
    assert "Order steel" in text and "Neil" in text


def test_a_brief_with_zero_tasks_is_treated_as_no_brief(monkeypatch):
    """Plan §1.3: an empty table where the extraction had rows would read as
    broken -- the row-count floor is gone, but zero tasks still falls back."""
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    sent = []
    fin.process_finalize_request(
        _final_artifact(), send=lambda *a: sent.append(a),
        write_result=lambda *a: None,
        poll_brief=lambda *a: {"tasks": [], "open_todos": []})
    _to, _subj, text, _html = sent[0]
    assert "Order steel" in text            # the request's action row survives


def test_a_brief_whose_tasks_are_all_blank_text_falls_back_like_zero_tasks(monkeypatch):
    """Fix round on 3237e56, finding 1: the gate used to read raw `tasks`
    (non-empty) while the rows it RENDERED came from `open_todos`, which
    session_brief.to_session_summary strips of any blank-text task. A brief
    with one task whose text is blank/whitespace-only passed the old gate,
    then rendered ZERO action rows -- discarding the request's real ones.
    Mutation: put the `tasks`-non-empty gate back -> red."""
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief = {"tasks": [{"text": "   ", "assignee": "Sam", "due": "Mon"}],
             "open_todos": []}          # to_session_summary already dropped it
    sent = []
    fin.process_finalize_request(
        _final_artifact(), send=lambda *a: sent.append(a),
        write_result=lambda *a: None, poll_brief=lambda *a: brief)
    _to, _subj, text, _html = sent[0]
    assert "Order steel" in text          # the request's real action row survives
    assert "Sam" not in text              # nothing rendered from the blank task


def test_a_brief_with_tasks_replaces_the_action_rows_but_keeps_topics(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief = {"tasks": [{"text": "Chase the delivery", "assignee": "Sam", "due": "Mon", "at": "09:10:00"}],
             "open_todos": [{"text": "Chase the delivery", "responsible": "Sam",
                             "due": "Mon", "at": "09:10:00"}]}
    sent = []
    fin.process_finalize_request(
        _final_artifact(), send=lambda *a: sent.append(a),
        write_result=lambda *a: None, poll_brief=lambda *a: brief)
    _to, _subj, text, _html = sent[0]
    assert "Chase the delivery" in text and "Sam" in text and "Mon" in text
    assert "Order steel" not in text                 # the request's action row is GONE
    assert "Site walk" in text, "the request's TOPIC row is kept"


def test_a_speaker_label_assignee_in_the_brief_renders_as_unassigned(monkeypatch):
    """§1.4/§2.2: `spk_0` is not a name. session_brief.to_session_summary already
    filters this via `_real_name`; this proves the email path actually uses that
    filtered `open_todos`, not the raw `tasks[]` assignee."""
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief = {"tasks": [{"text": "Fix the gate", "assignee": "spk_0", "due": None}],
             "open_todos": [{"text": "Fix the gate", "responsible": None, "due": None}]}
    sent = []
    fin.process_finalize_request(
        _final_artifact(), send=lambda *a: sent.append(a),
        write_result=lambda *a: None, poll_brief=lambda *a: brief)
    _to, _subj, text, _html = sent[0]
    assert "Fix the gate" in text and "spk_0" not in text


def test_missing_folder_or_date_skips_the_poll_and_keeps_the_requests_rows(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    sent = []
    fin.process_finalize_request(
        _final_artifact(folder=None), send=lambda *a: sent.append(a),
        write_result=lambda *a: None,
        poll_brief=lambda *a: pytest.fail("must not poll without a folder"))
    _to, _subj, text, _html = sent[0]
    assert "Order steel" in text


def test_rolling_and_updated_kinds_never_poll_for_a_brief(monkeypatch):
    """Only `final` polls. `rolling`/`updated` already carry the rows the
    backstop/merge decided on, and re-deriving them from a solo brief would be
    exactly the disagreement #856-#858 removed."""
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    sent = []
    fin.process_finalize_request(
        _final_artifact(kind="rolling"), send=lambda *a: sent.append(a),
        write_result=lambda *a: None,
        poll_brief=lambda *a: pytest.fail("rolling must not poll"))
    assert sent
