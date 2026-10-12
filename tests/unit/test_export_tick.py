"""export_tick: the scheduled pickup of export_requests/ (ruling R11a, no Lambda API from the VPC).

An idle tick must be S3-only (Aurora must be able to auto-pause); a tick resumes an unfinished
export and deletes the request only when it is done; a request that disagrees with its own key
is skipped with nothing deleted."""
import contextlib
import io
import json
import uuid

import lambda_org_api as api

CO = str(uuid.uuid4())
EID = str(uuid.uuid4())
KEY = f"export_requests/{CO}/{EID}.json"
STATE_KEY = f"exports/{CO}/{EID}/state.json"


class S3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.deleted = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(outer.objects) if k.startswith(Prefix)]}
        return P()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


def _request(cid=CO, eid=EID):
    return {"company_id": cid, "export_id": eid, "requested_by": "u1", "requested_at": "t"}


def _wire(monkeypatch, objects, runs):
    s3 = S3(objects)
    conns = []

    @contextlib.contextmanager
    def fake_conn():
        conns.append(1)
        yield "CONN"
    monkeypatch.setattr(api, "get_connection", fake_conn)
    monkeypatch.setattr(api, "s3", lambda: s3)
    monkeypatch.setenv("LAKE_BUCKET", "lake-b")
    monkeypatch.setattr(api, "LAKE_BUCKET", "lake-b", raising=False)
    monkeypatch.setattr(api, "S3_BUCKET", "data-b", raising=False)
    monkeypatch.setattr(api.company_export, "_read_state",
                        lambda s3c, bucket, key: (s3.objects.get(key), None))
    calls = []

    def fake_run(conn, s3c, company_id, **kw):
        calls.append((company_id, kw))
        return runs.pop(0)
    monkeypatch.setattr(api.company_export, "run", fake_run)
    return s3, conns, calls


def _out(done):
    return {"company_id": CO, "export_id": EID, "done": done, "next_cursor": None if done else "c"}


def test_the_task_is_dispatched_by_name():
    assert api.EXPORT_TICK_TASK == "export_tick"
    assert api.EXPORT_REQUEST_PREFIX == "export_requests/"


def test_an_empty_tick_makes_zero_database_connections(monkeypatch):
    s3, conns, calls = _wire(monkeypatch, {}, [])
    out = api.lambda_handler({"task": "export_tick"}, None)
    assert out["requests"] == 0 and out["ran"] is None
    assert conns == [] and calls == []


def test_a_tick_resumes_an_unfinished_export_and_deletes_the_request_when_done(monkeypatch):
    s3, conns, calls = _wire(
        monkeypatch, {KEY: _request(), STATE_KEY: {"done": False, "cursor": "k"}}, [_out(True)])
    out = api.lambda_handler({"task": "export_tick"}, None)
    assert out["ran"] == KEY and out["done"] is True
    assert len(conns) == 1
    (cid, kw), = calls
    assert cid == CO and kw["export_id"] == EID
    assert "cursor" not in kw            # the saved state, not a passed cursor, is the position
    assert s3.deleted == [KEY]


def test_a_tick_that_runs_out_of_time_keeps_the_request(monkeypatch):
    s3, conns, calls = _wire(monkeypatch, {KEY: _request()}, [_out(False)])
    out = api.lambda_handler({"task": "export_tick"}, None)
    assert out["ran"] == KEY and out["done"] is False
    assert s3.deleted == []


def test_a_request_whose_state_is_done_is_deleted_without_running(monkeypatch):
    s3, conns, calls = _wire(monkeypatch, {KEY: _request(), STATE_KEY: {"done": True}}, [])
    out = api.lambda_handler({"task": "export_tick"}, None)
    assert out["ran"] is None and out["finished_without_running"] == 1
    assert s3.deleted == [KEY] and calls == [] and conns == []


def test_a_request_that_disagrees_with_its_key_is_skipped_and_nothing_is_deleted(monkeypatch):
    other = str(uuid.uuid4())
    s3, conns, calls = _wire(monkeypatch, {KEY: _request(cid=other)}, [])
    out = api.lambda_handler({"task": "export_tick"}, None)
    assert out["ran"] is None and out["skipped"] == 1
    assert s3.deleted == [] and calls == [] and conns == []


def test_a_malformed_key_is_skipped_and_the_next_request_still_runs(monkeypatch):
    bad = "export_requests/!not-a-uuid/x.json"
    s3, conns, calls = _wire(monkeypatch, {bad: _request(), KEY: _request()}, [_out(True)])
    out = api.lambda_handler({"task": "export_tick"}, None)
    assert out["ran"] == KEY and out["skipped"] == 1
    assert s3.deleted == [KEY]


def test_one_export_per_tick(monkeypatch):
    eid2 = str(uuid.uuid4())
    key2 = f"export_requests/{CO}/{eid2}.json"
    s3, conns, calls = _wire(monkeypatch, {KEY: _request(), key2: _request(eid=eid2)},
                             [_out(True), _out(True)])
    api.lambda_handler({"task": "export_tick"}, None)
    assert len(calls) == 1


def test_an_api_gateway_event_cannot_reach_the_tick(monkeypatch):
    s3, conns, calls = _wire(monkeypatch, {KEY: _request()}, [_out(True)])
    try:
        api.lambda_handler({"httpMethod": "POST", "path": "/x", "body": '{"task": "export_tick"}',
                            "requestContext": {}}, None)
    except Exception:  # noqa: BLE001
        pass
    assert calls == [] and s3.deleted == []


def test_the_cadence_exemption_is_backed_by_the_s3_first_gate():
    """LakeOpsFunction is exempt from the auto-pause cadence rule only because an idle tick never
    connects: the listing comes before any get_connection() in export_tick."""
    import inspect
    src = inspect.getsource(api.export_tick)
    assert src.index("list_objects_v2") < src.index("get_connection()")
    assert src.index('if not keys:') < src.index("get_connection()")
