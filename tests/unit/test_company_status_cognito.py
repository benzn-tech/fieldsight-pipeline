"""The Cognito executor for company status changes (spec 2026-11-01 §5.3, ruling R3, Task 8).

Unit level: S3, Cognito and the repository are in-memory fakes, so these pin the decisions
(when the database is opened, which rows run, in what order, what each outcome writes, when
a marker is deleted). The SQL itself is covered by
tests/integration/test_company_status_cognito.py against real Postgres."""
import contextlib
import io
import json
import logging
import uuid

import pytest
from botocore.exceptions import ClientError

import company_status_cognito as exe
import lambda_org_api as api

FLAG = "COMPANY_STATUS_ENFORCE"
POOL = "pool-1"
CO = str(uuid.uuid4())
E1 = str(uuid.uuid4())
E2 = str(uuid.uuid4())


def _key(cid, eid):
    return f"company_status_requests/{cid}/{eid}.json"


def _marker(cid=CO, eid=E1):
    return {"company_id": cid, "event_id": eid, "requested_by": None, "requested_at": "t"}


class S3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.deleted = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(outer.objects)
                                    if k.startswith(Prefix)]}
        return P()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


class Cognito:
    """Records every admin call; `errors[(method, sub)]` is raised instead."""

    def __init__(self):
        self.calls, self.errors, self.hook = [], {}, None

    def _call(self, method, UserPoolId, Username):
        assert UserPoolId == POOL
        self.calls.append((method, Username))
        if self.hook:
            self.hook(method, Username)
        err = self.errors.get((method, Username))
        if err:
            raise err

    def admin_disable_user(self, **kw):
        self._call("disable", **kw)

    def admin_user_global_sign_out(self, **kw):
        self._call("signout", **kw)

    def admin_enable_user(self, **kw):
        self._call("enable", **kw)


def _client_error(code, msg="boom"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, "AdminDisableUser")


class Repo:
    """In-memory company_status_cognito / events / companies with the repository's API."""

    def __init__(self):
        self.status = {CO: "suspended"}
        self.events = {}          # eid -> (cid, order)
        self.rows = {}            # (eid, sub, action) -> dict(state, attempts, last_error, ts)
        self.clock = 0

    def event(self, eid, cid=CO):
        self.events[eid] = (cid, len(self.events))

    def row(self, eid, sub, action, state="pending", attempts=0, last_error=None):
        self.rows[(eid, sub, action)] = dict(state=state, attempts=attempts,
                                             last_error=last_error, ts=0)

    def _retryable(self, r, max_attempts):
        return r["state"] == "pending" or (
            r["state"] == "failed" and r["attempts"] < max_attempts
            and not (r["last_error"] or "").startswith("superseded"))

    # ---- the repository API ----
    def event_company(self, conn, eid):
        ev = self.events.get(str(eid))
        return ev[0] if ev else None

    def list_retryable_cognito(self, conn, cid, max_attempts):
        order = {"disable": 0, "signout": 1, "enable": 2}
        out = [dict(event_id=k[0], user_sub=k[1], action=k[2], state=r["state"],
                    attempts=r["attempts"])
               for k, r in self.rows.items()
               if self.events[k[0]][0] == cid and self._retryable(r, max_attempts)]
        return sorted(out, key=lambda r: (self.events[r["event_id"]][1], r["user_sub"],
                                          order[r["action"]]))

    def count_retryable_cognito(self, conn, cid, max_attempts):
        return len(self.list_retryable_cognito(conn, cid, max_attempts))

    def lock_company_status(self, conn, cid):
        conn.log.append(("lock", cid))
        return self.status.get(cid)

    def _match(self, k, state, attempts):
        r = self.rows.get(k)
        return r if r and r["state"] == state and r["attempts"] == attempts else None

    def supersede_cognito_row(self, conn, eid, sub, action, prev_state, prev_attempts, err):
        r = self._match((eid, sub, action), prev_state, prev_attempts)
        if not r:
            return 0
        self.clock += 1
        r.update(state="failed", last_error=err, ts=self.clock)
        return 1

    def claim_cognito_row(self, conn, eid, sub, action, prev_state, prev_attempts,
                          max_attempts):
        r = self._match((eid, sub, action), prev_state, prev_attempts)
        if not r or not self._retryable(r, max_attempts):
            return None
        self.clock += 1
        r.update(state="pending", attempts=r["attempts"] + 1, ts=self.clock)
        return r["attempts"]

    def finish_cognito_row(self, conn, eid, sub, action, attempts, state, last_error):
        r = self._match((eid, sub, action), "pending", attempts)
        if not r:
            return 0
        self.clock += 1
        r.update(state=state, last_error=last_error, ts=self.clock)
        return 1


