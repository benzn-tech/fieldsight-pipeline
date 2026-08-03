"""lambda_device_report — the non-VPC scheduler.

Phase 1 ships this inert. The two behaviours worth pinning now are that an
unset token means it does absolutely nothing, and that a ledger failure
SURFACES. A silent partial run would leave a Notion table that merely looks
unchanged, which is the exact failure mode this design exists to avoid.
"""

import json

import lambda_device_report as report


class FakeLambdaClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)
        body = json.dumps(self._payload).encode()

        class Body:
            def read(self):
                return body

        return {"StatusCode": 200, "Payload": Body()}


def test_disabled_without_a_notion_token(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "")
    client = FakeLambdaClient({"devices": []})
    monkeypatch.setattr(report, "_lambda", lambda: client)

    assert report.lambda_handler({}, None) == {"status": "disabled", "devices": 0}
    assert client.calls == [], "an unset token must not even reach the ledger"


def test_invokes_the_in_vpc_ledger_when_enabled(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "secret_x")
    monkeypatch.setattr(report, "LEDGER_FUNCTION", "fieldsight-test-device-ledger")
    client = FakeLambdaClient({"devices": [{"asset_tag": "FS-07"}]})
    monkeypatch.setattr(report, "_lambda", lambda: client)

    out = report.lambda_handler({}, None)

    assert out == {"status": "ok", "devices": 1}
    assert client.calls[0]["FunctionName"] == "fieldsight-test-device-ledger"
    assert client.calls[0]["InvocationType"] == "RequestResponse"


def test_an_empty_ledger_is_reported_not_treated_as_failure(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "secret_x")
    monkeypatch.setattr(report, "_lambda", lambda: FakeLambdaClient({"devices": []}))

    assert report.lambda_handler({}, None) == {"status": "ok", "devices": 0}


def test_a_ledger_failure_raises_so_the_lambda_is_marked_failed(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "secret_x")

    class Boom:
        def invoke(self, **kw):
            raise RuntimeError("ledger unreachable")

    monkeypatch.setattr(report, "_lambda", lambda: Boom())

    try:
        report.lambda_handler({}, None)
    except RuntimeError:
        return
    raise AssertionError("a ledger failure must surface, not be swallowed")


def test_a_malformed_ledger_payload_does_not_report_phantom_devices(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "secret_x")
    monkeypatch.setattr(report, "_lambda", lambda: FakeLambdaClient({"unexpected": True}))

    assert report.lambda_handler({}, None) == {"status": "ok", "devices": 0}
