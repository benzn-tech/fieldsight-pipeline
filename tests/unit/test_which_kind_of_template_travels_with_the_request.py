"""Whether a template was reviewed by us travels WITH the request.

The renderer treats a customer's section plan differently from one that ships as
a file in this repo. That only works if it can tell them apart, and by the time
the worker sees them it cannot: org-api inlines both into the artifact, in the
same shape, precisely so there is one path rather than two.

Nor can the body say. A Library body is whatever JSON passed validation, and
validation does not strip keys it does not know: a customer who wants their
template treated as reviewed need only type one into it. A marker a customer can
write is not a marker.

What does know is the branch org-api took -- a uuid it resolved out of the
database, or a slug it loaded off disk. So that is where it is stated, and this
file pins the statement at both ends: org-api says it, and the worker reads it.

THE test is `an artifact with no source at all is treated as customer-written`.
The two mistakes are not the same size. Fencing a reviewed template costs a
fence around text that did not need one. Not fencing a customer's hands
unreviewed text the instruction layer -- and the artifacts this applies to are
the handful already in the bucket when this deploys, which is exactly when
nobody is watching for it.
"""
import datetime as dt
import json

import pytest

import report_template as rt
import lambda_session_report as sr

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

COMPANY = "c0000000-0000-0000-0000-000000000001"
TPL = "aaaaaaaa-1111-2222-3333-444444444444"
GM = {"id": "u-gm", "company_id": COMPANY, "global_role": "gm", "cognito_sub": "s-gm"}

BODY = {"sections": [{"key": "what", "title": "What this was", "purpose": "Whose meeting."}],
        "catch_all": {"key": "other", "title": "Anything else", "purpose": "Rest."},
        "excluded_subjects": [], "style": []}


class _Repo:
    def get_visible(self, conn, company_id, user_id, template_id):
        return {"id": TPL, "company_id": company_id, "name": "Site Daily",
                "scope": "org", "current_version": 1}

    def get_version(self, conn, template_id, version):
        return {"version": 1, "body": BODY}


@pytest.fixture()
def repo(monkeypatch):
    monkeypatch.setattr(oa, "report_templates", _Repo())


# ---- org-api says which branch it took --------------------------------------

def test_a_library_template_is_marked_as_customer_written(repo):
    gen, err = oa._generation_request({"templateId": TPL}, None, object(), GM)
    assert err is None
    assert gen["templateSource"] == rt.SOURCE_LIBRARY


def test_a_file_template_is_marked_as_reviewed(repo):
    gen, err = oa._generation_request(
        {"templateId": "personal-meeting", "templateVersion": 3}, None, object(), GM)
    assert err is None
    assert gen["templateSource"] == rt.SOURCE_BUILTIN


def test_a_customer_cannot_put_the_marker_in_their_own_body(repo):
    """The body is stored as given. If the renderer read identity out of it,
    this is all it would take."""
    forged = json.loads(json.dumps(BODY))
    forged["templateSource"] = rt.SOURCE_BUILTIN
    forged["template_id"] = "personal-meeting"
    assert rt.validate_body(forged) is None, "unknown keys are not refused, and never were"

    p = rt.render_prompt(forged, SCOPE, [], "x", source=rt.SOURCE_LIBRARY)
    assert rt.FENCE_BEGIN in p, "the caller decides, not the body"


SCOPE = {"folder": "Ben_UCPK2", "date": "2026-09-10", "from": "09:00", "to": "11:30",
         "recordings": 1}


# ---- the worker reads it ----------------------------------------------------

ARTIFACT = {
    "requestId": "r1", "folder": "Ben_UCPK2", "date": "2026-09-10",
    "sessionId": "sid" + "a" * 32, "resultKey": "session_report_results/x.json",
    "title": "Meeting Notes", "deliver": "download",
    "generate": {"templateId": TPL, "templateVersion": 1, "templateName": "Site Daily",
                 "templateBody": BODY},
    "window": {"from": "09:00", "to": "11:30"},
    "excludedTopics": [],
    "content": {"date": "2026-09-10", "participants": ["Ben"], "topics": []},
}


@pytest.fixture
def prompt_of(monkeypatch):
    seen = {}
    monkeypatch.setattr(sr.transcript_window, "select_keys",
                        lambda *a, **k: [(dt.datetime(2026, 9, 10, 9, 5), "transcripts/x.json")])
    monkeypatch.setattr(sr.transcript_window, "assemble",
                        lambda *a, **k: [{"at": dt.datetime(2026, 9, 10, 9, 5),
                                          "line": "[09:05:00] Ben: roofing"}])
    monkeypatch.setattr(sr.llm_utils, "call_llm",
                        lambda prompt, **kw: (seen.update(prompt=prompt) or
                                              ("### What this was\nBen's meeting.\n", None)))
    monkeypatch.setattr(sr.llm_utils, "active_model", lambda **k: "muse-spark-1.3")
    monkeypatch.setattr(sr.lambda_meeting_minutes, "generate_prose_document",
                        lambda *a, **k: __import__("io").BytesIO(b"PK-docx"))
    monkeypatch.setattr(sr, "_write_result", lambda key, payload: None)
    monkeypatch.setattr(sr, "_put_document", lambda *a, **k: "session_reports/x.docx")
    monkeypatch.setattr(sr, "_session_was_deleted", lambda artifact: False)

    def run(gen_over):
        art = json.loads(json.dumps(ARTIFACT))
        art["generate"].update(gen_over)
        for k, v in list(art["generate"].items()):
            if v is None:
                del art["generate"][k]
        sr.process_request(art)
        return seen["prompt"]
    return run


# ---- THE test ---------------------------------------------------------------

def test_THE_test_an_artifact_with_no_source_is_treated_as_customer_written(prompt_of):
    """Enqueued before org-api started saying it, still in the bucket when this
    deploys. It carries an inlined body and nothing about where it came from."""
    assert rt.FENCE_BEGIN in prompt_of({"templateSource": None})


def test_the_worker_fences_what_org_api_called_customer_written(prompt_of):
    assert rt.FENCE_BEGIN in prompt_of({"templateSource": rt.SOURCE_LIBRARY})


def test_the_worker_does_not_fence_what_org_api_called_reviewed(prompt_of):
    p = prompt_of({"templateSource": rt.SOURCE_BUILTIN})
    assert rt.FENCE_BEGIN not in p
    assert "Whose meeting." in p, "it still renders -- only the framing differs"
