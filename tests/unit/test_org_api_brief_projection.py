"""The brief endpoint returns what the writer stored.

Its docstring says "Returned whole rather than filtered". The code returns a
five-key projection, and `summary` and `open_todos` are in every stored brief and
reach no caller — verified against the live TEST object, whose top-level keys are
headline, sections, entities, tasks, stats, summary, open_todos.

A whitelist here protects nothing. The object is this company's own brief and the
ACL was already applied to reach it; all the projection does is guarantee that the
next field added to the writer is stored forever and served never, with every
writer test green. That is this repository's green-over-a-dead-path shape, and it
was already built in before anything new arrived to fall into it.
"""
import json

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


STORED = {
    "headline": "Door replacement agreed",
    "sections": [{"title": "Doors", "bullets": []}],
    "entities": [{"name": "Two Specialists"}],
    "tasks": [{"text": "Re-inspect before Tuesday"}],
    "stats": {"unmatched": 2},
    "summary": "Doors on floors 1-3 by Tuesday.",
    "open_todos": ["Re-inspect before Tuesday"],
}

CALLER = {"id": "u-1", "company_id": "c-1", "global_role": "admin"}
EVENT = {"queryStringParameters": {"date": "2026-08-27"}}
SESSION = "sid" + "a" * 32


def _wire(monkeypatch, stored):
    payload = json.dumps(stored).encode("utf-8")

    class _Body:
        def read(self):
            return payload

    class _S3:
        def get_object(self, **kw):
            return {"Body": _Body()}

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ben_UCPK2", None))


def _read(monkeypatch, stored):
    _wire(monkeypatch, stored)
    return json.loads(oa.session_brief_read(None, CALLER, SESSION, EVENT)["body"])


def test_every_stored_field_reaches_the_caller(monkeypatch):
    body = _read(monkeypatch, STORED)

    assert body["status"] == "ready"
    for key in ("headline", "sections", "entities", "tasks", "stats",
                "summary", "open_todos"):
        assert key in body, f"the endpoint dropped {key!r}"
    assert body["summary"] == "Doors on floors 1-3 by Tuesday."
    assert body["open_todos"] == ["Re-inspect before Tuesday"]


def test_a_brief_written_before_a_field_existed_still_answers(monkeypatch):
    """Every brief on disk predates something. A missing key must read as empty,
    not raise inside a lambda whose caller sees only a 500."""
    old = {k: STORED[k] for k in ("headline", "sections", "entities", "tasks")}
    body = _read(monkeypatch, old)

    assert body["status"] == "ready"
    assert body["summary"] == ""
    assert body["open_todos"] == []
    assert body["stats"] is None


def test_the_response_is_json_serialisable(monkeypatch):
    """`ok()` marshals this; a value it cannot encode surfaces as a 500 with the
    handler looking correct — which is how this endpoint shipped 500ing for every
    session once already (#596)."""
    _wire(monkeypatch, STORED)
    resp = oa.session_brief_read(None, CALLER, SESSION, EVENT)
    assert resp["statusCode"] == 200
    json.loads(resp["body"])


def _wire_error(monkeypatch, code):
    from botocore.exceptions import ClientError

    class _S3:
        def get_object(self, **kw):
            raise ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ben_UCPK2", None))


def test_a_brief_not_written_yet_is_pending(monkeypatch):
    _wire_error(monkeypatch, "NoSuchKey")
    body = json.loads(oa.session_brief_read(None, CALLER, SESSION, EVENT)["body"])
    assert body == {"status": "pending"}


def test_access_denied_is_not_swallowed_as_pending(monkeypatch):
    """The fix for the 500 this endpoint answered is a GRANT, not a wider except.

    Without `s3:ListBucket` on `session_brief/*`, S3 answers AccessDenied rather
    than NoSuchKey for a key that was never written, so every session with no
    brief 500ed — measured on TEST and PROD, 2026-08-31. The tempting one-line fix
    is to catch AccessDenied here too. That would report a real permission failure
    as "no brief yet", which is the same disguise the missing grant already wore,
    and the next missing grant would then be invisible instead of loud.

    So this asserts the endpoint still RAISES. If someone widens the except, this
    goes red and the template's prefix list is where to look.
    """
    from botocore.exceptions import ClientError

    _wire_error(monkeypatch, "AccessDenied")
    with pytest.raises(ClientError):
        oa.session_brief_read(None, CALLER, SESSION, EVENT)
