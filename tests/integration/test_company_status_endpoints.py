"""Integration: the company status endpoints (spec 2026-11-01-company-offboarding §5.3, §5.4,
§8, §9; Task 7) against real Postgres, through the real dispatch.

The handler commits explicitly before it writes the S3 marker. Every call here goes
through `Conn`, which delegates to the test's connection but turns commit() into a
recorded no-op, so nothing is really committed (the `db` fixture rolls back) and no
company_status_events / company_status_cognito cleanup is needed. The recorded commit is
what proves "commit before marker".

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import logging
import uuid

import psycopg
import pytest

from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")

FLAG = "COMPANY_STATUS_ENFORCE"


class Conn:
    """Delegates to the test's connection; commit() is recorded, never performed."""

    def __init__(self, c, log):
        self._c, self._log = c, log
        self.commits = 0

    def __getattr__(self, n):
        return getattr(self._c, n)

    def commit(self):
        self.commits += 1
        self._log.append("commit")


class _S3:
    def __init__(self, log):
        self.log, self.puts, self.fail = log, [], False

    def put_object(self, **kw):
        if self.fail:
            raise RuntimeError("s3 down")
        self.log.append("put")
        self.puts.append(kw)


def _user(db, co, role, tag):
    u = users.upsert_user(db, f"sub-cs-{tag}", f"cs-{tag}@example.com",
                          company_id=co["id"], global_role=role)
    return users.get_user_by_sub(db, u["cognito_sub"])


@pytest.fixture
def w(db, monkeypatch):
    """A (the target): admin aad, worker aw, archived worker ax, site sa.
    B (active): worker bw, an EXTERNAL member of sa. P: platform_admin pa (operator)."""
    t = uuid.uuid4().hex[:8]
    a = companies.create_company(db, f"Co-CSA-{t}")
    b = companies.create_company(db, f"Co-CSB-{t}")
    p = companies.create_company(db, f"Co-CSP-{t}")
    sa = sites.create_site(db, a["id"], f"Site-CSA-{t}")
    sb = sites.create_site(db, b["id"], f"Site-CSB-{t}")
    aad = _user(db, a, "admin", f"aad-{t}")
    aw = _user(db, a, "worker", f"aw-{t}")
    ax = _user(db, a, "worker", f"ax-{t}")
    db.execute("UPDATE users SET archived_at = now() WHERE id = %s", (ax["id"],))
    bw = _user(db, b, "worker", f"bw-{t}")
    pa = _user(db, p, "platform_admin", f"pa-{t}")
    memberships.ensure_membership(db, aw["id"], sa["id"], "worker")
    memberships.ensure_membership(db, bw["id"], sb["id"], "worker")
    memberships.add_external_membership(db, bw["id"], sa["id"], "worker")
    # Devices: one last used by A's worker, one by B's worker.
    for tag, sub in ((f"CS-A-{t}", aw["cognito_sub"]), (f"CS-B-{t}", bw["cognito_sub"])):
        db.execute("INSERT INTO devices (id, asset_tag, last_account_sub, last_seen_at) "
                   "VALUES (%s, %s, %s, now())", (str(uuid.uuid4()), tag, sub))
    # Sockets: one of A's worker, one of B's worker (B's is on A's site's channel too).
    db.execute("INSERT INTO ws_connections (connection_id, user_id, company_id) VALUES "
               "(%s, %s, %s), (%s, %s, %s)",
               (f"conn-a-{t}", aw["id"], a["id"], f"conn-b-{t}", bw["id"], b["id"]))
    log = []
    s3 = _S3(log)
    monkeypatch.setattr(org, "s3", lambda: s3)
    monkeypatch.delenv(FLAG, raising=False)
    conn = Conn(db, log)
    # Task 9: the directory republish (its real SQL + S3 write are proven in
    # test_directory_publisher.py); recorded with the commit count at call time.
    published = []
    monkeypatch.setattr(org, "_publish_directory",
                        lambda c: published.append(conn.commits))
    return dict(a=a, b=b, p=p, sa=sa, aad=aad, aw=aw, ax=ax, bw=bw, pa=pa, t=t, s3=s3,
                log=log, conn=conn, published=published)


