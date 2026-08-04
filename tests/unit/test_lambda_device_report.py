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
    monkeypatch.setattr(report.notion_client, "list_rows", lambda t, d: [])
    monkeypatch.setattr(report.device_notify, "push", lambda *a, **k: None)

    out = report.lambda_handler({}, None)

    assert client.calls[0]["FunctionName"] == "fieldsight-test-device-ledger"
    assert client.calls[0]["InvocationType"] == "RequestResponse"


def test_an_empty_ledger_is_reported_not_treated_as_failure(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "secret_x")
    monkeypatch.setattr(report, "_lambda", lambda: FakeLambdaClient({"devices": []}))
    monkeypatch.setattr(report.notion_client, "list_rows", lambda t, d: [])
    monkeypatch.setattr(report.device_notify, "push", lambda *a, **k: None)

    assert report.lambda_handler({}, None) == {"status": "ok", "devices": 0, "failed": 0}


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
    monkeypatch.setattr(report.notion_client, "list_rows", lambda t, d: [])
    monkeypatch.setattr(report.device_notify, "push", lambda *a, **k: None)

    assert report.lambda_handler({}, None) == {"status": "ok", "devices": 0, "failed": 0}


# --- Phase 3 wiring -------------------------------------------------------


LEDGER = {"devices": [{
    "asset_tag": "FS-01", "device_uuid": "u", "uuid_trusted": True,
    "app_version": "1.0.0", "last_seen_at": None, "last_account_sub": None,
    "actual_site": None, "actual_company": None,
}]}


def notion_row(page_id="p1", device="FS-01"):
    return {"page_id": page_id, "device": device, "dispatched": None,
            "due_back": None, "returned": False, "client": None,
            "activated": None, "notes": None}


def _wire(monkeypatch, rows, update=None, push=None):
    monkeypatch.setattr(report, "NOTION_TOKEN", "t")
    monkeypatch.setattr(report, "NOTION_DATA_SOURCE", "ds")
    monkeypatch.setattr(report, "_lambda", lambda: FakeLambdaClient(LEDGER))
    monkeypatch.setattr(report.notion_client, "list_rows", lambda t, d: rows)
    monkeypatch.setattr(report.notion_client, "update_row", update or (lambda t, p, v: None))
    monkeypatch.setattr(report.device_notify, "push", push or (lambda *a, **k: None))


def test_writes_every_row_and_counts_them(monkeypatch):
    written = []
    _wire(monkeypatch, [notion_row()], update=lambda t, p, v: written.append(p))

    out = report.lambda_handler({}, None)

    assert out["devices"] == 1
    assert out["failed"] == 0
    assert written == ["p1"]


def test_one_failing_row_does_not_abort_the_rest(monkeypatch):
    """A single bad row must not leave the other nineteen stale — that is the
    silent-staleness failure this whole design exists to avoid."""
    done = []

    def flaky(token, page_id, props):
        if page_id == "bad":
            raise RuntimeError("notion 500")
        done.append(page_id)

    _wire(monkeypatch, [notion_row("bad"), notion_row("ok", "FS-02")], update=flaky)

    out = report.lambda_handler({}, None)

    assert done == ["ok"]
    assert out["failed"] == 1
    assert out["status"] == "ok"


def test_the_push_receives_the_derived_results(monkeypatch):
    pushed = {}

    def capture(text, teams_webhook, email_to, ses_sender):
        pushed["text"] = text

    _wire(monkeypatch, [notion_row()], push=capture)
    report.lambda_handler({}, None)

    # Nothing is dispatched, so nothing needs a decision and the push is silent.
    assert pushed["text"] is None


def test_an_overdue_device_produces_a_message(monkeypatch):
    import datetime as dt

    pushed = {}
    row = notion_row()
    row["due_back"] = dt.date(2020, 1, 1)
    _wire(monkeypatch, [row],
          push=lambda text, **kw: pushed.__setitem__("text", text))

    report.lambda_handler({}, None)

    assert pushed["text"] is not None
    assert "FS-01" in pushed["text"]


def test_still_inert_when_the_token_is_unset(monkeypatch):
    """The wiring must not weaken the kill switch."""
    monkeypatch.setattr(report, "NOTION_TOKEN", "")
    called = []
    monkeypatch.setattr(report.notion_client, "list_rows",
                        lambda t, d: called.append(1) or [])

    assert report.lambda_handler({}, None) == {"status": "disabled", "devices": 0}
    assert called == []
