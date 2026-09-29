"""The collapse runs as an org-api task because Aurora is only reachable from
inside the VPC. It is a dry run unless `apply` is exactly true, and no API
request can reach it."""
import json

import lambda_org_api as api


class Conn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _wire(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "get_connection", lambda: Conn())
    monkeypatch.setattr(api, "s3", lambda: "S3")
    import photo_collapse
    monkeypatch.setattr(photo_collapse, "run",
                        lambda conn, s3, bucket, apply=False, folder=None, date=None:
                        calls.append({"apply": apply, "folder": folder, "date": date}) or {"ok": 1})
    monkeypatch.setattr(api, "dispatch", lambda conn, event, method, route: {"statusCode": 404})
    return calls


def test_no_apply_means_a_dry_run(monkeypatch):
    calls = _wire(monkeypatch)
    api.lambda_handler({"task": "collapse_multibound_photos"}, None)
    assert calls == [{"apply": False, "folder": None, "date": None}]


def test_only_a_literal_true_writes(monkeypatch):
    calls = _wire(monkeypatch)
    for v in ("true", 1, "yes", [True]):
        api.lambda_handler({"task": "collapse_multibound_photos", "apply": v}, None)
    api.lambda_handler({"task": "collapse_multibound_photos", "apply": True,
                        "folder": "Ben_Lin", "date": "2026-09-11"}, None)
    assert [c["apply"] for c in calls] == [False, False, False, False, True]
    assert calls[-1]["folder"] == "Ben_Lin"


def test_an_api_request_cannot_reach_it(monkeypatch):
    calls = _wire(monkeypatch)
    api.lambda_handler({"httpMethod": "POST", "path": "/api/org/anything",
                        "body": json.dumps({"task": "collapse_multibound_photos", "apply": True})}, None)
    assert calls == []