def _call(w, method, route, body=None, who="pa"):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": None,
          "body": json.dumps(body) if body is not None else None, "headers": {},
          "requestContext": {"authorizer": {"claims": {"sub": w[who]["cognito_sub"]}}}}
    res = org.dispatch(w["conn"], ev, method, route)
    return res["statusCode"], json.loads(res["body"])


def _set(w, status, reason="test reason", **extra):
    return _call(w, "POST", f"/companies/{w['a']['id']}/status",
                 dict(status=status, reason=reason, **extra))


def _company(db, co):
    return db.cursor(row_factory=psycopg.rows.dict_row).execute(
        "SELECT status, status_changed_at, status_reason, billing_stopped_at, exited_at "
        "FROM companies WHERE id = %s", (co["id"],)).fetchone()


def _events(db, co):
    return db.cursor(row_factory=psycopg.rows.dict_row).execute(
        "SELECT * FROM company_status_events WHERE company_id = %s ORDER BY at",
        (co["id"],)).fetchall()


def _rows(db, event_id):
    return {(r[0], r[1]): (r[2], r[3]) for r in db.execute(
        "SELECT user_sub, action, state, last_error FROM company_status_cognito "
        "WHERE event_id = %s", (event_id,)).fetchall()}


def _mark(db, event_id, sub, action, state):
    db.execute("UPDATE company_status_cognito SET state = %s "
               "WHERE event_id = %s AND user_sub = %s AND action = %s",
               (state, event_id, sub, action))


def _usable(db):
    assert db.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    assert db.execute("SELECT 1").fetchone()[0] == 1


# ---- suspend: status, audit, rows, marker ----------------------------------------------

def test_suspend_writes_status_event_rows_and_marker(db, w):
    status, body = _set(w, "suspended", reason="  unpaid invoices  ")
    assert status == 200, body
    a = str(w["a"]["id"])
    assert body["companyId"] == a and body["status"] == "suspended"
    assert body["previousStatus"] == "active" and body["changed"] is True
    assert body["affectedUsers"] == 3 and body["affectedDevices"] == 1
    assert body["cognito"] == {"disable": 3, "signout": 3, "enable": 0}
    assert body["cognitoTrigger"] == "queued" and body["socketsClosed"] == 0

    co = _company(db, w["a"])
    assert co["status"] == "suspended" and co["status_reason"] == "unpaid invoices"
    assert co["status_changed_at"] is not None
    assert co["billing_stopped_at"] is None and co["exited_at"] is None

    [ev] = _events(db, w["a"])
    assert str(ev["id"]) == body["eventId"]
    assert (ev["from_status"], ev["to_status"], ev["reason"]) == (
        "active", "suspended", "unpaid invoices")
    assert ev["actor_user_id"] == w["pa"]["id"] and ev["actor_sub"] == w["pa"]["cognito_sub"]
    assert ev["detail"]["stop_billing"] is False
    assert ev["detail"]["previous"] == {"status_reason": None, "billing_stopped_at": None,
                                        "exited_at": None}

    # Home users of A only -- archived included; B's external member of A's site never.
    homes = {w["aad"]["cognito_sub"], w["aw"]["cognito_sub"], w["ax"]["cognito_sub"]}
    assert _rows(db, ev["id"]) == {(s, act): ("pending", None)
                                   for s in homes for act in ("disable", "signout")}

    # Committed, THEN the marker.
    assert w["conn"].commits == 1 and w["log"] == ["commit", "put"]
    [put] = w["s3"].puts
    assert put["Key"] == f"company_status_requests/{a}/{body['eventId']}.json"
    doc = json.loads(put["Body"])
    assert doc["company_id"] == a and doc["event_id"] == body["eventId"]
    assert doc["requested_by"] == str(w["pa"]["id"]) and doc["requested_at"]
    _usable(db)


def test_a_status_change_republishes_the_directory_after_commit(db, w):
    """Task 9: the non-VPC senders read inactive companies from the directory, so every
    change republishes it -- after the commit, so it reflects the new status."""
    assert _set(w, "suspended")[0] == 200
    assert w["published"] == [1]
    assert _set(w, "suspended")[0] == 200          # unchanged: nothing to republish
    assert w["published"] == [1]
    assert _set(w, "active")[0] == 200             # reactivation releases held notices
    assert w["published"] == [1, 2]


