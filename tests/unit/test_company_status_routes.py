"""POST /companies/{id}/status and GET /companies/{id}/status-events (company offboarding,
spec 2026-11-01 §5.3, §5.4, §8, §9; Task 7).

Fake-connection tests: they cover routing, the auth matrix, validation, idempotency, the
flag-off socket shadow, the commit-before-marker order and the 503. They execute no SQL --
every statement is covered by tests/integration/test_company_status_endpoints.py.
"""
import json
import logging
import uuid

import psycopg.errors
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO = str(uuid.uuid4())
OWN_CO = str(uuid.uuid4())
FLAG = "COMPANY_STATUS_ENFORCE"


class FakeConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        log = self.log

        class _Tx:
            def __enter__(self):
                log.append("begin")

            def __exit__(self, exc_type, *a):
                log.append("rollback" if exc_type else "release")
                return False
        return _Tx()

    def commit(self):
        self.log.append("commit")


class S3:
    def __init__(self, log, fail=False):
        self.log, self.fail, self.puts = log, fail, []

    def put_object(self, **kw):
        if self.fail:
            raise RuntimeError("s3 down")
        self.log.append("put")
        self.puts.append(kw)
        return {}


class Repo:
    """Stands in for repositories.company_status; records what the handler asked for."""

    def __init__(self, state):
        self.s = state

    def lock_company(self, conn, cid):
        if self.s.get("not_migrated"):
            raise psycopg.errors.UndefinedTable("relation company_status_events does not exist")
        self.s["log"].append("lock")
        return {"id": cid, "status": self.s["current"], "status_changed_at": None,
                "status_reason": None, "billing_stopped_at": None, "exited_at": None}

    def apply_status(self, conn, cid, to_status, reason, stop_billing):
        self.s["log"].append(("update", to_status, reason, stop_billing))
        return {"status": to_status, "status_changed_at": "t", "status_reason": reason,
                "billing_stopped_at": None, "exited_at": None}

    def insert_event(self, conn, cid, from_status, to_status, reason, actor_user_id,
                     actor_sub, detail):
        self.s["log"].append(("event", from_status, to_status))
        self.s["detail"] = detail
        return "ev-1"

    def create_deactivation_rows(self, conn, event_id, cid):
        self.s["log"].append("rows")
        return {"disable": self.s["n_users"], "signout": self.s["n_users"]}

    def supersede_pending_enables(self, conn, event_id, cid):
        return 0

    def create_reactivation_rows(self, conn, event_id, cid):
        self.s["log"].append("enable-rows")
        return {"superseded": 0, "enable": self.s["n_enable"]}

    def home_user_count(self, conn, cid):
        return self.s["n_users"]

    def home_device_count(self, conn, cid):
        return 2

    def count_sockets(self, conn, cid):
        self.s["log"].append("count-sockets")
        return 3

    def close_sockets(self, conn, cid):
        self.s["log"].append("close-sockets")
        return 3

    def read_status(self, conn, cid):
        if self.s.get("not_migrated"):
            raise psycopg.errors.UndefinedColumn("column status does not exist")
        return {"status": "suspended", "status_changed_at": None, "status_reason": "r",
                "billing_stopped_at": None, "exited_at": None}

    def list_events(self, conn, cid):
        return [{"id": "ev-1", "from_status": "active", "to_status": "suspended",
                 "reason": "r", "actor_user_id": "u1", "actor_sub": "sub-1", "at": "t",
                 "detail": {}, "cognito": [{"user_sub": "s1", "action": "disable",
                                            "state": "done", "attempts": 1,
                                            "last_error": None, "updated_at": "t"}]}]


