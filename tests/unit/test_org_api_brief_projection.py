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
    """The BRIEF read fails with `code`; the deletion mirror reads clean.

    The mirror is stubbed separately on purpose. Both reads go through the same
    `s3()` client, so a stub that failed every get_object could not tell a
    permission fault on the brief from one on the mirror -- and those have
    deliberately opposite postures (the mirror fails closed, the brief raises).
    A single stub would have made this file pass either way.
    """
    from botocore.exceptions import ClientError

    import deletion_mirror

    class _S3:
        def get_object(self, **kw):
            raise ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict",
                        lambda s3, bucket, folder, date: set())
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


def test_a_field_added_to_the_writer_tomorrow_is_served_without_an_edit_here(monkeypatch):
    """The structural version of the two tests above, and the reason they were
    not enough.

    Widening the whitelist by hand fixed `summary` and `open_todos` and left a
    comment saying the NEXT field added to the writer would be stored forever and
    served never. `open_points` was added to the writer within the hour and was
    not added to the list. A rule that must be remembered at every future edit is
    not a rule, so this asserts the property instead of the field list.
    """
    stored = dict(STORED, open_points=[{"quote": "I think it's 150", "kind": "standard"}],
                  something_invented_next_week={"a": 1})
    body = _read(monkeypatch, stored)

    missing = [k for k in stored if k not in body]
    assert not missing, f"the endpoint dropped {missing}"


def test_status_is_the_endpoints_own_and_cannot_be_overwritten(monkeypatch):
    """`status` is the one key this endpoint adds. A brief that happened to carry
    its own `status` must not be able to tell the caller it is pending."""
    body = _read(monkeypatch, dict(STORED, status="pending"))
    assert body["status"] == "ready"


# ==========================================================================
# a deleted recording's brief must not still be served
# ==========================================================================

def _wire_with_mirror(monkeypatch, stored, deleted, bucket_seen=None, strict_raises=False):
    """S3 double serving both the brief object and the deletion mirror, recording
    which bucket each was read from."""
    import deletion_mirror

    class _S3:
        def get_object(self, **kw):
            if bucket_seen is not None:
                bucket_seen.setdefault("brief", kw.get("Bucket"))

            class _B:
                def read(self):
                    return json.dumps(stored).encode("utf-8")
            return {"Body": _B()}

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ben_UCPK2", None))

    def fake_strict(s3, bucket, folder, date):
        if bucket_seen is not None:
            bucket_seen["mirror"] = bucket
        if strict_raises:
            raise deletion_mirror.MirrorUnreadable("boom")
        return set(deleted)

    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict", fake_strict)


def test_a_deleted_sessions_brief_is_not_served(monkeypatch):
    """The brief is a frozen copy of verbatim transcript quotes with a live
    endpoint, and `session_brief/` is nowhere in the deletion machinery. Deleting
    a recording hides its chunks and its topics; the brief kept answering.
    """
    _wire_with_mirror(monkeypatch, STORED, deleted={SESSION})
    body = json.loads(oa.session_brief_read(None, CALLER, SESSION, EVENT)["body"])
    assert body == {"status": "removed"}


def test_the_bare_hex_spelling_is_matched_too(monkeypatch):
    """Two spellings of a session are equal as sessions and not as strings. The
    mirror carries whatever `sessionBase` the delete endpoint had; the brief key
    is always `sid{hex}`."""
    bare = SESSION[3:]
    _wire_with_mirror(monkeypatch, STORED, deleted={bare})
    body = json.loads(oa.session_brief_read(None, CALLER, SESSION, EVENT)["body"])
    assert body == {"status": "removed"}


def test_another_sessions_deletion_does_not_hide_this_one(monkeypatch):
    """A day with one deleted recording must not blank every brief on it."""
    _wire_with_mirror(monkeypatch, STORED, deleted={"sid" + "b" * 32})
    body = json.loads(oa.session_brief_read(None, CALLER, SESSION, EVENT)["body"])
    assert body["status"] == "ready"


def test_the_mirror_is_read_from_the_bucket_it_is_written_to(monkeypatch):
    """S3_BUCKET and LAKE_BUCKET are two variables that happen to hold the same
    value today. The mirror is WRITTEN to S3_BUCKET (`delete_recordings_endpoint`)
    and the brief is READ from LAKE_BUCKET, so reading the mirror from the brief's
    bucket would work until the day they diverge and then hide nothing, silently.
    """
    seen = {}
    monkeypatch.setattr(oa, "S3_BUCKET", "bucket-where-the-mirror-lives")
    monkeypatch.setattr(oa, "LAKE_BUCKET", "bucket-where-the-brief-lives")
    _wire_with_mirror(monkeypatch, STORED, deleted=set(), bucket_seen=seen)

    oa.session_brief_read(None, CALLER, SESSION, EVENT)

    assert seen["mirror"] == "bucket-where-the-mirror-lives"
    assert seen["brief"] == "bucket-where-the-brief-lives"


def test_an_unreadable_mirror_is_loud_and_serves_nothing(monkeypatch):
    """Opposite trade to `lambda_ask_agent`, deliberately -- and not merely
    fail-closed.

    There the lenient read is right: an unreadable mirror must not take the
    nightly report down for everyone, and the cost is one day staying visible
    until a retry. Here the request is one brief on demand, so failing it is
    cheap and serving a deleted one is not.

    A first version answered "removed" instead of raising. That is fail-closed
    and still wrong: "we checked and it is removed" and "we could not check" are
    different facts, and reporting the second as the first turns a broken grant
    on `redactions/` into a silent total outage wearing the costume of normal
    operation -- the exact disguise this endpoint already shipped once, when a
    missing ListBucket made AccessDenied read as `pending` (#627).
    """
    import deletion_mirror

    _wire_with_mirror(monkeypatch, STORED, deleted=set(), strict_raises=True)
    with pytest.raises(deletion_mirror.MirrorUnreadable):
        oa.session_brief_read(None, CALLER, SESSION, EVENT)