def test_other_companies_get_no_rows(db, w):
    _set(w, "suspended")
    n = db.execute(
        "SELECT count(*) FROM company_status_cognito c JOIN users u ON u.cognito_sub = c.user_sub "
        "WHERE u.company_id <> %s", (w["a"]["id"],)).fetchone()[0]
    assert n == 0


def test_suspend_with_stop_billing(db, w):
    status, _ = _set(w, "suspended", stop_billing=True)
    assert status == 200
    co = _company(db, w["a"])
    assert co["billing_stopped_at"] is not None and co["exited_at"] is None
    assert _events(db, w["a"])[0]["detail"]["stop_billing"] is True


def test_exit_sets_exited_and_billing_and_reactivation_keeps_exited_at(db, w):
    assert _set(w, "exited")[0] == 200
    co = _company(db, w["a"])
    assert co["status"] == "exited"
    assert co["exited_at"] is not None and co["billing_stopped_at"] is not None

    assert _set(w, "active", reason="paid up")[0] == 200
    back = _company(db, w["a"])
    assert back["status"] == "active" and back["billing_stopped_at"] is None
    assert back["exited_at"] == co["exited_at"]

    # exited_at is the FIRST exit: a second exit keeps it.
    db.execute("UPDATE companies SET exited_at = '2020-01-01T00:00:00Z' WHERE id = %s",
               (w["a"]["id"],))
    assert _set(w, "exited", reason="gone again")[0] == 200
    again = _company(db, w["a"])
    assert again["exited_at"].year == 2020 and again["billing_stopped_at"] is not None
    ev = _events(db, w["a"])[-1]
    assert ev["detail"]["previous"]["exited_at"].startswith("2020-01-01")


def test_billing_already_stopped_is_kept(db, w):
    db.execute("UPDATE companies SET billing_stopped_at = '2021-05-05T00:00:00Z' WHERE id = %s",
               (w["a"]["id"],))
    _set(w, "exited")
    assert _company(db, w["a"])["billing_stopped_at"].year == 2021


def test_suspend_then_exit_is_an_event_without_rows(db, w):
    _set(w, "suspended")
    w["log"].clear()
    status, body = _set(w, "exited", reason="for good")
    assert status == 200 and body["changed"] is True and body["previousStatus"] == "suspended"
    assert body["cognito"] == {"disable": 0, "signout": 0, "enable": 0}
    assert body["cognitoTrigger"] == "none" and w["s3"].puts[1:] == []
    first, second = _events(db, w["a"])
    assert (second["from_status"], second["to_status"]) == ("suspended", "exited")
    assert _rows(db, second["id"]) == {}
    assert len(_rows(db, first["id"])) == 6


def test_same_status_is_idempotent(db, w):
    _set(w, "suspended")
    commits = w["conn"].commits
    status, body = _set(w, "suspended", reason="again")
    assert status == 200 and body["changed"] is False and body["eventId"] is None
    assert len(_events(db, w["a"])) == 1
    assert _company(db, w["a"])["status_reason"] == "test reason"
    assert w["conn"].commits == commits and len(w["s3"].puts) == 1


def test_active_to_active_is_a_no_op(db, w):
    status, body = _set(w, "active")
    assert status == 200 and body["changed"] is False
    assert _events(db, w["a"]) == [] and _company(db, w["a"])["status_changed_at"] is None


# ---- reactivation ----------------------------------------------------------------------