@pytest.fixture
def w(monkeypatch):
    log = []
    state = {"role": "platform_admin", "company": {"id": CO, "name": "Co"}, "current": "active",
             "n_users": 4, "n_enable": 0, "log": log}
    state["s3"] = S3(log)
    monkeypatch.setenv("LAKE_BUCKET", "lake-b")
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setattr(org, "get_connection", lambda: FakeConn(log))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: {
        "id": "u1", "cognito_sub": "sub-1", "company_id": uuid.UUID(OWN_CO),
        "global_role": state["role"], "archived_at": None})
    monkeypatch.setattr(org.companies, "get_company_by_id",
                        lambda conn, cid: state["company"])
    monkeypatch.setattr(org, "s3", lambda: state["s3"])
    monkeypatch.setattr(org, "company_status_repo", Repo(state))
    return state


def call(method, path, body=None):
    res = org.lambda_handler(
        {"httpMethod": method, "path": "/api/org" + path,
         "body": json.dumps(body) if isinstance(body, dict) else body, "headers": {},
         "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)
    return res["statusCode"], json.loads(res["body"])


def post(co=CO, **body):
    return call("POST", f"/companies/{co}/status", body)


SUSPEND = {"status": "suspended", "reason": "unpaid invoices"}


# ---- auth ------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["admin", "gm", "worker", "pm", "regional_manager"])
def test_only_platform_admin_may_change_status(w, role):
    w["role"] = role
    assert post(**SUSPEND) == (403, {"error": "platform_admin role required"})
    assert not [e for e in w["log"] if e != "begin" and e != "release"]


@pytest.mark.parametrize("role", ["admin", "gm", "worker"])
def test_only_platform_admin_may_read_status_events(w, role):
    w["role"] = role
    assert call("GET", f"/companies/{CO}/status-events") == (
        403, {"error": "platform_admin role required"})


def test_platform_admin_changes_status(w):
    status, body = post(**SUSPEND)
    assert status == 200, body
    assert body["changed"] is True and body["status"] == "suspended"
    assert body["previousStatus"] == "active" and body["eventId"] == "ev-1"


def test_own_company_is_refused(w):
    assert post(co=OWN_CO, **SUSPEND) == (403, {"error": "cannot change your own company's status"})
    assert "lock" not in w["log"]


def test_unknown_company_is_404(w):
    w["company"] = None
    assert post(**SUSPEND)[0] == 404
    assert call("GET", f"/companies/{CO}/status-events")[0] == 404


def test_non_uuid_company_is_404(w):
    assert post(co="not-a-uuid", **SUSPEND)[0] == 404
    assert call("GET", "/companies/not-a-uuid/status-events")[0] == 404


# ---- validation ------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"status": "deleted", "reason": "x"},
    {"status": "", "reason": "x"},
    {"status": None, "reason": "x"},
    {"status": "Suspended", "reason": "x"},
    {"reason": "x"},
    {"status": "suspended"},
    {"status": "suspended", "reason": ""},
    {"status": "suspended", "reason": "   "},
    {"status": "suspended", "reason": 7},
    {"status": "suspended", "reason": "x" * 1001},
    {"status": "suspended", "reason": "x", "stop_billing": "yes"},
    {"status": "suspended", "reason": "x", "stop_billing": 1},
])
def test_bad_body_is_400(w, body):
    status, out = post(**body)
    assert status == 400 and "error" in out
    assert "lock" not in w["log"]


def test_malformed_json_is_400(w):
    assert call("POST", f"/companies/{CO}/status", "{nope")[0] == 400


def test_reason_is_trimmed(w):
    post(status="suspended", reason="  unpaid  ")
    assert ("update", "suspended", "unpaid", False) in w["log"]


# ---- idempotency -----------------------------------------------------------------------

def test_same_status_is_a_no_op(w):
    w["current"] = "suspended"
    status, body = post(**SUSPEND)
    assert status == 200
    assert body["changed"] is False and body["status"] == "suspended"
    assert body["eventId"] is None and body["cognitoTrigger"] == "none"
    assert not [e for e in w["log"] if isinstance(e, tuple)]
    assert "commit" not in w["log"] and w["s3"].puts == []


# ---- sockets (R1) ----------------------------------------------------------------------

