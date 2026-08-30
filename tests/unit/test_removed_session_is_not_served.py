"""A removed recording must not be served by ANY of the three session endpoints.

`#635` found that a deleted recording's brief kept answering: `session_brief/` is nowhere in
the deletion machinery, so deleting a recording hid its chunks and topics while the endpoint
served a frozen copy of the same verbatim quotes. It fixed the brief.

`/rolling` and `/report/status` sit in the same file, take the same ACL, and serve the same
meeting's words — the rolling summary is built from the same turns, and the report status
hands out a presigned URL to a generated document of the whole session. Hiding one of three
copies hides nothing.

So the guard is one function with three callers, and this is the test that says so. It is
parametrised rather than written three times, because a rule written three times is how this
repository shipped `#603` — the same homogeneity check in three places, two of them fixed,
and nothing changed.

The presign matters more than the summary: a presigned URL outlives the check that produced
it, so refusing after issuing one would be no refusal at all. That case asserts no URL is
ever built, not merely that the response says "removed".
"""
import json

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
import deletion_mirror  # noqa: E402

CALLER = {"id": "u-1", "company_id": "c-1", "global_role": "admin"}
BARE = "b" * 32
SID = "sid" + BARE


def _event(**extra):
    params = {"date": "2026-08-31", "user": "Ben_UCPK2", "requestId": "r-1"}
    params.update(extra)
    return {"queryStringParameters": params}


class _Presigned(Exception):
    """Raised if anything tries to mint a URL for a removed session."""


def _wire(monkeypatch, removed, stored=None):
    payload = json.dumps(stored or {"status": "done", "docKey": "k.docx"}).encode("utf-8")

    class _Body:
        def read(self):
            return payload

    class _S3:
        def get_object(self, **kw):
            return {"Body": _Body()}

        def generate_presigned_url(self, *a, **kw):
            raise _Presigned("a removed session must never reach a presign")

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ben_UCPK2", None))
    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict",
                        lambda s3, bucket, folder, date: set(removed))


READERS = [
    ("brief", lambda sid: oa.session_brief_read(None, CALLER, sid, _event())),
    ("rolling", lambda sid: oa.session_rolling(None, CALLER, sid, _event())),
    ("report_status", lambda sid: oa.session_report_status(None, CALLER, sid, _event())),
]
IDS = [r[0] for r in READERS]


@pytest.mark.parametrize("name,call", READERS, ids=IDS)
@pytest.mark.parametrize("mirror_spelling", [BARE, SID],
                         ids=["mirror-holds-bare", "mirror-holds-sid"])
@pytest.mark.parametrize("asked_as", [BARE, SID], ids=["asked-bare", "asked-sid"])
def test_a_removed_session_is_not_served(monkeypatch, name, call, mirror_spelling, asked_as):
    """Both spellings, both directions.

    The mirror carries whatever `sessionBase` the delete endpoint had; the brief's key is
    always `sid{hex}` while the other two take the id as the caller wrote it. Two spellings
    of a session are equal as sessions and not as strings, and a guard that compares only one
    of them serves the recording whenever the two disagree.
    """
    _wire(monkeypatch, removed={mirror_spelling})
    resp = call(asked_as)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"status": "removed"}, (
        f"{name} served a removed session (mirror held {mirror_spelling[:6]}…, "
        f"asked as {asked_as[:6]}…)")


@pytest.mark.parametrize("name,call", READERS, ids=IDS)
def test_a_session_that_was_not_removed_is_still_served(monkeypatch, name, call):
    """The guard must not be a blanket refusal — a green test suite over a dead endpoint is
    this repository's most common shape, and 'nothing is ever served' would pass the test
    above on all three."""
    _wire(monkeypatch, removed=set(), stored={"status": "error", "error": "boom",
                                              "summary": "x", "headline": "h"})
    resp = call(SID)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] != "removed"


def test_the_report_presign_is_never_reached_for_a_removed_session(monkeypatch):
    """A presigned URL outlives the check that produced it. `_Presigned` is raised by the
    stub if the handler mints one, so this fails loudly rather than reporting `removed` while
    a live URL was already handed out."""
    _wire(monkeypatch, removed={SID}, stored={"status": "done", "docKey": "k.docx"})
    resp = oa.session_report_status(None, CALLER, SID, _event())
    assert json.loads(resp["body"]) == {"status": "removed"}


def test_an_unreadable_mirror_raises_rather_than_answering_removed(monkeypatch):
    """"We checked and it is removed" and "we could not check" are different facts.

    Reporting the second as the first turns a broken grant on `redactions/` into a silent
    total outage that looks like normal operation — which is the disguise these very
    endpoints wore until #627 (AccessDenied read as `pending`).
    """
    def _boom(*a, **kw):
        raise RuntimeError("mirror unreadable")

    _wire(monkeypatch, removed=set())
    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict", _boom)
    for _, call in READERS:
        with pytest.raises(RuntimeError):
            call(SID)