def test_reactivation_enables_only_users_actually_disabled(db, w):
    aad, aw, ax = (w[k]["cognito_sub"] for k in ("aad", "aw", "ax"))
    _, s1 = _set(w, "suspended")
    e1 = uuid.UUID(s1["eventId"])
    # The executor (Task 8) got through some of it.
    _mark(db, e1, aad, "disable", "done")
    _mark(db, e1, aw, "disable", "done")
    _mark(db, e1, aad, "signout", "done")
    _, s2 = _set(w, "exited")
    w["log"].clear()

    status, body = _set(w, "active", reason="back")
    assert status == 200 and body["changed"] is True
    assert body["cognito"] == {"disable": 0, "signout": 0, "enable": 2}
    assert body["cognitoTrigger"] == "queued" and w["log"] == ["commit", "put"]
    e3 = uuid.UUID(body["eventId"])

    sup = "superseded by reactivation"
    assert _rows(db, e1) == {
        (aad, "disable"): ("done", None), (aw, "disable"): ("done", None),
        (ax, "disable"): ("failed", sup),
        (aad, "signout"): ("done", None), (aw, "signout"): ("failed", sup),
        (ax, "signout"): ("failed", sup)}
    assert _rows(db, uuid.UUID(s2["eventId"])) == {}
    assert _rows(db, e3) == {(aad, "enable"): ("pending", None),
                             (aw, "enable"): ("pending", None)}


def test_reactivation_window_starts_at_the_last_active(db, w):
    """Users disabled in an EARLIER inactive period (before the last reactivation) are not
    enabled again by this reactivation -- the union covers events since last active."""
    aad, ax = w["aad"]["cognito_sub"], w["ax"]["cognito_sub"]
    _, s1 = _set(w, "suspended")
    _mark(db, uuid.UUID(s1["eventId"]), ax, "disable", "done")
    _, r1 = _set(w, "active")
    assert _rows(db, uuid.UUID(r1["eventId"])) == {(ax, "enable"): ("pending", None)}
    _mark(db, uuid.UUID(r1["eventId"]), ax, "enable", "done")

    _, s2 = _set(w, "suspended")
    _mark(db, uuid.UUID(s2["eventId"]), aad, "disable", "done")
    _, r2 = _set(w, "active")
    assert _rows(db, uuid.UUID(r2["eventId"])) == {(aad, "enable"): ("pending", None)}


def test_superseded_enable_does_not_strand_a_disabled_user(db, w):
    """E1 suspend (U's disable done) -> E3 active (enable pending) -> E4 suspend before the
    tick (E3's enable superseded, E4's disable pending) -> E5 active before the tick (E4's
    disable superseded). U is still disabled from E1, so E5 must enable U."""
    u = w["aw"]["cognito_sub"]
    _, e1 = _set(w, "suspended")
    _mark(db, uuid.UUID(e1["eventId"]), u, "disable", "done")
    _, e3 = _set(w, "active")
    _, e4 = _set(w, "suspended")
    status, e5 = _set(w, "active")
    assert status == 200
    assert _rows(db, uuid.UUID(e3["eventId"])) == {
        (u, "enable"): ("failed", "superseded by deactivation")}
    assert _rows(db, uuid.UUID(e4["eventId"]))[(u, "disable")] == (
        "failed", "superseded by reactivation")
    assert _rows(db, uuid.UUID(e5["eventId"])) == {(u, "enable"): ("pending", None)}
    assert e5["cognito"]["enable"] == 1 and e5["cognitoTrigger"] == "queued"


def test_user_whose_latest_done_is_enable_gets_no_enable(db, w):
    """Disabled and then re-enabled (both done): not disabled by us any more, so a later
    reactivation (after a suspension the executor never processed) enables nobody."""
    u = w["aad"]["cognito_sub"]
    _, s1 = _set(w, "suspended")
    _mark(db, uuid.UUID(s1["eventId"]), u, "disable", "done")
    _, r1 = _set(w, "active")
    _mark(db, uuid.UUID(r1["eventId"]), u, "enable", "done")
    _set(w, "suspended")
    status, r2 = _set(w, "active")
    assert status == 200 and r2["cognito"]["enable"] == 0
    assert _rows(db, uuid.UUID(r2["eventId"])) == {} and r2["cognitoTrigger"] == "none"