class Conn:
    def __init__(self):
        self.log = []

    @contextlib.contextmanager
    def transaction(self):
        self.log.append("begin")
        yield
        self.log.append("end")

    def commit(self):
        self.log.append("commit")


@pytest.fixture
def w(monkeypatch):
    repo = Repo()
    for name in ("event_company", "list_retryable_cognito", "count_retryable_cognito",
                 "lock_company_status", "supersede_cognito_row", "claim_cognito_row",
                 "finish_cognito_row"):
        monkeypatch.setattr(exe.repo, name, getattr(repo, name))
    s3 = S3({})
    cog = Cognito()
    conns = []
    conn = Conn()

    @contextlib.contextmanager
    def fake_conn():
        conns.append(1)
        yield conn
    monkeypatch.setattr(api, "get_connection", fake_conn)
    monkeypatch.setattr(api, "s3", lambda: s3)
    monkeypatch.setattr(api, "cognito_executor", lambda: cog)
    monkeypatch.setattr(api, "cognito", lambda: pytest.fail("shared cognito() used"))
    monkeypatch.setattr(api, "COGNITO_USER_POOL_ID", POOL)
    monkeypatch.setattr(api, "LAKE_BUCKET", "lake-b", raising=False)
    monkeypatch.setattr(api, "S3_BUCKET", "data-b", raising=False)
    monkeypatch.setenv(FLAG, "on")
    # export_tick is not under test here; keep it S3-only and inert.
    return type("W", (), dict(repo=repo, s3=s3, cog=cog, conns=conns, conn=conn))


def _tick(ctx=None):
    return api.lambda_handler({"task": "export_tick"}, ctx)


def _suspend_rows(w, eid=E1, subs=("u1", "u2")):
    w.repo.event(eid)
    for s in subs:
        w.repo.row(eid, s, "disable")
        w.repo.row(eid, s, "signout")


# ---- dispatch and the idle / flag-off paths ---------------------------------------------

def test_names():
    assert api.COMPANY_STATUS_COGNITO_TASK == "company_status_cognito"
    assert exe.REQUEST_PREFIX == api.COMPANY_STATUS_REQUEST_PREFIX
    assert exe.MAX_ATTEMPTS == 5


def test_an_idle_tick_opens_no_database_connection(w):
    out = _tick()
    assert w.conns == [] and w.cog.calls == []
    assert out["company_status"]["markers"] == 0
    assert out["requests"] == 0 and out["ran"] is None       # export result shape kept


def test_flag_off_tick_logs_and_touches_nothing(w, monkeypatch, caplog):
    monkeypatch.delenv(FLAG)
    _suspend_rows(w)
    w.s3.objects[_key(CO, E1)] = _marker()
    with caplog.at_level(logging.WARNING):
        out = _tick()
    assert w.conns == [] and w.cog.calls == [] and w.s3.deleted == []
    assert out["company_status"] == {"markers": 1, "enforced": False}
    assert [r["state"] for r in w.repo.rows.values()] == ["pending"] * 4
    lines = [r.getMessage() for r in caplog.records
             if "COMPANY_GATE_WOULD_CALL_COGNITO" in r.getMessage()]
    assert len(lines) == 1 and "markers=1" in lines[0]


