"""The three org-api endpoints that read ONE lake key, and the state they all got wrong.

Each reads a single object, treats a missing one as `pending` — "not written yet" and
"does not exist" are the same thing to a caller — and re-raises anything else. All three
were correct. All three answered **500** in production for the state they call `pending`,
because `session_brief/`, `session_rolling/` and `session_report_results/` had `GetObject`
and no `ListBucket`, and without `ListBucket` S3 answers `AccessDenied` rather than
`NoSuchKey` for a key that was never written.

Measured, not reasoned: `simulate-principal-policy` against the live prod role returned
GetObject=allowed / ListBucket=implicitDeny for all three, and both endpoints still on the
old grant answered 500 on prod for a real session.

So the fix is in `template.yaml`, and what these tests hold is the two properties that make
the fix the RIGHT one and keep it that way:

1. a missing key is `pending`, not an error — the branch the grant makes reachable;
2. `AccessDenied` still RAISES. Widening the except is the tempting one-line alternative and
   it is the trap: it reports a real permission failure as "nothing here yet", which is the
   disguise this defect already wore for as long as it existed, and it would make the next
   missing grant silent instead of loud.

Unit tests cannot see an IAM policy. These pin the code either side of it; the grant itself
is verified by simulate-principal-policy after deploy.
"""
import json

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

from botocore.exceptions import ClientError  # noqa: E402

CALLER = {"id": "u-1", "company_id": "c-1", "global_role": "admin"}
SESSION = "sid" + "b" * 32


def _event(**extra):
    params = {"date": "2026-08-31", "user": "Ben_UCPK2"}
    params.update(extra)
    return {"queryStringParameters": params}


def _wire(monkeypatch, code=None, stored=None):
    """An S3 that either raises `code` or returns `stored`."""
    payload = json.dumps(stored or {}).encode("utf-8")

    class _Body:
        def read(self):
            return payload

    class _S3:
        def get_object(self, **kw):
            if code:
                raise ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")
            return {"Body": _Body()}

        def generate_presigned_url(self, *a, **kw):
            return "https://example.invalid/doc"

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ben_UCPK2", None))
    # The deletion mirror reads CLEAN, always. The brief route now reads it
    # before the brief itself, and leaving it to the failing `_S3` above made the
    # mirror raise first -- which shielded the very guard below from ever being
    # reached. The test stayed green and stopped testing what it says: exactly
    # the shape it was written to prevent, one layer up. Stubbed separately so a
    # fault on the BRIEF is the only thing these cases can be measuring.
    import deletion_mirror

    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict",
                        lambda s3, bucket, folder, date: set())


# The three handlers, and the query params each needs beyond date/user.
READERS = [
    ("brief", lambda: oa.session_brief_read(None, CALLER, SESSION, _event())),
    ("rolling", lambda: oa.session_rolling(None, CALLER, SESSION, _event())),
    ("report_status", lambda: oa.session_report_status(
        None, CALLER, SESSION, _event(requestId="r-1"))),
]


@pytest.mark.parametrize("name,call", READERS, ids=[r[0] for r in READERS])
def test_a_key_that_was_never_written_is_pending(monkeypatch, name, call):
    _wire(monkeypatch, code="NoSuchKey")
    resp = call()
    assert resp["statusCode"] == 200, f"{name} did not answer 200 for a missing key"
    assert json.loads(resp["body"])["status"] == "pending"


@pytest.mark.parametrize("name,call", READERS, ids=[r[0] for r in READERS])
def test_access_denied_still_raises(monkeypatch, name, call):
    """The fix is the ListBucket grant in template.yaml, never a wider except here.

    If this goes red, someone caught AccessDenied to make a 500 go away. The 500 was
    telling the truth: the role could not tell a missing object from a forbidden one.
    Look at the `s3:prefix` condition on the OrgApiFunction ListBucket statement.
    """
    _wire(monkeypatch, code="AccessDenied")
    with pytest.raises(ClientError):
        call()