def test_latest_done_is_ordered_by_updated_at(db, w):
    """An enable marked done AFTER a later event's disable (updated_at) wins over it."""
    u = w["aad"]["cognito_sub"]
    _, s1 = _set(w, "suspended")
    e1 = uuid.UUID(s1["eventId"])
    _set(w, "active")
    _, s2 = _set(w, "suspended")
    e2 = uuid.UUID(s2["eventId"])
    db.execute("UPDATE company_status_cognito SET state='done', updated_at=%s "
               "WHERE event_id=%s AND user_sub=%s AND action='disable'",
               ("2026-01-01T00:00:00Z", e2, u))
    db.execute("UPDATE company_status_cognito SET state='done', updated_at=%s "
               "WHERE event_id=%s AND user_sub=%s AND action='disable'",
               ("2026-01-02T00:00:00Z", e1, u))
    _, r2 = _set(w, "active")
    assert _rows(db, uuid.UUID(r2["eventId"])) == {(u, "enable"): ("pending", None)}
    # An enable done later than every disable: nobody to enable next time.
    db.execute("UPDATE company_status_cognito SET state='done', updated_at=%s "
               "WHERE event_id=%s AND user_sub=%s AND action='enable'",
               ("2026-01-03T00:00:00Z", uuid.UUID(r2["eventId"]), u))
    _set(w, "suspended")
    _, r3 = _set(w, "active")
    assert _rows(db, uuid.UUID(r3["eventId"])) == {}


def test_deactivation_supersedes_pending_enables(db, w):
    ax = w["ax"]["cognito_sub"]
    _, s1 = _set(w, "suspended")
    _mark(db, uuid.UUID(s1["eventId"]), ax, "disable", "done")
    _, r1 = _set(w, "active")
    _, s2 = _set(w, "suspended")
    assert _rows(db, uuid.UUID(r1["eventId"])) == {
        (ax, "enable"): ("failed", "superseded by deactivation")}
    assert len(_rows(db, uuid.UUID(s2["eventId"]))) == 6


def test_reactivation_with_nothing_disabled_queues_nothing(db, w):
    _set(w, "suspended")
    w["log"].clear()
    status, body = _set(w, "active")
    assert status == 200 and body["cognito"]["enable"] == 0
    assert body["cognitoTrigger"] == "none" and w["log"] == ["commit"]


# ---- atomicity -------------------------------------------------------------------------

