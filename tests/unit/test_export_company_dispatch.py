"""export_company is an operator task: reachable only by a direct invoke carrying `task`
(never through an API Gateway proxy event), and it chains itself with the cursor until done."""
import contextlib
import json
import uuid

import lambda_org_api as api

CO = str(uuid.uuid4())


class Lam:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)
        return {"StatusCode": 202}


def _patch(monkeypatch, outs):
    seen = {"runs": []}
    lam = Lam()

    @contextlib.contextmanager
    def fake_conn():
        yield "CONN"
    monkeypatch.setattr(api, "get_connection", fake_conn)
    monkeypatch.setattr(api, "s3", lambda: "S3")
    monkeypatch.setattr(api, "lambda_client", lambda: lam)

    def fake_run(conn, s3_client, company_id, **kw):
        seen["runs"].append((conn, s3_client, company_id, kw))
        return outs.pop(0)
    monkeypatch.setattr(api.company_export, "run", fake_run)
    return seen, lam


def _out(done, cursor=None):
    return {"company_id": CO, "export_id": "e1", "done": done, "next_cursor": cursor,
            "parts": [], "objects": 0, "bytes": 0}


def test_constant_and_dispatch_pass_the_event_fields(monkeypatch):
    seen, lam = _patch(monkeypatch, [_out(True)])
    assert api.EXPORT_COMPANY_TASK == "export_company"
    out = api.lambda_handler({"task": "export_company", "company_id": CO, "export_id": "e1",
                              "cursor": "k", "time_budget_s": 99, "part_bytes": 1234}, None)
    assert out["done"] is True and lam.calls == []
    conn, s3c, co, kw = seen["runs"][0]
    assert (conn, s3c, co) == ("CONN", "S3", CO)
    assert kw["export_id"] == "e1" and kw["cursor"] == "k"
    assert kw["time_budget_s"] == 99 and kw["part_bytes"] == 1234


def test_defaults_and_junk_values(monkeypatch):
    seen, _ = _patch(monkeypatch, [_out(True)])
    api.lambda_handler({"task": "export_company", "company_id": CO, "time_budget_s": "x",
                        "part_bytes": True}, None)
    kw = seen["runs"][0][3]
    assert kw["time_budget_s"] == 780 and kw["part_bytes"] == 536870912


def test_not_done_chains_to_lake_ops_with_the_cursor(monkeypatch):
    monkeypatch.setenv("LAKE_OPS_FUNCTION", "fs-lakeops")
    _, lam = _patch(monkeypatch, [_out(False, "cur-1")])
    out = api.lambda_handler({"task": "export_company", "company_id": CO, "export_id": "e1"}, None)
    assert out["chained"] is True
    (call,) = lam.calls
    assert call["FunctionName"] == "fs-lakeops" and call["InvocationType"] == "Event"
    payload = json.loads(call["Payload"])
    assert payload["task"] == "export_company" and payload["cursor"] == "cur-1"
    assert payload["company_id"] == CO and payload["export_id"] == "e1"


def test_not_done_without_a_target_returns_the_cursor_for_a_hand_re_invoke(monkeypatch):
    monkeypatch.delenv("LAKE_OPS_FUNCTION", raising=False)
    _, lam = _patch(monkeypatch, [_out(False, "cur-1")])
    out = api.lambda_handler({"task": "export_company", "company_id": CO}, None)
    assert out["next_cursor"] == "cur-1" and lam.calls == [] and "chained" not in out


def test_bad_ids_are_an_error_value_not_a_crash(monkeypatch):
    _patch(monkeypatch, [])

    def boom(*a, **k):
        raise ValueError("export_id must be a UUID")
    monkeypatch.setattr(api.company_export, "run", boom)
    out = api.lambda_handler({"task": "export_company", "company_id": CO, "export_id": "../x"}, None)
    assert "UUID" in out["error"]


def test_an_api_gateway_event_cannot_reach_the_task(monkeypatch):
    seen, _ = _patch(monkeypatch, [_out(True)])
    try:
        api.lambda_handler({"httpMethod": "POST", "path": "/x",
                            "body": '{"task": "export_company"}', "requestContext": {}}, None)
    except Exception:  # noqa: BLE001 - whatever the router does, it must not export
        pass
    assert seen["runs"] == []
