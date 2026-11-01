"""The records export (company_records_export) inside the existing export plumbing: it runs
before the file export of the same export id (task and tick), and GET export status presigns
its files -- exactly exports/{cid}/{eid}/records/<name>, nothing deeper or outside."""
import contextlib
import uuid

import pytest

import lambda_org_api as api
from tests.unit.test_company_export_routes import (  # noqa: F401 - `wired` is a fixture
    CO, EID, OTHER_CO, PREFIX, body, call, wired)


# -- presign confinement ------------------------------------------------------------------
@pytest.mark.parametrize("rest", [
    "part-0001.zip", "manifest.json", "records/topics.jsonl", "records/README.txt",
    "records/programme_task_deps.jsonl",
])
def test_presignable_accepts_top_level_and_records_files(rest):
    assert api._export_key_presignable(PREFIX, PREFIX + rest)


@pytest.mark.parametrize("key", [
    PREFIX + "records/../x", PREFIX + "records/../../secret.zip", PREFIX + "records/a/b",
    PREFIX + "records/", PREFIX + "recordsx/a", PREFIX + "other/a.jsonl",
    PREFIX + "records/.x", PREFIX + "records/a..b", PREFIX + "records/a\\b",
    PREFIX + "records/a\x00b", PREFIX + "../x", PREFIX + "a/b",
    f"exports/{OTHER_CO}/{EID}/records/topics.jsonl", "records/topics.jsonl", "",
])
def test_presignable_rejects_anything_else(key):
    assert not api._export_key_presignable(PREFIX, key)


def _records_state(company=CO, done=True, files=None):
    files = files if files is not None else [PREFIX + "records/topics.jsonl",
                                             PREFIX + "records/README.txt"]
    return {"company_id": company, "export_id": EID, "done": done, "files": files,
            "tables": {"topics": {"done": True, "rows": 3, "excluded": {"deleted": 1}}}}


def _manifest():
    return {"company_id": CO, "parts": [{"key": PREFIX + "part-0001.zip", "objects": 1,
                                         "bytes": 1}], "objects": [{}], "excluded": {}}


def test_done_status_presigns_the_records_files(wired):
    wired["s3"].docs[PREFIX + "manifest.json"] = _manifest()
    wired["s3"].docs[PREFIX + "records_state.json"] = _records_state(files=[
        PREFIX + "records/topics.jsonl", PREFIX + "records/README.txt",
        PREFIX + "records/../../x.jsonl", f"exports/{OTHER_CO}/{EID}/records/topics.jsonl"])
    out = body(call("GET", f"/companies/{CO}/export/{EID}"))
    assert out["status"] == "done"
    rec = out["records"]
    assert rec["status"] == "done"
    assert [f["key"] for f in rec["files"]] == [PREFIX + "records/topics.jsonl",
                                                PREFIX + "records/README.txt"]
    assert all(f["url"].startswith("https://signed/") for f in rec["files"])
    assert rec["tables"]["topics"] == {"done": True, "rows": 3, "excluded": {"deleted": 1}}
    signed = {k for _, k, _ in wired["s3"].presigned}
    assert signed == {PREFIX + "part-0001.zip", PREFIX + "manifest.json",
                      PREFIX + "records/topics.jsonl", PREFIX + "records/README.txt"}


def test_records_state_of_another_company_is_ignored(wired):
    wired["s3"].docs[PREFIX + "manifest.json"] = _manifest()
    wired["s3"].docs[PREFIX + "records_state.json"] = _records_state(company=OTHER_CO)
    out = body(call("GET", f"/companies/{CO}/export/{EID}"))
    assert out["records"] is None
    assert not any("/records/" in k for _, k, _ in wired["s3"].presigned)


def test_unfinished_records_presign_nothing(wired):
    wired["s3"].docs[PREFIX + "manifest.json"] = _manifest()
    wired["s3"].docs[PREFIX + "records_state.json"] = _records_state(done=False)
    out = body(call("GET", f"/companies/{CO}/export/{EID}"))
    assert out["records"]["status"] == "running" and out["records"]["files"] == []
    assert not any("/records/" in k for _, k, _ in wired["s3"].presigned)


def test_an_export_without_records_still_reports_done(wired):
    wired["s3"].docs[PREFIX + "manifest.json"] = _manifest()
    out = body(call("GET", f"/companies/{CO}/export/{EID}"))
    assert out["status"] == "done" and out["records"] is None


def test_running_status_carries_records_progress(wired):
    wired["s3"].docs[PREFIX + "state.json"] = {"company_id": CO, "parts": [], "entries": []}
    wired["s3"].docs[PREFIX + "records_state.json"] = _records_state(done=False)
    out = body(call("GET", f"/companies/{CO}/export/{EID}"))
    assert out["status"] == "running" and out["records"]["status"] == "running"
    assert wired["s3"].presigned == []


