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