def test_status_and_event_are_one_transaction(db, w, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("forced after the UPDATE")
    monkeypatch.setattr(org.company_status_repo, "insert_event", boom)
    with pytest.raises(RuntimeError):
        _set(w, "suspended")
    _usable(db)
    co = _company(db, w["a"])
    assert co["status"] == "active" and co["status_changed_at"] is None
    assert _events(db, w["a"]) == []
    assert w["conn"].commits == 0 and w["s3"].puts == []


def test_rows_failure_rolls_back_status_and_event(db, w, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("forced after the event")
    monkeypatch.setattr(org.company_status_repo, "create_deactivation_rows", boom)
    with pytest.raises(RuntimeError):
        _set(w, "suspended")
    _usable(db)
    assert _company(db, w["a"])["status"] == "active" and _events(db, w["a"]) == []


# ---- sockets ---------------------------------------------------------------------------

def _sockets(db, w):
    return {r[0] for r in db.execute(
        "SELECT connection_id FROM ws_connections WHERE connection_id = ANY(%s)",
        ([f"conn-a-{w['t']}", f"conn-b-{w['t']}"],)).fetchall()}


def test_flag_on_deletes_the_company_sockets(db, w, monkeypatch):
    monkeypatch.setenv(FLAG, "on")
    status, body = _set(w, "suspended")
    assert status == 200 and body["socketsClosed"] == 1
    assert _sockets(db, w) == {f"conn-b-{w['t']}"}


def test_flag_off_keeps_sockets_and_logs(db, w, caplog):
    with caplog.at_level(logging.WARNING):
        status, body = _set(w, "exited")
    assert status == 200 and body["socketsClosed"] == 0
    assert _sockets(db, w) == {f"conn-a-{w['t']}", f"conn-b-{w['t']}"}
    [line] = [r.getMessage() for r in caplog.records
              if "COMPANY_GATE_WOULD_DELETE_SOCKETS" in r.getMessage()]
    assert str(w["a"]["id"]) in line and "sockets=1" in line


# ---- marker failure --------------------------------------------------------------------

def test_marker_failure_still_commits(db, w):
    w["s3"].fail = True
    status, body = _set(w, "suspended")
    assert status == 200 and body["cognitoTrigger"] == "failed"
    assert w["conn"].commits == 1 and _company(db, w["a"])["status"] == "suspended"


# ---- auth through dispatch -------------------------------------------------------------

def test_own_company_refused(db, w):
    status, body = _call(w, "POST", f"/companies/{w['p']['id']}/status",
                         {"status": "suspended", "reason": "x"})
    assert (status, body) == (403, {"error": "cannot change your own company's status"})
    assert _company(db, w["p"])["status"] == "active"


def test_company_admin_refused(db, w):
    status, _ = _call(w, "POST", f"/companies/{w['b']['id']}/status",
                      {"status": "suspended", "reason": "x"}, who="aad")
    assert status == 403 and _company(db, w["b"])["status"] == "active"


def test_unknown_company_404(db, w):
    status, _ = _call(w, "POST", f"/companies/{uuid.uuid4()}/status",
                      {"status": "suspended", "reason": "x"})
    assert status == 404
    _usable(db)


# ---- GET status-events / GET companies -------------------------------------------------

def test_status_events_newest_first_with_cognito_rows(db, w):
    _, s1 = _set(w, "suspended", reason="first")
    _mark(db, uuid.UUID(s1["eventId"]), w["aw"]["cognito_sub"], "disable", "done")
    _, s2 = _set(w, "exited", reason="second", stop_billing=True)
    status, body = _call(w, "GET", f"/companies/{w['a']['id']}/status-events")
    assert status == 200, body
    assert body["companyId"] == str(w["a"]["id"]) and body["status"] == "exited"
    assert body["statusReason"] == "second"
    assert body["exitedAt"] and body["billingStoppedAt"] and body["statusChangedAt"]
    assert [e["id"] for e in body["events"]] == [s2["eventId"], s1["eventId"]]
    newest, oldest = body["events"]
    assert newest["cognito"] == [] and newest["fromStatus"] == "suspended"
    assert newest["actorUserId"] == str(w["pa"]["id"]) and newest["detail"]["stop_billing"]
    assert len(oldest["cognito"]) == 6
    row = next(c for c in oldest["cognito"]
               if c["userSub"] == w["aw"]["cognito_sub"] and c["action"] == "disable")
    assert row["state"] == "done" and row["attempts"] == 0 and row["lastError"] is None
    assert row["updatedAt"]


def test_status_events_for_a_company_with_none(db, w):
    status, body = _call(w, "GET", f"/companies/{w['b']['id']}/status-events")
    assert status == 200 and body["events"] == [] and body["status"] == "active"


def test_list_companies_carries_status(db, w):
    _set(w, "suspended")
    status, body = _call(w, "GET", "/companies")
    assert status == 200
    by_id = {c["id"]: c for c in body["companies"]}
    assert by_id[str(w["a"]["id"])]["status"] == "suspended"
    assert by_id[str(w["b"]["id"])]["status"] == "active"
    assert set(by_id[str(w["a"]["id"])]) == {"id", "name", "status"}


# ---- not migrated ----------------------------------------------------------------------

def test_missing_events_table_is_503_and_writes_nothing(db, w):
    db.execute("ALTER TABLE company_status_events RENAME TO company_status_events_gone")
    assert _set(w, "suspended") == (503, {"error": "not migrated"})
    _usable(db)
    assert _company(db, w["a"])["status"] == "active"
    assert w["conn"].commits == 0 and w["s3"].puts == []
    assert _call(w, "GET", f"/companies/{w['a']['id']}/status-events") == (
        503, {"error": "not migrated"})
    _usable(db)


def test_missing_cognito_table_is_503(db, w):
    db.execute("ALTER TABLE company_status_cognito RENAME TO company_status_cognito_gone")
    assert _set(w, "suspended") == (503, {"error": "not migrated"})
    _usable(db)
    assert _company(db, w["a"])["status"] == "active"


def test_missing_status_column_is_503_and_companies_read_active(db, w):
    db.execute("ALTER TABLE companies RENAME COLUMN status TO status_gone")
    assert _set(w, "suspended") == (503, {"error": "not migrated"})
    _usable(db)
    status, body = _call(w, "GET", "/companies")
    assert status == 200 and body["companies"]
    assert {c["status"] for c in body["companies"]} == {"active"}
    _usable(db)
