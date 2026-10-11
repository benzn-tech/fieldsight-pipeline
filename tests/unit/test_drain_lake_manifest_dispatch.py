"""The drain is an operator task: reachable by a direct invoke with `task`, never by an API
Gateway proxy event, and its event fields reach drain_lake_manifest."""
import contextlib

import lambda_org_api as api


def _patch(monkeypatch):
    seen = {}

    @contextlib.contextmanager
    def fake_conn():
        yield "CONN"
    monkeypatch.setattr(api, "get_connection", fake_conn)
    monkeypatch.setattr(api, "s3", lambda: "S3")

    def fake_drain(conn, s3_client, **kw):
        seen.update(conn=conn, s3=s3_client, **kw)
        return {"manifests": 0}
    monkeypatch.setattr(api, "drain_lake_manifest", fake_drain)
    return seen


def test_task_constant_and_dispatch_pass_the_event_fields(monkeypatch):
    seen = _patch(monkeypatch)
    assert api.DRAIN_LAKE_MANIFEST_TASK == "drain_lake_manifest"
    out = api.lambda_handler({"task": "drain_lake_manifest", "dry_run": True,
                              "max_objects": "7", "buckets": ["b1"]}, None)
    assert out == {"manifests": 0}
    assert seen == {"conn": "CONN", "s3": "S3", "dry_run": True, "max_objects": 7,
                    "buckets": ["b1"]}


def test_defaults_are_a_real_run_of_500_over_the_configured_buckets(monkeypatch):
    seen = _patch(monkeypatch)
    api.lambda_handler({"task": "drain_lake_manifest"}, None)
    assert seen["dry_run"] is False and seen["max_objects"] == 500 and seen["buckets"] is None
    api.lambda_handler({"task": "drain_lake_manifest", "dry_run": "true"}, None)
    assert seen["dry_run"] is False  # only the boolean true is a dry run


def test_an_api_gateway_event_cannot_reach_the_task(monkeypatch):
    seen = _patch(monkeypatch)
    try:
        api.lambda_handler({"httpMethod": "POST", "path": "/x", "body": '{"task": "drain_lake_manifest"}',
                            "requestContext": {}}, None)
    except Exception:  # noqa: BLE001 - whatever the router does, it must not drain
        pass
    assert seen == {}
