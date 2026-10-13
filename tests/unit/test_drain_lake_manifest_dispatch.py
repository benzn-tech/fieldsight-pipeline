"""The drain is an operator/scheduled task: reachable by a direct invoke with `task`, never by an
API Gateway proxy event; its event fields reach drain_lake_manifest; it lists S3 FIRST and opens
no database connection when there is nothing to drain (Aurora must be able to auto-pause); and it
loops over manifest objects until a time budget runs out, reporting `more`."""
import contextlib
import inspect

import lambda_org_api as api


class Ctx:
    def __init__(self, ms):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


def _patch(monkeypatch, pages=None, listed=True):
    """`pages` = the results of successive drain passes."""
    seen = {"conns": 0, "calls": []}

    @contextlib.contextmanager
    def fake_conn():
        seen["conns"] += 1
        yield "CONN"
    monkeypatch.setattr(api, "get_connection", fake_conn)
    monkeypatch.setattr(api, "s3", lambda: "S3")
    monkeypatch.setattr(api, "S3_BUCKET", "data-b", raising=False)
    monkeypatch.setattr(api, "LAKE_BUCKET", "lake-b", raising=False)
    monkeypatch.setattr(api, "_list_manifest_objects",
                        lambda s3c, bucket, limit: [(bucket, "lake_manifest/x")] if listed else [])
    results = list(pages if pages is not None else [{"manifests": 0}])

    def fake_drain(conn, s3_client, **kw):
        seen["calls"].append(dict(conn=conn, s3=s3_client, **kw))
        return results.pop(0)
    monkeypatch.setattr(api, "drain_lake_manifest", fake_drain)
    return seen


def test_task_constant_and_dispatch_pass_the_event_fields(monkeypatch):
    seen = _patch(monkeypatch)
    assert api.DRAIN_LAKE_MANIFEST_TASK == "drain_lake_manifest"
    out = api.lambda_handler({"task": "drain_lake_manifest", "dry_run": True,
                              "max_objects": "7", "buckets": ["b1"]}, None)
    assert out["manifests"] == 0 and out["more"] is False and out["dry_run"] is True
    assert seen["calls"] == [{"conn": "CONN", "s3": "S3", "dry_run": True, "max_objects": 7,
                              "buckets": ["b1"]}]


def test_defaults_are_a_real_run_of_500_over_the_configured_buckets(monkeypatch):
    seen = _patch(monkeypatch, pages=[{"manifests": 0}, {"manifests": 0}])
    api.lambda_handler({"task": "drain_lake_manifest"}, None)
    c = seen["calls"][0]
    assert c["dry_run"] is False and c["max_objects"] == 500 and c["buckets"] is None
    api.lambda_handler({"task": "drain_lake_manifest", "dry_run": "true"}, None)
    assert seen["calls"][1]["dry_run"] is False  # only the boolean true is a dry run


def test_an_empty_listing_opens_no_database_connection(monkeypatch):
    seen = _patch(monkeypatch, listed=False)
    out = api.lambda_handler({"task": "drain_lake_manifest"}, None)
    assert seen["conns"] == 0 and seen["calls"] == []
    assert out["manifests"] == 0 and out["more"] is False


def test_the_cadence_exemption_is_backed_by_the_s3_first_gate():
    src = inspect.getsource(api.drain_lake_manifest_run)
    assert src.index("_list_manifest_objects") < src.index("get_connection()")
    assert src.index("if not found:") < src.index("get_connection()")


def test_it_loops_full_passes_and_reports_the_sums(monkeypatch):
    full = {"manifests": 2, "lines": 5, "upserted": 5, "deleted_manifests": 2,
            "unresolved_sample": [{"key": "k"}], "table_missing": False}
    seen = _patch(monkeypatch, pages=[dict(full), dict(full),
                                      {"manifests": 1, "lines": 1, "upserted": 1,
                                       "deleted_manifests": 1}])
    out = api.lambda_handler({"task": "drain_lake_manifest", "max_objects": 2}, None)
    assert len(seen["calls"]) == 3 and seen["conns"] == 1
    assert (out["manifests"], out["lines"], out["upserted"], out["passes"]) == (5, 11, 11, 3)
    assert out["more"] is False and len(out["unresolved_sample"]) == 2


def test_it_stops_at_the_time_budget_and_says_more(monkeypatch):
    full = {"manifests": 2, "deleted_manifests": 2}
    ticks = iter(range(0, 10_000, 100))     # every clock read advances 100 s
    monkeypatch.setattr(api.time, "monotonic", lambda: next(ticks))
    seen = _patch(monkeypatch, pages=[dict(full) for _ in range(50)])
    out = api.lambda_handler({"task": "drain_lake_manifest", "max_objects": 2,
                              "time_budget_s": 500}, None)
    assert 1 <= len(seen["calls"]) < 50
    assert out["more"] is True


def test_the_budget_is_cut_to_the_lambdas_remaining_time(monkeypatch):
    full = {"manifests": 2, "deleted_manifests": 2}
    ticks = iter(range(0, 10_000, 100))
    monkeypatch.setattr(api.time, "monotonic", lambda: next(ticks))
    seen = _patch(monkeypatch, pages=[dict(full) for _ in range(50)])
    out = api.lambda_handler({"task": "drain_lake_manifest", "max_objects": 2,
                              "time_budget_s": 10_000}, Ctx(200_000))   # 200 s - 90 s margin
    assert len(seen["calls"]) <= 2 and out["more"] is True


def test_a_pass_that_deletes_nothing_ends_the_loop(monkeypatch):
    seen = _patch(monkeypatch, pages=[{"manifests": 2, "deleted_manifests": 0, "kept": 2}] * 5)
    out = api.lambda_handler({"task": "drain_lake_manifest", "max_objects": 2}, None)
    assert len(seen["calls"]) == 1 and out["more"] is True


def test_a_dry_run_is_one_pass(monkeypatch):
    seen = _patch(monkeypatch, pages=[{"manifests": 2}] * 5)
    out = api.lambda_handler({"task": "drain_lake_manifest", "max_objects": 2, "dry_run": True},
                             None)
    assert len(seen["calls"]) == 1 and out["more"] is False


def test_an_api_gateway_event_cannot_reach_the_task(monkeypatch):
    seen = _patch(monkeypatch)
    try:
        api.lambda_handler({"httpMethod": "POST", "path": "/x", "body": '{"task": "drain_lake_manifest"}',
                            "requestContext": {}}, None)
    except Exception:  # noqa: BLE001 - whatever the router does, it must not drain
        pass
    assert seen["calls"] == []
