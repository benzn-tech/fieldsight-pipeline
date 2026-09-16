"""The confirmation email shows the record's rows, not a second summary of them.

Today the worker re-summarises the whole session at finalize -- an LLM call whose
only consumer is this email. The website, the reports and Aurora all show the
EXTRACTION's action items instead, so one meeting is summarised twice and the
same commitment can be worded one way in the email and another way on the site.
It also races: measured on prod, a 28-minute session was emailed 44 s after the
last segment while its final extraction was still being written (the last write
landed at +253 s).

So a request may now arrive WITH its rows, and this worker renders them:

  final    item-writer enqueues once the extraction's rows are durable in Aurora
  rolling  the backstop, for a session whose final extraction never arrived
  updated  the one merged summary every member of a group receives (already so)

A request with NO kind keeps today's behaviour exactly, because that is what is
in flight while the producers are being changed (spec 2026-09-16, step 1: this
lands inert -- nothing sends `final` or `rolling` yet).

The `Updated:` subject and the `-updated` result key stay keyed on `updated`
ALONE. Putting a `final` result on the `-updated` key would hide it from
reconcile, which reads `session_finalize_results/{sessionId}.json` to settle a
claimed session -- and the session would sit in `finalizing` for good, which is
the exact failure the `skipped` branch was added to fix.
"""
import pytest

fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")

SID = "c" * 32
ROWS = [{"text": "Re-inspect the cylinder", "responsible": "Neil", "due": "2026-09-18"}]


def _run(artifact):
    """Returns (result, subject, result_key, summariser_called)."""
    calls, sent, written = [], [], []

    def summariser(_artifact):
        calls.append(_artifact)
        return {"summary": "RE-SUMMARISED", "open_todos": [{"text": "FROM THE LLM"}]}

    out = fin.process_finalize_request(
        dict(artifact),
        send=lambda to, subject, text, html: sent.append((subject, text)),
        write_result=lambda sid, res: written.append(sid),
        complete_summary=summariser,
        already_sent=lambda rid: False)
    return out, (sent[0] if sent else (None, None)), (written[0] if written else None), bool(calls)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(fin, "_session_was_deleted", lambda artifact: False)


BASE = {"recipient": "ben@example.nz", "folder": "Ben_UCPK2", "date": "2026-09-16",
        "sessionId": SID, "siteName": "Ellesmere"}


def test_THE_test_a_final_request_is_rendered_as_handed(monkeypatch):
    out, (subject, text), key, summarised = _run({**BASE, "kind": "final", "openTodos": ROWS})
    assert out["status"] == "sent"
    assert not summarised, "the rows came with the request; summarising again is the old way"
    assert "Re-inspect the cylinder" in text
    assert "FROM THE LLM" not in text


def test_the_backstop_rows_are_rendered_as_handed_too():
    out, (subject, text), key, summarised = _run({**BASE, "kind": "rolling", "openTodos": ROWS})
    assert out["status"] == "sent" and not summarised
    assert "Re-inspect the cylinder" in text


def test_a_request_with_no_kind_still_re_summarises():
    """Today's path, unchanged: this lands inert."""
    out, (subject, text), key, summarised = _run({**BASE, "openTodos": ROWS})
    assert summarised, "nothing enqueues a kind yet, so this path must not change"
    assert "FROM THE LLM" in text


def test_a_final_email_is_not_dressed_as_an_updated_one():
    """The subject rewrite and the -updated result key belong to the merge alone.
    A final result on the -updated key is invisible to reconcile, and the session
    stays `finalizing` for good."""
    _out, (subject, _text), key, _s = _run({**BASE, "kind": "final", "openTodos": ROWS})
    assert not subject.startswith("Updated:")
    assert key == SID


def test_the_merged_email_keeps_its_subject_and_its_own_key():
    _out, (subject, _text), key, summarised = _run(
        {**BASE, "kind": "updated", "summary": "merged", "openTodos": ROWS})
    assert subject.startswith("Updated:")
    assert key == SID + "-updated"
    assert not summarised


def test_a_final_with_no_rows_says_so_rather_than_showing_an_empty_table():
    """11 of 16 measured prod sessions produced no action items. Those still get
    an email, and it says why it is empty."""
    _out, (_subject, text), _key, summarised = _run({**BASE, "kind": "final", "openTodos": []})
    assert not summarised
    assert "No action items" in text
