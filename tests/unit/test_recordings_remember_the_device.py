"""Which physical unit recorded this.

`recordings.device_id` was added by migration 0030 and `lambda_device_ledger`
joins on it to report each device's most recent site. Nothing ever wrote it, so
that join could only ever return NULL -- the ledger's `actual_site` column has
been structurally empty since the day it shipped, and every row on prod today
reads `None`.

It stopped being cosmetic on 2026-09-02, when a device still signed in as one
account filed another site's forty-minute meeting into that account's folder.
Answering "which device made this recording" took an afternoon of
reconstructing identity out of S3 filenames, because the column built for
exactly that question was empty.

The rule these tests hold: knowing the device is worth having and is never
worth a recording. An unknown device, a device the ledger has not seen, or a
failed lookup all resolve to NULL and the upload still succeeds.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


class _NoopCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    """Records the ORDER of what the handler does, because one of the things
    being asserted is that the device lookup happens before the transaction
    opens."""

    def __init__(self, trace):
        self.trace = trace

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        self.trace.append("transaction")
        return _NoopCtx()


class FakeS3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        return "https://s3.example/" + Params["Key"]


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "global_role": "pm", "created_at": "2026-07-04", "archived_at": None}

DEVICE_HEADERS = {"X-Device-Tag": "FS-07", "X-Device-Id": "abcd1234efgh",
                  "X-App-Version": "1.4.2"}


def make_event(body, headers=None):
    return {"httpMethod": "POST", "path": "/api/org/recordings/upload-url",
            "queryStringParameters": None, "headers": headers,
            "body": json.dumps(body),
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


BODY = {"kind": "audio", "clientUuid": "cap-1", "siteId": None,
        "fileName": "a.wav", "contentType": "audio/wav",
        "startedAt": "2026-09-06T10:00:00Z"}


@pytest.fixture
def wired(monkeypatch):
    trace = []
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn(trace))
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org, "_s3_client", FakeS3())
    monkeypatch.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)
    monkeypatch.setattr(org.device_heartbeat, "record", lambda conn, ident, sub: None)
    captured = {}

    def _insert(conn, **kw):
        trace.append("insert")
        captured.update(kw)
        return {"id": "rec-1", **kw}

    monkeypatch.setattr(org.recordings, "insert_pending", _insert)
    return monkeypatch, captured, trace


# --------------------------------------------------------------------------

def test_a_recording_from_a_known_device_remembers_it(wired):
    mp, captured, _ = wired
    mp.setattr(org.device_heartbeat, "device_id", lambda conn, ident: "dev-uuid-7")
    res = org.lambda_handler(make_event(BODY, DEVICE_HEADERS), None)
    assert res["statusCode"] == 200
    assert captured["device_id"] == "dev-uuid-7"


def test_the_device_identity_reaches_the_lookup(wired):
    """The tag is the authoritative identity; the uuid is advisory and can be
    shared across a whole flashed batch. If the handler passed the wrong thing
    the column would fill with confident nonsense, which is worse than NULL."""
    mp, _, _ = wired
    seen = {}
    mp.setattr(org.device_heartbeat, "device_id",
               lambda conn, ident: seen.update(ident=ident) or None)
    org.lambda_handler(make_event(BODY, DEVICE_HEADERS), None)
    assert seen["ident"]["asset_tag"] == "FS-07"


def test_no_device_headers_means_null_and_a_successful_upload(wired):
    """The web app sends no device headers at all. NULL is the honest answer
    and must not be an error."""
    mp, captured, _ = wired
    res = org.lambda_handler(make_event(BODY, None), None)
    assert res["statusCode"] == 200
    assert captured["device_id"] is None


def test_a_failed_lookup_still_stores_the_recording(wired):
    """`device_heartbeat.device_id` swallows its own exceptions and returns
    None. Telemetry must never cost a recording."""
    mp, captured, _ = wired
    mp.setattr(org.device_heartbeat, "device_id", lambda conn, ident: None)
    res = org.lambda_handler(make_event(BODY, DEVICE_HEADERS), None)
    assert res["statusCode"] == 200 and captured["device_id"] is None


def test_the_lookup_happens_before_the_transaction_opens(wired):
    """Not a style point -- a correctness one.

    `device_id` catches its own exceptions, but a failed query still leaves the
    enclosing transaction aborted. Run inside `with conn.transaction()`, the
    swallow would turn a telemetry miss into "current transaction is aborted"
    on the INSERT and lose the upload entirely. Ordering is the only thing that
    prevents it, so ordering is what is asserted.
    """
    mp, _, trace = wired
    mp.setattr(org.device_heartbeat, "device_id",
               lambda conn, ident: trace.append("device_lookup") or "dev-1")
    org.lambda_handler(make_event(BODY, DEVICE_HEADERS), None)
    assert trace.index("device_lookup") < trace.index("transaction") < trace.index("insert")


# --------------------------------------------------------------------------
# The repository half
# --------------------------------------------------------------------------

def test_the_column_is_actually_in_the_insert():
    """A handler that passes `device_id` to a repository that drops it is the
    shape this whole change exists to correct -- a writer and a reader that
    never met. Assert the value reaches the SQL parameters."""
    recordings = pytest.importorskip("repositories.recordings")
    seen = {}

    class _Cur:
        def execute(self, sql, params):
            seen["sql"] = sql
            seen["params"] = params
            return self

        def fetchone(self):
            return {"id": "rec-1"}

    class _Conn:
        def cursor(self, **kw):
            return _Cur()

    recordings.insert_pending(_Conn(), company_id="c", user_id="u", site_id=None,
                              kind="audio", s3_key="k", client_uuid="cu",
                              started_at="2026-09-06", device_id="dev-9")
    assert "device_id" in seen["sql"]
    assert "dev-9" in seen["params"]
    # The placeholder count must match the column count, or Postgres rejects
    # the statement at runtime with nothing in the unit suite to catch it.
    cols = seen["sql"].split("INSERT INTO recordings (")[1].split(")")[0]
    assert len(cols.split(",")) == seen["sql"].count("%s")