@pytest.mark.parametrize("value", ["", "off", "yes", " on"])
def test_flag_off_direct_task_refuses(w, monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    _suspend_rows(w)
    out = api.lambda_handler({"task": "company_status_cognito", "event_id": E1}, None)
    assert out == {"enforced": False}
    assert w.conns == [] and w.cog.calls == []


def test_an_api_gateway_event_cannot_reach_the_task(w):
    _suspend_rows(w)
    try:
        api.lambda_handler({"httpMethod": "POST", "path": "/x", "requestContext": {},
                            "body": json.dumps({"task": "company_status_cognito",
                                                "event_id": E1})}, None)
    except Exception:  # noqa: BLE001
        pass
    assert w.cog.calls == []


def test_the_s3_listing_comes_before_any_connection():
    import inspect
    assert "list_objects_v2" in inspect.getsource(exe._list_markers)
    src = inspect.getsource(exe.tick)
    assert src.index("_list_markers(") < src.index("get_connection()")
    assert src.index("if not keys:") < src.index("get_connection()")
    assert src.index("enforce_enabled()") < src.index("get_connection()")


# ---- the work ---------------------------------------------------------------------------

def test_disable_then_signout_per_user_and_marker_deleted(w):
    _suspend_rows(w)
    w.s3.objects[_key(CO, E1)] = _marker()
    out = _tick()
    assert w.cog.calls == [("disable", "u1"), ("signout", "u1"),
                           ("disable", "u2"), ("signout", "u2")]
    assert all(r["state"] == "done" and r["attempts"] == 1 and r["last_error"] is None
               for r in w.repo.rows.values())
    assert w.s3.deleted == [_key(CO, E1)]
    cs = out["company_status"]
    assert cs["done"] == 4 and cs["failed"] == 0 and cs["markers_deleted"] == 1
    assert len(w.conns) == 1
    # Every row runs under the company lock, and commits on its own.
    assert w.conn.log.count(("lock", CO)) == 4
    assert w.conn.log.count("commit") >= 4


def test_user_not_found_is_done(w):
    _suspend_rows(w, subs=("u1",))
    w.cog.errors[("disable", "u1")] = _client_error("UserNotFoundException")
    w.cog.errors[("signout", "u1")] = _client_error("UserNotFoundException")
    w.s3.objects[_key(CO, E1)] = _marker()
    _tick()
    for r in w.repo.rows.values():
        assert r["state"] == "done" and r["last_error"] == "user not found in Cognito"
    assert w.s3.deleted == [_key(CO, E1)]


def test_other_error_fails_the_row_and_keeps_the_marker(w):
    _suspend_rows(w, subs=("u1", "u2"))
    w.cog.errors[("disable", "u2")] = _client_error("TooManyRequestsException", "slow down")
    w.s3.objects[_key(CO, E1)] = _marker()
    out = _tick()
    r = w.repo.rows[(E1, "u2", "disable")]
    assert r["state"] == "failed" and r["attempts"] == 1
    assert r["last_error"] == "TooManyRequestsException: slow down"
    # The rest still ran (signout is independent of the disable).
    assert w.repo.rows[(E1, "u2", "signout")]["state"] == "done"
    assert w.s3.deleted == []
    assert out["company_status"]["failed"] == 1


def test_a_long_error_is_truncated(w):
    _suspend_rows(w, subs=("u1",))
    w.cog.errors[("disable", "u1")] = RuntimeError("x" * 5000)
    w.s3.objects[_key(CO, E1)] = _marker()
    _tick()
    err = w.repo.rows[(E1, "u1", "disable")]["last_error"]
    assert err.startswith("RuntimeError: x") and len(err) <= exe.ERROR_MAX


def test_retry_runs_only_retryable_rows(w):
    w.repo.event(E1)
    w.repo.row(E1, "u1", "disable", state="done", attempts=1)
    w.repo.row(E1, "u1", "signout", state="failed", attempts=2, last_error="boom")
    w.repo.row(E1, "u2", "disable", state="failed", attempts=1,
               last_error="superseded by reactivation")
    w.repo.row(E1, "u3", "disable", state="failed", attempts=5, last_error="boom")
    w.repo.row(E1, "u4", "disable", state="pending")
    w.s3.objects[_key(CO, E1)] = _marker()
    _tick()
    assert w.cog.calls == [("signout", "u1"), ("disable", "u4")]
    assert w.repo.rows[(E1, "u1", "signout")] | {"ts": 0} == dict(
        state="done", attempts=3, last_error=None, ts=0)
    assert w.repo.rows[(E1, "u2", "disable")]["state"] == "failed"
    assert w.repo.rows[(E1, "u3", "disable")]["attempts"] == 5
    # Only exhausted / superseded failures are left: nothing retryable, marker goes.
    assert w.s3.deleted == [_key(CO, E1)]


def test_max_attempts_stops_retrying(w):
    _suspend_rows(w, subs=("u1",))
    w.cog.errors[("disable", "u1")] = _client_error("InternalErrorException")
    w.s3.objects[_key(CO, E1)] = _marker()
    for i in range(1, 8):
        _tick()
        assert w.repo.rows[(E1, "u1", "disable")]["attempts"] == min(i, 5)
    assert w.cog.calls.count(("disable", "u1")) == 5
    assert w.s3.deleted == [_key(CO, E1)]      # deleted once the row is exhausted
    assert w.repo.rows[(E1, "u1", "disable")]["state"] == "failed"


def test_status_changed_rows_are_superseded_without_a_call(w):
    _suspend_rows(w, subs=("u1",))
    w.repo.status[CO] = "active"
    w.s3.objects[_key(CO, E1)] = _marker()
    out = _tick()
    assert w.cog.calls == []
    for r in w.repo.rows.values():
        assert r["state"] == "failed" and r["last_error"] == "superseded by status change"
        assert r["attempts"] == 0
    assert out["company_status"]["superseded"] == 2
    assert w.s3.deleted == [_key(CO, E1)]


def test_enable_runs_only_while_active(w):
    w.repo.event(E1)
    w.repo.event(E2)
    w.repo.row(E2, "u1", "enable")
    w.s3.objects[_key(CO, E2)] = _marker(eid=E2)
    w.repo.status[CO] = "exited"
    _tick()
    assert w.cog.calls == []
    assert w.repo.rows[(E2, "u1", "enable")]["last_error"] == "superseded by status change"
    w.repo.row(E2, "u2", "enable")
    w.repo.status[CO] = "active"
    w.s3.objects[_key(CO, E2)] = _marker(eid=E2)
    _tick()
    assert w.cog.calls == [("enable", "u2")]


def test_a_concurrent_supersede_is_not_overwritten(w):
    _suspend_rows(w, subs=("u1",))

    def supersede(method, sub):
        if method == "disable":
            w.repo.rows[(E1, sub, "disable")].update(
                state="failed", last_error="superseded by reactivation")
    w.cog.hook = supersede
    w.s3.objects[_key(CO, E1)] = _marker()
    out = _tick()
    r = w.repo.rows[(E1, "u1", "disable")]
    assert r["state"] == "failed" and r["last_error"] == "superseded by reactivation"
    assert out["company_status"]["raced"] == 1


def test_bad_markers_are_skipped_before_the_database(w):
    w.s3.objects["company_status_requests/not-a-uuid/x.json"] = _marker()
    w.s3.objects[_key(CO, E1)] = _marker(eid=E2)               # body disagrees with key
    w.s3.objects[_key(CO.upper(), E1)] = _marker()             # not canonical
    out = _tick()
    assert w.conns == [] and w.s3.deleted == []
    assert out["company_status"]["skipped"] == 3


def test_a_marker_whose_event_is_missing_or_elsewhere_is_dropped(w):
    other = str(uuid.uuid4())
    w.repo.event(E2, cid=other)
    w.s3.objects[_key(CO, E1)] = _marker()                     # no such event
    w.s3.objects[_key(CO, E2)] = _marker(eid=E2)               # event of another company
    out = _tick()
    assert sorted(w.s3.deleted) == sorted([_key(CO, E1), _key(CO, E2)])
    assert w.cog.calls == [] and out["company_status"]["dropped"] == 2


def test_one_tick_sweeps_the_whole_company_and_several_markers(w):
    co2 = str(uuid.uuid4())
    e3 = str(uuid.uuid4())
    w.repo.status[co2] = "exited"
    _suspend_rows(w, eid=E1, subs=("u1",))
    w.repo.event(E2)
    w.repo.row(E2, "u9", "disable")          # a second event of CO, marker lost
    w.repo.event(e3, cid=co2)
    w.repo.row(e3, "v1", "disable")
    w.s3.objects[_key(CO, E1)] = _marker()
    w.s3.objects[_key(co2, e3)] = _marker(cid=co2, eid=e3)
    _tick()
    assert sorted(w.cog.calls) == sorted([("disable", "u1"), ("signout", "u1"),
                                          ("disable", "u9"), ("disable", "v1")])
    assert len(w.s3.deleted) == 2 and len(w.conns) == 1


class Ctx:
    def __init__(self, ms):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


def test_out_of_time_keeps_the_marker(w):
    _suspend_rows(w, subs=("u1",))
    w.s3.objects[_key(CO, E1)] = _marker()
    out = _tick(Ctx(10_000))            # less than the safety margin
    assert w.cog.calls == [] and w.s3.deleted == []
    assert out["company_status"]["more"] is True


def test_no_pool_id_does_nothing(w, monkeypatch):
    monkeypatch.setattr(api, "COGNITO_USER_POOL_ID", "")
    _suspend_rows(w)
    w.s3.objects[_key(CO, E1)] = _marker()
    out = _tick()
    assert w.conns == [] and w.cog.calls == [] and w.s3.deleted == []
    assert "error" in out["company_status"]


def test_a_failing_cognito_tick_still_runs_the_export(w, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(exe, "tick", boom)
    out = _tick()
    assert out["requests"] == 0 and "error" in out["company_status"]


# ---- the direct task --------------------------------------------------------------------

def test_direct_task_sweeps_without_a_marker(w):
    _suspend_rows(w, subs=("u1",))
    out = api.lambda_handler({"task": "company_status_cognito", "event_id": E1}, None)
    assert out["enforced"] is True and out["company_id"] == CO and out["done"] == 2
    assert w.cog.calls == [("disable", "u1"), ("signout", "u1")]


def test_direct_task_by_company_and_deletes_its_markers(w):
    _suspend_rows(w, subs=("u1",))
    w.s3.objects[_key(CO, E1)] = _marker()
    out = api.lambda_handler({"task": "company_status_cognito", "company_id": CO}, None)
    assert out["done"] == 2 and w.s3.deleted == [_key(CO, E1)]


@pytest.mark.parametrize("ev, err", [
    ({}, "event_id or company_id"),
    ({"event_id": "nope"}, "event_id"),
    ({"company_id": "nope"}, "company_id"),
    ({"event_id": str(uuid.uuid4())}, "event not found"),
    ({"event_id": E1, "company_id": str(uuid.uuid4())}, "does not belong"),
])
def test_direct_task_validation(w, ev, err):
    w.repo.event(E1)
    out = api.lambda_handler({"task": "company_status_cognito", **ev}, None)
    assert err in out["error"] and w.cog.calls == []


def test_direct_task_accepts_an_upper_case_id(w):
    _suspend_rows(w, subs=("u1",))
    out = api.lambda_handler({"task": "company_status_cognito", "event_id": E1.upper()}, None)
    assert out["company_id"] == CO and out["done"] == 2


def test_the_executor_uses_its_own_short_timeout_client(monkeypatch):
    """Each call runs under FOR SHARE on the company row, which the status POST waits on
    inside org-api's 30 s: the executor's client must give up fast (controller ruling)."""
    made = []

    def fake_client(service, **kw):
        made.append((service, kw))
        return object()
    monkeypatch.setattr(api.boto3, "client", fake_client)
    monkeypatch.setattr(api, "_cognito_executor_client", None)
    c1 = api.cognito_executor()
    assert api.cognito_executor() is c1                       # lazy, created once
    (service, kw), = made
    cfg = kw["config"]
    assert service == "cognito-idp" and cfg is api.COGNITO_EXECUTOR_CONFIG
    assert cfg.connect_timeout == 3 and cfg.read_timeout == 8
    assert cfg.retries == {"max_attempts": 2, "mode": "standard"}
    assert api._company_status_deps()["cognito_factory"] is api.cognito_executor
