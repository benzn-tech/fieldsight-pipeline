"""The worker generates from a template written in the Library, not only a file.

WHAT HAPPENED. `_generate_document` built its provenance from the template's own
text: `template["template_id"]` and `template["version"]`. The files in
report_templates/ carry those keys inside them. A template written in the
Library does not -- its body is sections, catch_all, excluded_subjects and
style, and nothing else.

So the first customer to generate a report from their own template got
KeyError('template_id'), which the worker wrote into the result as `str(e)`.
`str(KeyError('template_id'))` is `"'template_id'"`, so the screen said,
in full:

    'template_id'

and offered no download, because there was none.

WHY EVERY EXISTING WORKER TEST STAYED GREEN. All of them pass an artifact whose
generate block names `personal-meeting` and whose template is loaded from disk
-- the one shape that has those keys. The stored-template shape had never been
through this function. The endpoint that resolves a Library template was tested;
the worker that consumes one was not. That is the third time in a day that a
thing was verified at one end and not along the path.

THE test is `a Library template generates a document`. Its whole job is to send
a body with no identity fields in it.
"""
import datetime as dt

import pytest

import lambda_session_report as sr

# A body exactly as the Library stores it: no template_id, no version, no name.
LIBRARY_BODY = {
    "sections": [{"key": "what", "title": "What this was", "purpose": "Whose meeting."},
                 {"key": "actions", "title": "Actions", "purpose": "One line each."}],
    "catch_all": {"key": "other", "title": "Anything else", "purpose": "The rest."},
    "excluded_subjects": [],
    "style": ["Keep it short."],
}

TEMPLATE_UUID = "aaaaaaaa-1111-2222-3333-444444444444"

ARTIFACT = {
    "requestId": "r1", "folder": "Ben_UCPK2", "date": "2026-09-10",
    "sessionId": "sid" + "a" * 32, "resultKey": "session_report_results/x.json",
    "title": "Meeting Notes", "deliver": "download",
    "generate": {"templateId": TEMPLATE_UUID, "templateVersion": 2,
                 "templateName": "Site Daily", "templateBody": LIBRARY_BODY},
    "window": {"from": "09:00", "to": "11:30"},
    "excludedTopics": [],
    "content": {"date": "2026-09-10", "participants": ["Ben"], "topics": [
        {"topic_title": "Roofing", "summary": "Xtreme withdrew.", "time_range": "09:10 - 09:20",
         "action_items": [{"action": "Send roofing prices", "responsible": "Alex",
                           "deadline": None}]},
    ]},
}

PROSE = ("### What this was\nBen's site meeting.\n\n"
         "### Actions\n- **Alex** - send roofing prices - *no date*\n")


@pytest.fixture
def wired(monkeypatch):
    calls = {}
    monkeypatch.setattr(sr.transcript_window, "select_keys",
                        lambda *a, **k: [(dt.datetime(2026, 9, 10, 9, 5), "transcripts/x.json")])
    monkeypatch.setattr(sr.transcript_window, "assemble",
                        lambda *a, **k: [{"at": dt.datetime(2026, 9, 10, 9, 5),
                                          "line": "[09:05:00] Ben: roofing"}])

    def fake_call(prompt, **kw):
        calls["prompt"] = prompt
        return PROSE, None

    monkeypatch.setattr(sr.llm_utils, "call_llm", fake_call)
    monkeypatch.setattr(sr.llm_utils, "active_model", lambda **k: "muse-spark-1.3")
    titles = []
    monkeypatch.setattr(sr.lambda_meeting_minutes, "generate_prose_document",
                        lambda title, *a, **k: (titles.append(title)
                                                or __import__("io").BytesIO(b"PK-docx")))
    written = []
    monkeypatch.setattr(sr, "_write_result", lambda key, payload: written.append(payload))
    monkeypatch.setattr(sr, "_put_document", lambda *a, **k: "session_reports/x.docx")
    monkeypatch.setattr(sr, "_session_was_deleted", lambda artifact: False)
    calls["titles"] = titles
    return calls, written


# ---- THE test ---------------------------------------------------------------

def test_THE_test_a_library_template_generates_a_document(wired):
    """No identity fields anywhere in the body. It must still produce a report."""
    calls, written = wired
    sr.process_request(dict(ARTIFACT))
    assert written[0]["status"] == "done", written[0]
    assert written[0].get("error") is None
    assert written[0]["docKey"]


def test_the_provenance_comes_from_the_request(wired):
    """`gen` is what was ASKED for, and what the status endpoint echoes back."""
    _, written = wired
    sr.process_request(dict(ARTIFACT))
    assert written[0]["templateId"] == TEMPLATE_UUID
    assert written[0]["templateVersion"] == 2
    assert written[0]["generated"] is True


def test_the_sections_of_the_stored_template_reach_the_prompt(wired):
    """Not merely that it did not crash: the body has to be the one used."""
    calls, _ = wired
    sr.process_request(dict(ARTIFACT))
    assert "What this was" in calls["prompt"]
    assert "Whose meeting." in calls["prompt"]
    assert "Keep it short." in calls["prompt"], "the house style rides with the template"
    assert "Anything else" in calls["prompt"], "the catch_all too"


def test_the_document_is_titled_from_something_that_exists(wired):
    """`template.get("name")` is absent on a Library body; the request carries
    templateName. A document called "Report" would not be wrong, but it would
    be the second thing lost to the same assumption."""
    calls, _ = wired
    art = dict(ARTIFACT)
    art.pop("title")
    sr.process_request(art)
    assert calls["titles"][0] == "Site Daily"


# ---- the file-backed path is untouched --------------------------------------

def test_a_file_template_still_reports_its_own_id(wired, monkeypatch):
    """The fallback to the body's own fields stays, for artifacts enqueued
    before the request carried them."""
    art = dict(ARTIFACT)
    art["generate"] = {"templateId": "personal-meeting", "templateVersion": 3}
    _, written = wired
    sr.process_request(art)
    assert written[0]["templateId"] == "personal-meeting"
    assert written[0]["templateVersion"] == 3


# ---- what the screen says when it does fail ---------------------------------

def test_a_failure_is_reported_as_a_sentence_not_a_bare_key(wired, monkeypatch):
    """`str(KeyError('x'))` is `"'x'"`. A result carrying that reaches the user
    as a word in quotes and nothing else -- no cause, no next step. Whatever
    else changes, an error that escapes here must not be one token long."""
    def boom(*a, **k):
        raise KeyError("template_id")

    monkeypatch.setattr(sr.llm_utils, "call_llm", boom)
    _, written = wired
    sr.process_request(dict(ARTIFACT))
    assert written[0]["status"] == "error"
    message = written[0]["error"]
    assert len(message.split()) > 1, (
        "a one-word error tells the person nothing: %r" % message)