def test_flag_off_keeps_sockets_and_logs(w, caplog):
    with caplog.at_level(logging.WARNING):
        status, body = post(**SUSPEND)
    assert status == 200 and body["socketsClosed"] == 0
    assert "close-sockets" not in w["log"]
    lines = [r.getMessage() for r in caplog.records
             if "COMPANY_GATE_WOULD_DELETE_SOCKETS" in r.getMessage()]
    assert len(lines) == 1 and CO in lines[0]


def test_flag_on_closes_sockets(w, monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "on")
    with caplog.at_level(logging.WARNING):
        status, body = post(**SUSPEND)
    assert status == 200 and body["socketsClosed"] == 3 and "close-sockets" in w["log"]
    assert not [r for r in caplog.records if "WOULD_DELETE_SOCKETS" in r.getMessage()]


def test_reactivation_touches_no_sockets(w, monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    w["current"] = "suspended"
    status, body = post(status="active", reason="paid")
    assert status == 200 and body["socketsClosed"] == 0
    assert "close-sockets" not in w["log"] and "count-sockets" not in w["log"]


# ---- the Cognito trigger ---------------------------------------------------------------

def test_marker_is_written_after_commit(w):
    status, body = post(**SUSPEND)
    assert status == 200 and body["cognitoTrigger"] == "queued"
    assert body["cognito"] == {"disable": 4, "signout": 4, "enable": 0}
    log = [e for e in w["log"] if isinstance(e, str)]
    assert log.index("release") < log.index("commit") < log.index("put")
    [put] = w["s3"].puts
    assert put["Bucket"] == org._export_bucket_for("exports")
    assert put["Key"] == f"company_status_requests/{CO}/ev-1.json"
    doc = json.loads(put["Body"])
    assert set(doc) == {"company_id", "event_id", "requested_by", "requested_at"}
    assert doc["company_id"] == CO and doc["event_id"] == "ev-1" and doc["requested_by"] == "u1"


def test_marker_failure_does_not_fail_the_request(w):
    w["s3"].fail = True
    status, body = post(**SUSPEND)
    assert status == 200 and body["changed"] is True and body["cognitoTrigger"] == "failed"
    assert "commit" in w["log"]


def test_no_pending_rows_writes_no_marker(w):
    w["n_users"] = 0
    status, body = post(**SUSPEND)
    assert status == 200 and body["cognitoTrigger"] == "none" and w["s3"].puts == []


def test_inactive_to_inactive_creates_no_rows(w):
    w["current"] = "suspended"
    status, body = post(status="exited", reason="gone")
    assert status == 200 and body["changed"] is True
    assert body["cognito"] == {"disable": 0, "signout": 0, "enable": 0}
    assert "rows" not in w["log"] and body["cognitoTrigger"] == "none"


def test_reactivation_queues_enable_rows(w):
    w["current"] = "exited"
    w["n_enable"] = 2
    status, body = post(status="active", reason="back")
    assert status == 200 and body["cognito"] == {"disable": 0, "signout": 0, "enable": 2}
    assert "enable-rows" in w["log"] and body["cognitoTrigger"] == "queued"


def test_event_detail_carries_previous_fields_and_stop_billing(w):
    post(status="suspended", reason="x", stop_billing=True)
    d = w["detail"]
    assert d["stop_billing"] is True
    assert "billing_stopped_at" in d["previous"] and "exited_at" in d["previous"]


# ---- pre-migration ---------------------------------------------------------------------

def test_not_migrated_is_503(w):
    w["not_migrated"] = True
    assert post(**SUSPEND) == (503, {"error": "not migrated"})
    assert "commit" not in w["log"] and w["s3"].puts == []
    assert call("GET", f"/companies/{CO}/status-events") == (503, {"error": "not migrated"})


# ---- GET status-events -----------------------------------------------------------------

def test_status_events_shape(w):
    status, body = call("GET", f"/companies/{CO}/status-events")
    assert status == 200
    assert body["companyId"] == CO and body["status"] == "suspended"
    [ev] = body["events"]
    assert ev["toStatus"] == "suspended" and ev["cognito"][0]["action"] == "disable"
