"""The device heartbeat on the org-api request path.

Three invariants, in order of how much damage their absence would do:

1. A request with device headers records a heartbeat.
2. A request without them records nothing — the overwhelming majority of
   traffic is the dashboard, which must not touch the devices table at all.
3. A failing heartbeat never fails the request. This code sits on the hot path
   of every dashboard call; telemetry is not worth a 500.
"""

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-08-04",
}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def make_event(headers):
    return {
        "httpMethod": "GET",
        "path": "/api/org/me",
        "queryStringParameters": None,
        "body": None,
        "headers": headers,
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(org, "get_me", lambda conn, caller: org.ok({"me": caller["id"]}))
    recorded = []
    monkeypatch.setattr(org.device_heartbeat, "record",
                        lambda conn, ident, sub: recorded.append((ident, sub)))
    return recorded


def test_records_a_heartbeat_when_device_headers_are_present(wired):
    org.lambda_handler(make_event({"X-Device-Tag": "FS-07", "X-App-Version": "1.4.2"}), None)

    assert len(wired) == 1
    ident, sub = wired[0]
    assert ident["asset_tag"] == "FS-07"
    assert ident["app_version"] == "1.4.2"
    assert sub == "sub-1"


def test_dashboard_traffic_carries_no_device_identity(wired):
    """The overwhelming majority of requests are the dashboard. record() is
    called unconditionally and no-ops on None, so what matters is that nothing
    identifiable reaches it — not that the call is skipped."""
    org.lambda_handler(make_event({}), None)
    assert [ident for ident, _ in wired] == [None]


def test_absent_headers_key_is_handled(wired):
    event = make_event({})
    del event["headers"]
    org.lambda_handler(event, None)
    assert [ident for ident, _ in wired] == [None]


def test_the_request_still_succeeds_with_device_headers(wired):
    res = org.lambda_handler(make_event({"X-Device-Tag": "FS-07"}), None)
    assert res["statusCode"] == 200


def test_an_unprovisioned_account_still_reports_its_device(monkeypatch):
    """A device whose account is not in Aurora yet must still show as alive —
    otherwise it is indistinguishable from a device nobody switched on, which
    is exactly the alert this ledger exists to raise."""
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: None)
    recorded = []
    monkeypatch.setattr(org.device_heartbeat, "record",
                        lambda conn, ident, sub: recorded.append(ident))

    res = org.lambda_handler(make_event({"X-Device-Tag": "FS-07"}), None)

    assert res["statusCode"] == 403          # caller guard still rejects
    assert recorded[0]["asset_tag"] == "FS-07"  # but the device was recorded
