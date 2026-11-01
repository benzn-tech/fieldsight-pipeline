"""The task_no backfill (action packs spec §14 D1) runs as an org-api task because Aurora is
only reachable from inside the VPC (same reason as collapse_multibound_photos). It is a dry
run unless `apply` is exactly true, it is NOT behind ACTION_PACKS (Phase 0 prerequisite), and
no API request can reach it."""
import json

import lambda_org_api as api

SITE = "6f1c2a8e-3b7d-4c55-9a51-1f0e2b7c9d10"


class Conn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _wire(monkeypatch):
    calls, conns = [], []

    def fake_conn(*a, **kw):
        conns.append(kw)
        return Conn()

    monkeypatch.setattr(api, "get_connection", fake_conn)
    import task_numbers_backfill
    monkeypatch.setattr(task_numbers_backfill, "run_backfill",
                        lambda conn, apply=False, site_ids=None:
                        calls.append({"apply": apply, "site_ids": site_ids})
                        or {"sites": 1, "to_number": 2, "numbered": 2 if apply else 0})
    monkeypatch.setattr(api, "dispatch", lambda conn, event, method, route: {"statusCode": 404})
    monkeypatch.delenv("ACTION_PACKS", raising=False)
    return calls, conns


def test_no_apply_means_a_dry_run(monkeypatch):
    calls, conns = _wire(monkeypatch)
    out = api.lambda_handler({"task": "backfill_task_numbers"}, None)
    assert calls == [{"apply": False, "site_ids": None}]
    assert out == {"applied": False, "sites": 1, "to_number": 2, "numbered": 0}
    # autocommit: each site's write is its own transaction, so one site's lock at a time
    assert conns == [{"autocommit": True}]


def test_only_a_literal_true_writes(monkeypatch):
    calls, _ = _wire(monkeypatch)
    for v in ("true", 1, "yes", [True]):
        api.lambda_handler({"task": "backfill_task_numbers", "apply": v}, None)
    out = api.lambda_handler({"task": "backfill_task_numbers", "apply": True,
                              "site_ids": [SITE]}, None)
    assert [c["apply"] for c in calls] == [False, False, False, False, True]
    assert calls[-1]["site_ids"] == [SITE]
    assert out["applied"] is True and out["numbered"] == 2


def test_runs_with_action_packs_off(monkeypatch):
    calls, _ = _wire(monkeypatch)
    monkeypatch.setenv("ACTION_PACKS", "off")
    api.lambda_handler({"task": "backfill_task_numbers"}, None)
    assert len(calls) == 1


def test_malformed_site_ids_are_refused_before_any_connection(monkeypatch):
    calls, conns = _wire(monkeypatch)
    for bad in ("6f1c2a8e-3b7d-4c55-9a51-1f0e2b7c9d10", ["not-a-uuid"], [], [1], {"a": 1}):
        out = api.lambda_handler({"task": "backfill_task_numbers", "site_ids": bad,
                                  "apply": True}, None)
        assert out == {"error": "site_ids must be a non-empty list of UUIDs"}
    assert calls == [] and conns == []


def test_an_api_request_cannot_reach_it(monkeypatch):
    calls, _ = _wire(monkeypatch)
    api.lambda_handler({"httpMethod": "POST", "path": "/api/org/anything",
                        "body": json.dumps({"task": "backfill_task_numbers", "apply": True})},
                       None)
    assert calls == []
