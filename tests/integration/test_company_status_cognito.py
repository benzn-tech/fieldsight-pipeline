"""Integration: the company-status Cognito executor (spec 2026-11-01 §5.3, ruling R3,
Task 8) against real Postgres, end to end: the real status endpoint (Task 7) writes the
event, the work rows and the S3 marker; the real LakeOps tick (lambda_handler
{"task": "export_tick"}) reads the marker and runs every SQL statement of the executor;
Cognito and S3 are in-memory stubs.

Every call goes through `Conn`, which delegates to the test's connection but turns
commit() into a recorded no-op, so the `db` fixture rolls everything back (no cleanup of
company_status_cognito / company_status_events needed). Per-row transactions become
savepoints. Because the whole test is one transaction, now() is constant: rows are
pre-staled to 2000-01-01 where a test asserts that updated_at moved.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import contextlib
import io
import json
import uuid

import pytest
from botocore.exceptions import ClientError

from repositories import companies, company_status as repo, users

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")
exe = pytest.importorskip("company_status_cognito")

FLAG = "COMPANY_STATUS_ENFORCE"
POOL = "pool-int"


class Conn:
    def __init__(self, c):
        self._c, self.commits = c, 0

    def __getattr__(self, n):
        return getattr(self._c, n)

    def commit(self):
        self.commits += 1


class S3:
    def __init__(self):
        self.objects, self.deleted = {}, []

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body if isinstance(Body, bytes) else str(Body).encode()

    def get_paginator(self, name):
        outer = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(outer.objects)
                                    if k.startswith(Prefix)]}
        return P()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


class Cognito:
    def __init__(self):
        self.calls, self.errors, self.hook = [], {}, None

    def _call(self, action, UserPoolId, Username):
        assert UserPoolId == POOL
        self.calls.append((action, Username))
        if self.hook:
            self.hook(action, Username)
        if (action, Username) in self.errors:
            raise self.errors[(action, Username)]

    def admin_disable_user(self, **kw):
        self._call("disable", **kw)

    def admin_user_global_sign_out(self, **kw):
        self._call("signout", **kw)

    def admin_enable_user(self, **kw):
        self._call("enable", **kw)


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "Admin")


@pytest.fixture
def w(db, monkeypatch):
    """A (the target): admin aad, worker aw, archived worker ax. B: worker bw (untouched).
    P: platform_admin pa (the operator)."""
    t = uuid.uuid4().hex[:8]
    a = companies.create_company(db, f"Co-CXA-{t}")
    b = companies.create_company(db, f"Co-CXB-{t}")
    p = companies.create_company(db, f"Co-CXP-{t}")

    def mk(co, role, tag):
        u = users.upsert_user(db, f"sub-cx-{tag}-{t}", f"cx-{tag}-{t}@example.com",
                              company_id=co["id"], global_role=role)
        return users.get_user_by_sub(db, u["cognito_sub"])
    aad, aw, ax = mk(a, "admin", "aad"), mk(a, "worker", "aw"), mk(a, "worker", "ax")
    db.execute("UPDATE users SET archived_at = now() WHERE id = %s", (ax["id"],))
    bw, pa = mk(b, "worker", "bw"), mk(p, "platform_admin", "pa")
    conn = Conn(db)
    s3, cog, opened = S3(), Cognito(), []

    @contextlib.contextmanager
    def get_connection():
        opened.append(1)
        yield conn
    monkeypatch.setattr(org, "s3", lambda: s3)
    monkeypatch.setattr(org, "cognito", lambda: cog)
    monkeypatch.setattr(org, "get_connection", get_connection)
    monkeypatch.setattr(org, "COGNITO_USER_POOL_ID", POOL)
    monkeypatch.setattr(org, "LAKE_BUCKET", "lake-b", raising=False)
    monkeypatch.setenv(FLAG, "on")
    subs = sorted(u["cognito_sub"] for u in (aad, aw, ax))
    return type("W", (), dict(db=db, a=a, b=b, aad=aad, aw=aw, ax=ax, bw=bw, pa=pa,
                              conn=conn, s3=s3, cog=cog, opened=opened, subs=subs))


def _set(w, status):
    route = f"/companies/{w.a['id']}/status"
    ev = {"httpMethod": "POST", "path": "/api/org" + route, "queryStringParameters": None,
          "headers": {}, "body": json.dumps({"status": status, "reason": "test"}),
          "requestContext": {"authorizer": {"claims": {"sub": w.pa["cognito_sub"]}}}}
    res = org.dispatch(w.conn, ev, "POST", route)
    assert res["statusCode"] == 200, res
    return json.loads(res["body"])


def _events(w):
    route = f"/companies/{w.a['id']}/status-events"
    ev = {"httpMethod": "GET", "path": "/api/org" + route, "queryStringParameters": None,
          "headers": {}, "body": None,
          "requestContext": {"authorizer": {"claims": {"sub": w.pa["cognito_sub"]}}}}
    res = org.dispatch(w.conn, ev, "GET", route)
    assert res["statusCode"] == 200, res
    return json.loads(res["body"])["events"]


def _tick():
    return org.lambda_handler({"task": "export_tick"}, None)["company_status"]


def _rows(w, event_id):
    return {(r[0], r[1]): dict(state=r[2], attempts=r[3], last_error=r[4], updated_at=r[5])
            for r in w.db.execute(
                "SELECT user_sub, action, state, attempts, last_error, updated_at "
                "FROM company_status_cognito WHERE event_id = %s", (event_id,)).fetchall()}


def _stale(w, event_id):
    w.db.execute("UPDATE company_status_cognito SET updated_at = '2000-01-01' "
                 "WHERE event_id = %s", (event_id,))


def _marker(w, event_id):
    return f"company_status_requests/{w.a['id']}/{event_id}.json"


# ---- the full flow ----------------------------------------------------------------------

def test_suspend_then_tick_disables_and_signs_out_every_home_user(w):
    ev = _set(w, "suspended")["eventId"]
    assert _marker(w, ev) in w.s3.objects
    _stale(w, ev)
    out = _tick()
    assert out["done"] == 6 and out["failed"] == 0 and out["markers_deleted"] == 1
    expected = []
    for s in w.subs:
        expected += [("disable", s), ("signout", s)]
    assert w.cog.calls == expected                      # per user: disable before signout
    rows = _rows(w, ev)
    assert set(rows) == {(s, a) for s in w.subs for a in ("disable", "signout")}
    for r in rows.values():
        assert r["state"] == "done" and r["attempts"] == 1 and r["last_error"] is None
        assert r["updated_at"].year > 2000
    assert w.s3.deleted == [_marker(w, ev)]
    assert w.bw["cognito_sub"] not in {s for _, s in w.cog.calls}
    # A second tick finds nothing and opens nothing.
    opened = len(w.opened)
    assert _tick() == {"markers": 0} and len(w.opened) == opened


def test_partial_failure_is_visible_and_a_rerun_completes(w):
    ev = _set(w, "suspended")["eventId"]
    w.cog.errors[("disable", w.aw["cognito_sub"])] = _err("TooManyRequestsException")
    w.cog.errors[("signout", w.ax["cognito_sub"])] = _err("UserNotFoundException")
    out = _tick()
    assert out["failed"] == 1 and out["done"] == 5
    assert _marker(w, ev) in w.s3.objects and w.s3.deleted == []
    by = {(c["userSub"], c["action"]): c for e in _events(w) for c in e["cognito"]}
    bad = by[(w.aw["cognito_sub"], "disable")]
    assert bad["state"] == "failed" and bad["attempts"] == 1
    assert bad["lastError"] == "TooManyRequestsException: nope"
    gone = by[(w.ax["cognito_sub"], "signout")]
    assert gone["state"] == "done" and gone["lastError"] == "user not found in Cognito"
    # Re-run: only the failed row is attempted again.
    w.cog.errors.clear()
    w.cog.calls.clear()
    _stale(w, ev)
    out = _tick()
    assert w.cog.calls == [("disable", w.aw["cognito_sub"])]
    r = _rows(w, ev)[(w.aw["cognito_sub"], "disable")]
    assert r["state"] == "done" and r["attempts"] == 2 and r["last_error"] is None
    assert r["updated_at"].year > 2000
    assert _rows(w, ev)[(w.aad["cognito_sub"], "disable")]["updated_at"].year == 2000
    assert w.s3.deleted == [_marker(w, ev)]


def test_suspend_exit_active_enables_exactly_the_disabled_users(w):
    e1 = _set(w, "suspended")["eventId"]
    w.cog.errors[("disable", w.aw["cognito_sub"])] = _err("InternalErrorException")
    _tick()                                        # aad, ax disabled; aw's disable failed
    assert _set(w, "exited")["eventId"] and _set(w, "exited")["changed"] is False
    body = _set(w, "active")
    e3 = body["eventId"]
    assert body["cognito"]["enable"] == 2
    w.cog.errors.clear()
    w.cog.calls.clear()
    out = _tick()
    # The enables, for exactly the users we disabled; aw's still-retryable failed disable
    # is superseded by the status change instead of being run against an active company.
    assert sorted(w.cog.calls) == sorted([("enable", w.aad["cognito_sub"]),
                                          ("enable", w.ax["cognito_sub"])])
    assert out["superseded"] == 1 and out["done"] == 2
    enables = _rows(w, e3)
    assert set(enables) == {(w.aad["cognito_sub"], "enable"), (w.ax["cognito_sub"], "enable")}
    assert all(r["state"] == "done" for r in enables.values())
    sup = _rows(w, e1)[(w.aw["cognito_sub"], "disable")]
    assert sup["state"] == "failed" and sup["last_error"] == "superseded by status change"
    assert sup["attempts"] == 1
    assert not w.s3.objects                       # every marker gone


def test_a_concurrent_supersede_is_not_overwritten(w):
    ev = _set(w, "suspended")["eventId"]
    target = w.aad["cognito_sub"]

    def supersede(action, sub):
        if (action, sub) == ("disable", target):
            w.db.execute(
                "UPDATE company_status_cognito SET state = 'failed', last_error = %s "
                "WHERE event_id = %s AND user_sub = %s AND action = 'disable'",
                (repo.SUPERSEDED_BY_REACTIVATION, ev, target))
    w.cog.hook = supersede
    out = _tick()
    assert out["raced"] == 1
    r = _rows(w, ev)[(target, "disable")]
    assert r["state"] == "failed" and r["last_error"] == "superseded by reactivation"
    # Superseded rows are final: the marker goes, and nothing re-runs it.
    assert w.s3.deleted == [_marker(w, ev)]


def test_flag_off_tick_leaves_rows_pending_and_no_connection(w, monkeypatch):
    monkeypatch.delenv(FLAG)
    ev = _set(w, "suspended")["eventId"]
    out = _tick()
    assert out == {"markers": 1, "enforced": False}
    assert w.opened == [] and w.cog.calls == [] and _marker(w, ev) in w.s3.objects
    assert {r["state"] for r in _rows(w, ev).values()} == {"pending"}


def test_direct_task_by_event_without_a_marker(w):
    ev = _set(w, "suspended")["eventId"]
    w.s3.objects.clear()                          # the marker was lost
    out = org.lambda_handler({"task": "company_status_cognito", "event_id": ev}, None)
    assert out["enforced"] is True and out["company_id"] == str(w.a["id"])
    assert out["done"] == 6 and out["retryable_left"] == 0
    assert {r["state"] for r in _rows(w, ev).values()} == {"done"}


def test_a_marker_naming_no_event_is_dropped(w):
    key = f"company_status_requests/{w.a['id']}/{uuid.uuid4()}.json"
    eid = key.rsplit("/", 1)[1][:-5]
    w.s3.objects[key] = json.dumps({"company_id": str(w.a["id"]), "event_id": eid}).encode()
    out = _tick()
    assert out["dropped"] == 1 and w.s3.deleted == [key] and w.cog.calls == []


# ---- the repository queries on their own ------------------------------------------------

def test_retryable_selection_and_guards(w):
    ev = _set(w, "suspended")["eventId"]
    aad, aw, ax = w.aad["cognito_sub"], w.aw["cognito_sub"], w.ax["cognito_sub"]
    upd = ("UPDATE company_status_cognito SET state = %s, attempts = %s, last_error = %s "
           "WHERE event_id = %s AND user_sub = %s AND action = %s")
    w.db.execute(upd, ("done", 1, None, ev, aad, "disable"))
    w.db.execute(upd, ("failed", 4, "boom", ev, aad, "signout"))         # retryable
    w.db.execute(upd, ("failed", 5, "boom", ev, aw, "disable"))          # exhausted
    w.db.execute(upd, ("failed", 0, "superseded by reactivation", ev, aw, "signout"))
    w.db.execute(upd, ("failed", 1, None, ev, ax, "disable"))            # NULL error
    cid = w.a["id"]
    got = [(r["user_sub"], r["action"], r["state"], r["attempts"])
           for r in repo.list_retryable_cognito(w.db, cid, exe.MAX_ATTEMPTS)]
    assert sorted(got) == sorted([(aad, "signout", "failed", 4), (ax, "disable", "failed", 1),
                                  (ax, "signout", "pending", 0)])
    assert repo.count_retryable_cognito(w.db, cid, exe.MAX_ATTEMPTS) == 3
    assert repo.count_retryable_cognito(w.db, w.b["id"], exe.MAX_ATTEMPTS) == 0
    assert str(repo.event_company(w.db, ev)) == str(cid)
    assert repo.event_company(w.db, str(uuid.uuid4())) is None
    assert repo.lock_company_status(w.db, cid) == "suspended"
    assert repo.lock_company_status(w.db, str(uuid.uuid4())) is None
    # Claim: a stale snapshot (wrong attempts) and a non-retryable row are refused.
    assert repo.claim_cognito_row(w.db, ev, aad, "signout", "failed", 3, 5) is None
    assert repo.claim_cognito_row(w.db, ev, aw, "disable", "failed", 5, 5) is None
    assert repo.claim_cognito_row(w.db, ev, aad, "signout", "failed", 4, 5) == 5
    r = _rows(w, ev)[(aad, "signout")]
    assert r["state"] == "pending" and r["attempts"] == 5
    # Finish: only the claimed attempt may write.
    assert repo.finish_cognito_row(w.db, ev, aad, "signout", 4, "done", None) == 0
    assert repo.finish_cognito_row(w.db, ev, aad, "signout", 5, "failed", "x") == 1
    assert _rows(w, ev)[(aad, "signout")]["state"] == "failed"
    # Now exhausted: attempts 5 is no longer retryable.
    assert repo.count_retryable_cognito(w.db, cid, exe.MAX_ATTEMPTS) == 2
    # Supersede: guarded by the listed state/attempts, attempts unchanged.
    assert repo.supersede_cognito_row(w.db, ev, ax, "signout", "failed", 0, "s") == 0
    assert repo.supersede_cognito_row(w.db, ev, ax, "signout", "pending", 0,
                                      exe.SUPERSEDED_BY_STATUS_CHANGE) == 1
    r = _rows(w, ev)[(ax, "signout")]
    assert (r["state"], r["attempts"], r["last_error"]) == (
        "failed", 0, "superseded by status change")


def test_order_is_oldest_event_first_then_disable_before_signout(w):
    _set(w, "suspended")
    _set(w, "active")                 # nothing done: no enables, pending rows superseded
    e3 = _set(w, "suspended")["eventId"]
    rows = repo.list_retryable_cognito(w.db, w.a["id"], exe.MAX_ATTEMPTS)
    assert {str(r["event_id"]) for r in rows} == {e3}
    assert [(r["user_sub"], r["action"]) for r in rows] == [
        (s, a) for s in w.subs for a in ("disable", "signout")]
