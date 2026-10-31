"""export_company is an operator task: reachable only by a direct invoke carrying `task`
(never through an API Gateway proxy event). It does not chain: export_tick resumes it."""
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

    def fake_run(conn, s3_client, company_id, **kw):
        seen["runs"].append((conn, s3_client, company_id, kw))
        return outs.pop(0)
    monkeypatch.setattr(api.company_export, "run", fake_run)
    # the records export (runs first, same export id) is covered in
    # test_company_records_export_wiring.py; here it is always already done
    monkeypatch.setattr(api.company_records_export, "run",
                        lambda *a, **k: {"done": True, "tables": {}, "files": []})
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


def test_not_done_returns_the_cursor_and_invokes_nothing(monkeypatch):
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


def test_export_error_is_an_error_value(monkeypatch):
    _patch(monkeypatch, [])

    def boom(*a, **k):
        raise api.company_export.ExportError("lake_objects missing")
    monkeypatch.setattr(api.company_export, "run", boom)
    out = api.lambda_handler({"task": "export_company", "company_id": CO}, None)
    assert out == {"error": "lake_objects missing"}