# -- task and tick ordering ---------------------------------------------------------------
def _wire_runs(monkeypatch, rec_outs, file_outs):
    order = []

    @contextlib.contextmanager
    def fake_conn():
        order.append("conn")
        yield "CONN"
    monkeypatch.setattr(api, "get_connection", fake_conn)

    def rec_run(conn, s3c, cid, **kw):
        order.append(("records", cid, kw["export_id"], kw["time_budget_s"]))
        return rec_outs.pop(0)

    def file_run(conn, s3c, cid, **kw):
        order.append(("files", cid, kw["export_id"], kw["time_budget_s"]))
        return file_outs.pop(0)
    monkeypatch.setattr(api.company_records_export, "run", rec_run)
    monkeypatch.setattr(api.company_export, "run", file_run)
    return order


def _rec(done):
    return {"company_id": CO, "export_id": EID, "done": done, "tables": {}, "files": []}


def _file(done):
    return {"company_id": CO, "export_id": EID, "done": done, "next_cursor": None if done else "c"}


def test_task_runs_records_first_then_files_with_the_same_export_id(monkeypatch):
    monkeypatch.setattr(api, "s3", lambda: "S3")
    order = _wire_runs(monkeypatch, [_rec(True)], [_file(True)])
    out = api.lambda_handler({"task": "export_company", "company_id": CO, "export_id": EID,
                              "time_budget_s": 99}, None)
    assert [o[0] for o in order if o != "conn"] == ["records", "files"]
    assert order[1][2] == order[2][2] == EID
    assert out["done"] is True and out["records"]["done"] is True


def test_task_mints_one_export_id_for_both_parts(monkeypatch):
    monkeypatch.setattr(api, "s3", lambda: "S3")
    order = _wire_runs(monkeypatch, [_rec(True)], [_file(True)])
    api.lambda_handler({"task": "export_company", "company_id": CO}, None)
    eids = {o[2] for o in order if o != "conn"}
    assert len(eids) == 1 and uuid.UUID(eids.pop())


def test_task_with_unfinished_records_does_not_start_the_files(monkeypatch):
    monkeypatch.setattr(api, "s3", lambda: "S3")
    order = _wire_runs(monkeypatch, [_rec(False)], [])
    out = api.lambda_handler({"task": "export_company", "company_id": CO, "export_id": EID}, None)
    assert [o[0] for o in order if o != "conn"] == ["records"]
    assert out["done"] is False and out["records"]["done"] is False


def test_task_cursor_without_export_id_is_an_error_value(monkeypatch):
    monkeypatch.setattr(api, "s3", lambda: "S3")
    order = _wire_runs(monkeypatch, [], [])
    out = api.lambda_handler({"task": "export_company", "company_id": CO, "cursor": "k"}, None)
    assert "export_id" in out["error"] and [o for o in order if o != "conn"] == []


def test_tick_runs_records_then_files_on_one_connection(monkeypatch):
    from tests.unit.test_export_tick import KEY, _request, _wire
    s3, conns, calls = _wire(monkeypatch, {KEY: _request()}, [])
    order = _wire_runs(monkeypatch, [_rec(True)], [_file(True)])
    out = api.lambda_handler({"task": "export_tick"}, None)
    assert order.count("conn") == 1
    assert [o[0] for o in order if o != "conn"] == ["records", "files"]
    assert out["done"] is True and s3.deleted == [KEY]


def test_tick_keeps_the_request_while_records_are_unfinished(monkeypatch):
    from tests.unit.test_export_tick import KEY, _request, _wire
    s3, conns, calls = _wire(monkeypatch, {KEY: _request()}, [])
    order = _wire_runs(monkeypatch, [_rec(False)], [])
    out = api.lambda_handler({"task": "export_tick"}, None)
    assert [o[0] for o in order if o != "conn"] == ["records"]
    assert out["done"] is False and s3.deleted == []


def test_records_phase_alone_reads_as_running(wired):
    """The records export runs before the file export writes state.json: still 'running'."""
    wired["s3"].docs[PREFIX + "records_state.json"] = _records_state(done=False)
    out = body(call("GET", f"/companies/{CO}/export/{EID}"))
    assert out["status"] == "running" and out["records"]["status"] == "running"
    assert out["partsWritten"] == 0 and wired["s3"].presigned == []
    wired["s3"].docs[PREFIX + "records_state.json"] = _records_state(company=OTHER_CO)
    assert call("GET", f"/companies/{CO}/export/{EID}")["statusCode"] == 404
