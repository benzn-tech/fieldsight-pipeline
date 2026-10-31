"""Integration: repositories.action_packs against real Postgres (migration 0095).

Every statement in the module runs here -- a fake connection never executes SQL. The claim
race and the cross-connection one-active-pack test commit their seed rows (the `db` fixture
rolls back, so a second connection could not see them) and delete them in `finally`.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as dt
import threading
import uuid

import psycopg
import pytest

from repositories import action_packs as ap
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- fixtures / helpers

def _co(db, tag):
    return companies.create_company(db, f"Co-ap-{tag}-{uuid.uuid4().hex[:8]}")


def _user(db, co, role="worker"):
    tag = uuid.uuid4().hex[:8]
    return users.upsert_user(db, f"sub-ap-{tag}", f"ap-{tag}@example.com",
                             company_id=co["id"], global_role=role)


def _world(db):
    co = _co(db, "a")
    site = sites.create_site(db, co["id"], f"Site-ap-{uuid.uuid4().hex[:6]}")
    creator = _user(db, co)
    memberships.ensure_membership(db, creator["id"], site["id"], "worker")
    return co, site, creator


def _reader(db, user):
    return ap.reader_for(user, memberships.caller_site_roles(db, user["id"]))


def _payload(**over):
    p = {
        "title": "Close the fire-door items",
        "reading": "Close #69 on Tower A",
        "conclusion": [{"text": "Door fixed", "refs": ["R1"], "confidence": "supported"}],
        "run_trace": {"rounds": 3, "tool_calls": 7},
        "models": {"planner": "m1", "writer": "m2", "verifier": "m3"},
        "as_of": "2026-11-01T01:02:03+00:00",
        "sources": [
            {"ref": "R1", "kind": "topic", "provenance": "records",
             "topic_id": str(uuid.uuid4()), "title": "Fire doors", "date": "2026-10-30",
             "who": "Sam", "excerpt": "the door is fixed"},
            {"ref": "W1", "kind": "web_source", "provenance": "web",
             "url": "https://example.com/a", "fetched_at": "2026-11-01T01:00:00+00:00",
             "snapshot_s3_key": "action_packs/c/p/web/W1.txt", "excerpt": "page text"},
        ],
        "actions": [
            {"seq": 1, "type": "close_item", "target_stable_id": str(uuid.uuid4()),
             "target_snapshot": {"status": "open", "deadline": None},
             "proposed": {"status": "done"}, "rationale": "Reported fixed [R1]",
             "refs": ["R1"], "verifier": "supported"},
            {"seq": 2, "type": "draft_email", "proposed": {"body": "Hi"},
             "rationale": "Tell the client [W1]", "refs": ["R1", "W1"], "verifier": "weak"},
        ],
    }
    p.update(over)
    return p


def _running(db, site, creator):
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"],
                            instruction="close #69")
    assert ap.claim(db, pack["id"]) is True
    return pack


def _state(db, pid):
    return db.execute("SELECT state, failure FROM action_packs WHERE id=%s", (pid,)).fetchone()


# ---------------------------------------------------------------- create_queued

def test_create_queued_takes_company_from_the_site(db):
    co, site, creator = _world(db)
    other = _co(db, "caller")
    ext = _user(db, other)
    memberships.add_external_membership(db, ext["id"], site["id"], "worker")
    pack = ap.create_queued(db, site_id=site["id"], created_by=ext["id"], instruction="do it")
    assert pack["company_id"] == co["id"]          # the SITE's company, not the caller's
    assert pack["site_id"] == site["id"] and pack["created_by"] == ext["id"]
    assert pack["state"] == "queued" and pack["failure"] is None
    assert pack["instruction"] == "do it"
    assert pack["heartbeat_at"] is not None        # liveness stamp from creation


def test_create_queued_unknown_site_returns_none(db):
    _, _, creator = _world(db)
    assert ap.create_queued(db, site_id=uuid.uuid4(), created_by=creator["id"],
                            instruction="x") is None


def test_create_queued_second_active_pack_raises_and_connection_stays_usable(db):
    _, site, creator = _world(db)
    first = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="a")
    with pytest.raises(ap.ActivePackExists):
        ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="b")
    # savepoint: the outer transaction is not aborted
    assert db.execute("SELECT 1").fetchone() == (1,)
    assert ap.claim(db, first["id"]) is True
    with pytest.raises(ap.ActivePackExists):        # running counts as active too
        ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="b")
    assert ap.fail(db, first["id"], "internal") is True
    second = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="b")
    assert second["id"] != first["id"]


def test_active_exists_is_a_distinct_exception():
    assert issubclass(ap.ActivePackExists, Exception)
    assert not issubclass(ap.ActivePackExists, psycopg.Error)


# ---------------------------------------------------------------- committed-data tests

@pytest.fixture
def committed(migrated_db_url):
    """Autocommit connection + committed seed world; everything deleted afterwards."""
    conn = psycopg.connect(migrated_db_url, autocommit=True)
    co = _co(conn, "race")
    site = sites.create_site(conn, co["id"], f"Site-race-{uuid.uuid4().hex[:6]}")
    creator = _user(conn, co)
    try:
        yield migrated_db_url, conn, co, site, creator
    finally:
        conn.execute("DELETE FROM action_packs WHERE company_id=%s", (co["id"],))
        conn.execute("DELETE FROM users WHERE id=%s", (creator["id"],))
        conn.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
        conn.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
        conn.close()


def test_claim_race_exactly_one_wins(committed):
    url, conn, _, site, creator = committed
    pack = ap.create_queued(conn, site_id=site["id"], created_by=creator["id"], instruction="x")
    a = psycopg.connect(url)
    b = psycopg.connect(url)
    try:
        assert ap.claim(a, pack["id"]) is True       # holds the row lock, not committed yet
        result = {}

        def _second():
            result["b"] = ap.claim(b, pack["id"])    # blocks on the lock, then re-checks
            b.commit()

        t = threading.Thread(target=_second)
        t.start()
        t.join(0.5)
        assert t.is_alive(), "second claim should wait on the first one's row lock"
        a.commit()
        t.join(10)
        assert result == {"b": False}
    finally:
        a.close()
        b.close()
    assert _state(conn, pack["id"]) == ("running", None)


def test_claim_race_threads_both_start_together(committed):
    url, conn, _, site, creator = committed
    pack = ap.create_queued(conn, site_id=site["id"], created_by=creator["id"], instruction="x")
    barrier = threading.Barrier(2)
    out = []

    def _go():
        with psycopg.connect(url) as c:
            barrier.wait()
            out.append(ap.claim(c, pack["id"]))

    ts = [threading.Thread(target=_go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert sorted(out) == [False, True]


@pytest.mark.parametrize("first", ["store", "cancel"])
def test_store_vs_cancel_race(committed, first):
    """Whoever takes the row lock first wins; the other waits, re-checks, and loses."""
    url, conn, _, site, creator = committed
    pack = ap.create_queued(conn, site_id=site["id"], created_by=creator["id"], instruction="x")
    assert ap.claim(conn, pack["id"]) is True
    ops = {"store": lambda c: ap.store_result(c, pack["id"], _payload()),
           "cancel": lambda c: ap.cancel(c, pack["id"])}
    second = "cancel" if first == "store" else "store"
    a = psycopg.connect(url)
    b = psycopg.connect(url)
    try:
        # Open each connection's transaction first (as org-api's request does), so
        # store_result's `with conn.transaction()` is a savepoint, not a commit of its own.
        a.execute("SELECT 1")
        b.execute("SELECT 1")
        ops[first](a)                                 # holds the lock, uncommitted
        result = {}

        def _second():
            result["v"] = ops[second](b)
            b.commit()

        t = threading.Thread(target=_second)
        t.start()
        t.join(0.5)
        assert t.is_alive(), "the second writer should wait on the first one's row lock"
        a.commit()
        t.join(10)
    finally:
        a.close()
        b.close()
    rows = conn.execute("SELECT (SELECT count(*) FROM action_pack_sources WHERE pack_id=%s), "
                        "(SELECT count(*) FROM action_pack_actions WHERE pack_id=%s)",
                        (pack["id"], pack["id"])).fetchone()
    if first == "store":
        assert result == {"v": False}                 # cancel lost
        assert _state(conn, pack["id"]) == ("ready", None)
        assert rows == (2, 2)
    else:
        assert result == {"v": "refused"}             # store lost, wrote nothing
        assert _state(conn, pack["id"]) == ("cancelled", None)
        assert rows == (0, 0)


def test_one_active_pack_across_two_connections(committed):
    url, conn, _, site, creator = committed
    ap.create_queued(conn, site_id=site["id"], created_by=creator["id"], instruction="x")
    with psycopg.connect(url) as other:
        with pytest.raises(ap.ActivePackExists):
            ap.create_queued(other, site_id=site["id"], created_by=creator["id"],
                             instruction="y")


# ---------------------------------------------------------------- claim / heartbeat / fail / cancel

def test_claim_only_from_queued(db):
    _, site, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    db.execute("UPDATE action_packs SET heartbeat_at = now() - interval '1 hour' WHERE id=%s",
               (pack["id"],))
    assert ap.claim(db, pack["id"]) is True
    row = db.execute("SELECT state, heartbeat_at > now() - interval '1 minute' FROM action_packs "
                     "WHERE id=%s", (pack["id"],)).fetchone()
    assert row == ("running", True)
    assert ap.claim(db, pack["id"]) is False
    assert ap.claim(db, uuid.uuid4()) is False


def test_heartbeat_refreshes_only_while_running_and_reports_state(db):
    _, site, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    assert ap.heartbeat(db, pack["id"]) == "queued"     # not running: no stamp, state told
    ap.claim(db, pack["id"])
    db.execute("UPDATE action_packs SET heartbeat_at = now() - interval '1 hour' WHERE id=%s",
               (pack["id"],))
    assert ap.heartbeat(db, pack["id"]) == "running"
    fresh = db.execute("SELECT heartbeat_at > now() - interval '1 minute' FROM action_packs "
                       "WHERE id=%s", (pack["id"],)).fetchone()[0]
    assert fresh is True
    assert ap.cancel(db, pack["id"]) is True
    db.execute("UPDATE action_packs SET heartbeat_at = now() - interval '1 hour' WHERE id=%s",
               (pack["id"],))
    assert ap.heartbeat(db, pack["id"]) == "cancelled"   # the worker learns of the cancel
    stale = db.execute("SELECT heartbeat_at < now() - interval '30 minutes' FROM action_packs "
                       "WHERE id=%s", (pack["id"],)).fetchone()[0]
    assert stale is True                                 # and nothing was stamped
    assert ap.heartbeat(db, uuid.uuid4()) is None


def test_fail_from_queued_or_running_only(db):
    _, site, creator = _world(db)
    p1 = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    assert ap.fail(db, p1["id"], "company_inactive") is True
    assert _state(db, p1["id"]) == ("failed", "company_inactive")
    assert ap.fail(db, p1["id"], "internal") is False         # already failed: unchanged
    assert _state(db, p1["id"]) == ("failed", "company_inactive")

    p2 = _running(db, site, creator)
    assert ap.fail(db, p2["id"], "model", run_trace={"rounds": 2}) is True
    row = db.execute("SELECT state, failure, run_trace FROM action_packs WHERE id=%s",
                     (p2["id"],)).fetchone()
    assert row == ("failed", "model", {"rounds": 2})

    p3 = _running(db, site, creator)
    assert ap.store_result(db, p3["id"], _payload()) == "ok"
    assert ap.fail(db, p3["id"], "internal") is False         # never overwrites ready
    assert _state(db, p3["id"]) == ("ready", None)

    p4 = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    ap.cancel(db, p4["id"])
    assert ap.fail(db, p4["id"], "internal") is False         # never overwrites cancelled
    assert _state(db, p4["id"]) == ("cancelled", None)

    with pytest.raises(ValueError):
        ap.fail(db, p4["id"], "because")
    assert ap.fail(db, uuid.uuid4(), "internal") is False


def test_cancel_from_queued_or_running_only(db):
    _, site, creator = _world(db)
    p1 = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    assert ap.cancel(db, p1["id"]) is True
    assert ap.cancel(db, p1["id"]) is False
    p2 = _running(db, site, creator)
    assert ap.cancel(db, p2["id"]) is True
    assert _state(db, p2["id"]) == ("cancelled", None)
    p3 = _running(db, site, creator)
    ap.store_result(db, p3["id"], _payload())
    assert ap.cancel(db, p3["id"]) is False
    assert _state(db, p3["id"]) == ("ready", None)


# ---------------------------------------------------------------- store_result

def test_store_result_writes_pack_sources_actions(db):
    _, site, creator = _world(db)
    pack = _running(db, site, creator)
    payload = _payload()
    assert ap.store_result(db, pack["id"], payload) == "ok"
    row = db.execute(
        "SELECT state, failure, title, reading, conclusion, run_trace, models, as_of "
        "FROM action_packs WHERE id=%s", (pack["id"],)).fetchone()
    assert row[:7] == ("ready", None, payload["title"], payload["reading"],
                       payload["conclusion"], payload["run_trace"], payload["models"])
    assert row[7] == dt.datetime(2026, 11, 1, 1, 2, 3, tzinfo=dt.timezone.utc)
    srcs = db.execute("SELECT ref, kind, provenance, topic_id::text, title, date, who, excerpt, "
                      "url, snapshot_s3_key FROM action_pack_sources WHERE pack_id=%s "
                      "ORDER BY ref", (pack["id"],)).fetchall()
    s0 = payload["sources"][0]
    assert srcs[0] == ("R1", "topic", "records", s0["topic_id"], "Fire doors",
                       dt.date(2026, 10, 30), "Sam", "the door is fixed", None, None)
    assert srcs[1][:3] == ("W1", "web_source", "web")
    assert srcs[1][8:] == ("https://example.com/a", "action_packs/c/p/web/W1.txt")
    acts = db.execute("SELECT seq, type, target_stable_id::text, target_snapshot, proposed, "
                      "rationale, refs, verifier, state FROM action_pack_actions "
                      "WHERE pack_id=%s ORDER BY seq", (pack["id"],)).fetchall()
    a0 = payload["actions"][0]
    assert acts[0] == (1, "close_item", a0["target_stable_id"], a0["target_snapshot"],
                       {"status": "done"}, "Reported fixed [R1]", ["R1"], "supported",
                       "proposed")
    assert acts[1][:2] == (2, "draft_email") and acts[1][6] == ["R1", "W1"]


def test_store_result_refused_unless_running(db):
    _, site, creator = _world(db)
    queued = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    assert ap.store_result(db, queued["id"], _payload()) == "refused"
    ap.cancel(db, queued["id"])
    assert ap.store_result(db, queued["id"], _payload()) == "refused"
    assert _state(db, queued["id"]) == ("cancelled", None)
    failed = _running(db, site, creator)
    ap.fail(db, failed["id"], "budget")
    assert ap.store_result(db, failed["id"], _payload()) == "refused"
    assert _state(db, failed["id"]) == ("failed", "budget")
    done = _running(db, site, creator)
    assert ap.store_result(db, done["id"], _payload()) == "ok"
    assert ap.store_result(db, done["id"], _payload(title="again")) == "refused"
    for pid in (queued["id"], failed["id"]):
        n = db.execute("SELECT (SELECT count(*) FROM action_pack_sources WHERE pack_id=%s) + "
                       "(SELECT count(*) FROM action_pack_actions WHERE pack_id=%s)",
                       (pid, pid)).fetchone()[0]
        assert n == 0
    assert ap.store_result(db, uuid.uuid4(), _payload()) == "refused"


@pytest.mark.parametrize("breakage", ["bad_action_type", "dup_seq", "bad_source_kind",
                                      "dup_ref"])
def test_store_result_is_atomic(db, breakage):
    _, site, creator = _world(db)
    pack = _running(db, site, creator)
    p = _payload()
    if breakage == "bad_action_type":
        p["actions"][1]["type"] = "delete_everything"
    elif breakage == "dup_seq":
        p["actions"][1]["seq"] = 1
    elif breakage == "bad_source_kind":
        p["sources"][1]["kind"] = "photo"
    else:
        p["sources"].append(dict(p["sources"][0]))
    with pytest.raises(psycopg.Error):
        ap.store_result(db, pack["id"], p)
    # the outer transaction is still usable, the pack untouched, no rows written
    row = db.execute("SELECT state, title, reading, conclusion, as_of FROM action_packs "
                     "WHERE id=%s", (pack["id"],)).fetchone()
    assert row == ("running", None, None, None, None)
    n = db.execute("SELECT (SELECT count(*) FROM action_pack_sources WHERE pack_id=%s) + "
                   "(SELECT count(*) FROM action_pack_actions WHERE pack_id=%s)",
                   (pack["id"], pack["id"])).fetchone()[0]
    assert n == 0


def test_store_result_validates_payload_before_writing(db):
    _, site, creator = _world(db)
    pack = _running(db, site, creator)
    bad_ref = _payload()
    bad_ref["actions"][0]["refs"] = ["R9"]                 # cites a ref with no source row
    with pytest.raises(ValueError):
        ap.store_result(db, pack["id"], bad_ref)
    bad_claim = _payload(conclusion=[{"text": "x", "refs": ["Z1"], "confidence": "supported"}])
    with pytest.raises(ValueError):
        ap.store_result(db, pack["id"], bad_claim)
    with pytest.raises(ValueError):
        ap.store_result(db, pack["id"], _payload(surprise=1))   # unknown pack key
    typo = _payload()
    typo["sources"][0]["exerpt"] = "x"
    with pytest.raises(ValueError):
        ap.store_result(db, pack["id"], typo)
    typo = _payload()
    typo["actions"][0]["ref"] = ["R1"]
    with pytest.raises(ValueError):
        ap.store_result(db, pack["id"], typo)
    assert _state(db, pack["id"]) == ("running", None)


def test_store_result_with_no_actions_or_sources(db):
    _, site, creator = _world(db)
    pack = _running(db, site, creator)
    assert ap.store_result(db, pack["id"], _payload(sources=[], actions=[], conclusion=[])) == "ok"
    assert _state(db, pack["id"]) == ("ready", None)


# ---------------------------------------------------------------- sweep_stale

def test_sweep_fails_stale_running_and_queued_only(db):
    co, site, _ = _world(db)
    mk = []
    for _ in range(5):
        u = _user(db, co)
        mk.append(ap.create_queued(db, site_id=site["id"], created_by=u["id"], instruction="x"))
    old_q, new_q, old_r, new_r, old_ready = (p["id"] for p in mk)
    for pid in (old_r, new_r, old_ready):
        ap.claim(db, pid)
    ap.store_result(db, old_ready, _payload())
    db.execute("UPDATE action_packs SET heartbeat_at = now() - interval '25 minutes' "
               "WHERE id = ANY(%s)", ([old_q, old_r, old_ready],))
    n = ap.sweep_stale(db, dt.timedelta(minutes=20))
    # count is over the whole table (other tests' rows are rolled back, but another
    # suite's committed stale rows could exist), so check ours explicitly
    assert n >= 2
    assert _state(db, old_q) == ("failed", "internal")
    assert _state(db, old_r) == ("failed", "internal")
    assert _state(db, new_q) == ("queued", None)
    assert _state(db, new_r) == ("running", None)
    assert _state(db, old_ready) == ("ready", None)
    assert ap.sweep_stale(db, dt.timedelta(minutes=20)) == 0


# ---------------------------------------------------------------- set_action_state / archive

def _ready_pack(db, site, creator):
    pack = _running(db, site, creator)
    ap.store_result(db, pack["id"], _payload())
    ids = [r[0] for r in db.execute("SELECT id FROM action_pack_actions WHERE pack_id=%s "
                                    "ORDER BY seq", (pack["id"],)).fetchall()]
    return pack, ids


def _action_state(db, aid):
    return db.execute("SELECT state FROM action_pack_actions WHERE id=%s", (aid,)).fetchone()[0]


def test_set_action_state_applied(db):
    _, site, creator = _world(db)
    pack, (a1, _) = _ready_pack(db, site, creator)
    new_sid = uuid.uuid4()
    outcome, row = ap.set_action_state(db, pack["id"], a1, "applied", applied_by=creator["id"],
                                       applied_result={"item_id": "x", "edit_ids": [1]},
                                       created_item_stable_id=new_sid)
    assert outcome == "ok"
    assert row["state"] == "applied" and row["applied_by"] == creator["id"]
    assert row["applied_result"] == {"item_id": "x", "edit_ids": [1]}
    assert row["created_item_stable_id"] == new_sid and row["applied_at"] is not None
    # applied is terminal; the replay is told the current state without a re-read
    for st in ("dismissed", "stale", "failed", "applied"):
        assert ap.set_action_state(db, pack["id"], a1, st) == ("illegal", "applied")
    assert _action_state(db, a1) == "applied"


# from -> the targets that are legal from it; every (from, to) pair is exercised
_LEGAL = {
    "proposed": {"applied", "dismissed", "stale", "failed"},
    "stale": {"dismissed"},
    "failed": {"applied", "dismissed", "stale", "failed"},
    "applied": set(),
    "dismissed": set(),
}


@pytest.mark.parametrize("src", sorted(_LEGAL))
@pytest.mark.parametrize("dst", ["applied", "dismissed", "stale", "failed"])
def test_action_transition_table(db, src, dst):
    _, site, creator = _world(db)
    pack, (a1, _) = _ready_pack(db, site, creator)
    if src != "proposed":
        db.execute("UPDATE action_pack_actions SET state=%s WHERE id=%s", (src, a1))
    outcome, value = ap.set_action_state(db, pack["id"], a1, dst, applied_by=creator["id"])
    if dst in _LEGAL[src]:
        assert outcome == "ok" and value["state"] == dst
        assert _action_state(db, a1) == dst
    else:
        assert (outcome, value) == ("illegal", src)
        assert _action_state(db, a1) == src


def test_failed_is_retryable_and_keeps_its_error_until_replaced(db):
    _, site, creator = _world(db)
    pack, (a1, _) = _ready_pack(db, site, creator)
    ap.set_action_state(db, pack["id"], a1, "failed", applied_by=creator["id"],
                        applied_result={"error": "acl"})
    outcome, row = ap.set_action_state(db, pack["id"], a1, "failed")
    assert outcome == "ok" and row["applied_result"] == {"error": "acl"}   # None keeps it
    outcome, row = ap.set_action_state(db, pack["id"], a1, "applied",
                                       applied_result={"item_id": "y"})
    assert outcome == "ok" and row["applied_result"] == {"item_id": "y"}


def test_stale_dismissed_keeps_both_values(db):
    _, site, creator = _world(db)
    pack, (a1, _) = _ready_pack(db, site, creator)
    both = {"live": {"status": "done"}, "snapshot": {"status": "open"}}
    ap.set_action_state(db, pack["id"], a1, "stale", applied_result=both)
    outcome, row = ap.set_action_state(db, pack["id"], a1, "dismissed",
                                       applied_by=creator["id"])
    assert outcome == "ok" and row["applied_result"] == both
    assert row["applied_by"] == creator["id"]


def test_set_action_state_guards(db):
    _, site, creator = _world(db)
    pack, (a1, _) = _ready_pack(db, site, creator)
    other, _ = _ready_pack(db, site, creator)
    with pytest.raises(ValueError):
        ap.set_action_state(db, pack["id"], a1, "proposed")
    with pytest.raises(ValueError):
        ap.set_action_state(db, pack["id"], a1, "done")
    assert ap.set_action_state(db, other["id"], a1, "dismissed") == ("not_found", None)
    assert ap.set_action_state(db, uuid.uuid4(), a1, "dismissed") == ("not_found", None)
    assert ap.set_action_state(db, pack["id"], uuid.uuid4(), "dismissed") == ("not_found", None)
    assert _action_state(db, a1) == "proposed"


# ---------------------------------------------------------------- set_plan

def test_set_plan_only_while_running(db):
    _, site, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    assert ap.set_plan(db, pack["id"], "too early", "T") is False
    ap.claim(db, pack["id"])
    assert ap.set_plan(db, pack["id"], "Close #69 on Tower A", "Fire doors") is True
    got = ap.get_for_reader(db, _reader(db, creator), pack["id"])
    assert (got["state"], got["reading"], got["title"]) == (
        "running", "Close #69 on Tower A", "Fire doors")
    ap.cancel(db, pack["id"])
    assert ap.set_plan(db, pack["id"], "late", "late") is False
    row = db.execute("SELECT reading, title FROM action_packs WHERE id=%s",
                     (pack["id"],)).fetchone()
    assert row == ("Close #69 on Tower A", "Fire doors")
    assert ap.set_plan(db, uuid.uuid4(), "r", "t") is False


def test_archive_and_unarchive(db):
    _, site, creator = _world(db)
    pack, _ = _ready_pack(db, site, creator)
    assert ap.archive(db, pack["id"], True) is True
    assert db.execute("SELECT archived_at IS NOT NULL FROM action_packs WHERE id=%s",
                      (pack["id"],)).fetchone()[0] is True
    assert ap.archive(db, pack["id"], False) is True
    assert db.execute("SELECT archived_at FROM action_packs WHERE id=%s",
                      (pack["id"],)).fetchone()[0] is None
    assert ap.archive(db, uuid.uuid4(), True) is False


# ---------------------------------------------------------------- reader ACL

@pytest.fixture
def acl_world(db):
    co = _co(db, "acl")
    site = sites.create_site(db, co["id"], f"Site-acl-{uuid.uuid4().hex[:6]}")
    site2 = sites.create_site(db, co["id"], f"Site-acl2-{uuid.uuid4().hex[:6]}")
    other_co = _co(db, "acl-other")
    w = {
        "creator": _user(db, co),
        "admin": _user(db, co, "admin"),
        "gm": _user(db, co, "gm"),
        "admin_other": _user(db, other_co, "admin"),
        "pm": _user(db, co, "pm"),
        "sm": _user(db, co, "site_manager"),
        "pm_other_site": _user(db, co, "pm"),
        "worker": _user(db, co),
        "external": _user(db, other_co),
        "external_pm": _user(db, other_co, "pm"),
        "platform": _user(db, other_co, "platform_admin"),
    }
    memberships.ensure_membership(db, w["creator"]["id"], site["id"], "worker")
    memberships.ensure_membership(db, w["pm"]["id"], site["id"], "pm")
    memberships.ensure_membership(db, w["sm"]["id"], site["id"], "site_manager")
    memberships.ensure_membership(db, w["pm_other_site"]["id"], site2["id"], "pm")
    memberships.ensure_membership(db, w["worker"]["id"], site["id"], "worker")
    memberships.add_external_membership(db, w["external"]["id"], site["id"], "worker")
    memberships.add_external_membership(db, w["external_pm"]["id"], site["id"], "pm")
    packs = {
        "creator": ap.create_queued(db, site_id=site["id"], created_by=w["creator"]["id"],
                                    instruction="mine"),
        "external": ap.create_queued(db, site_id=site["id"], created_by=w["external"]["id"],
                                     instruction="ext"),
    }
    return db, co, site, site2, w, packs


def _sees(db, w, who, pack):
    return ap.get_for_reader(db, _reader(db, w[who]), pack["id"]) is not None


def _lists(db, w, who, **kw):
    return {p["id"] for p in ap.list_for_reader(db, _reader(db, w[who]), **kw)}


EXPECT = {
    # who:            (creator's pack, external member's pack)
    "creator":        (True, False),
    "admin":          (True, True),
    "gm":             (True, True),
    "admin_other":    (False, False),
    "pm":             (True, True),
    "sm":             (True, True),
    "pm_other_site":  (False, False),
    "worker":         (False, False),
    "external":       (False, True),
    "external_pm":    (False, False),   # external members read only what they created
    "platform":       (False, False),   # not a reader by role (spec §7/D4)
}


@pytest.mark.parametrize("who", sorted(EXPECT))
def test_reader_matrix(acl_world, who):
    db, _, _, _, w, packs = acl_world
    want_c, want_e = EXPECT[who]
    assert _sees(db, w, who, packs["creator"]) is want_c
    assert _sees(db, w, who, packs["external"]) is want_e
    listed = _lists(db, w, who)
    assert (packs["creator"]["id"] in listed) is want_c
    assert (packs["external"]["id"] in listed) is want_e


def test_external_creator_loses_read_after_membership_removed(acl_world):
    db, _, site, _, w, packs = acl_world
    assert _sees(db, w, "external", packs["external"]) is True
    memberships.archive_membership(db, w["external"]["id"], site["id"])
    assert _sees(db, w, "external", packs["external"]) is False
    assert packs["external"]["id"] not in _lists(db, w, "external")
    # the site's company still reads it
    assert _sees(db, w, "admin", packs["external"]) is True


def test_internal_creator_reads_own_pack_after_leaving_the_site(acl_world):
    db, _, site, _, w, packs = acl_world
    memberships.archive_membership(db, w["creator"]["id"], site["id"])
    assert _sees(db, w, "creator", packs["creator"]) is True


def test_platform_admin_reads_its_own_pack_on_another_companys_site(acl_world):
    db, co, site, _, w, packs = acl_world
    mine = ap.create_queued(db, site_id=site["id"], created_by=w["platform"]["id"],
                            instruction="internal run")
    assert mine["company_id"] == co["id"]               # still the site's company (D4)
    assert _sees(db, w, "platform", mine) is True
    assert mine["id"] in _lists(db, w, "platform")
    assert _sees(db, w, "platform", packs["creator"]) is False   # nobody else's


def test_list_archived_accepts_truthy_and_falsy(db):
    co, site, creator = _world(db)
    pack, _ = _ready_pack(db, site, creator)
    ap.archive(db, pack["id"], True)
    r = _reader(db, creator)
    assert [p["id"] for p in ap.list_for_reader(db, r, archived=1)] == [pack["id"]]
    assert ap.list_for_reader(db, r, archived=0) == []


def test_site_moved_to_another_company_moves_the_readers(acl_world):
    db, co, site, _, w, packs = acl_world
    new_co = _co(db, "acl-new")
    w["new_admin"] = _user(db, new_co, "admin")
    w["new_pm"] = _user(db, new_co, "pm")
    memberships.ensure_membership(db, w["new_pm"]["id"], site["id"], "pm")
    db.execute("UPDATE sites SET company_id=%s WHERE id=%s", (new_co["id"], site["id"]))
    # the pack keeps its creation-time owner ...
    assert db.execute("SELECT company_id FROM action_packs WHERE id=%s",
                      (packs["creator"]["id"],)).fetchone()[0] == co["id"]
    # ... but reading follows the site's current company
    for who in ("admin", "gm", "pm", "sm"):
        assert _sees(db, w, who, packs["creator"]) is False, who
        assert packs["creator"]["id"] not in _lists(db, w, who), who
    for who in ("new_admin", "new_pm"):
        assert _sees(db, w, who, packs["creator"]) is True, who
        assert _sees(db, w, who, packs["external"]) is True, who
        assert packs["creator"]["id"] in _lists(db, w, who), who
    # the old-company creator still reads while a member of the site ...
    assert _sees(db, w, "creator", packs["creator"]) is True
    # ... and not once that membership is gone
    memberships.archive_membership(db, w["creator"]["id"], site["id"])
    assert _sees(db, w, "creator", packs["creator"]) is False
    assert packs["creator"]["id"] not in _lists(db, w, "creator")


def test_list_limit_is_clamped(db):
    co, site, creator = _world(db)
    admin = _user(db, co, "admin")
    pack, _ = _ready_pack(db, site, creator)
    r = _reader(db, admin)
    assert [p["id"] for p in ap.list_for_reader(db, r, limit=-5)] == [pack["id"]]
    assert [p["id"] for p in ap.list_for_reader(db, r, limit=0)] == [pack["id"]]
    assert [p["id"] for p in ap.list_for_reader(db, r, limit=10**9)] == [pack["id"]]
    assert ap.LIST_LIMIT_MAX == 200


def test_get_for_reader_returns_sources_and_actions(db):
    _, site, creator = _world(db)
    pack, _ = _ready_pack(db, site, creator)
    got = ap.get_for_reader(db, _reader(db, creator), pack["id"])
    assert got["id"] == pack["id"] and got["state"] == "ready"
    assert got["title"] == "Close the fire-door items"
    assert got["conclusion"][0]["refs"] == ["R1"]
    assert got["run_trace"] == {"rounds": 3, "tool_calls": 7}
    assert [s["ref"] for s in got["sources"]] == ["R1", "W1"]
    assert got["sources"][0]["excerpt"] == "the door is fixed"
    assert [a["seq"] for a in got["actions"]] == [1, 2]
    assert got["actions"][0]["refs"] == ["R1"] and got["actions"][0]["state"] == "proposed"
    assert ap.get_for_reader(db, _reader(db, creator), uuid.uuid4()) is None


def test_list_filters_and_counts(db):
    co, site, creator = _world(db)
    site2 = sites.create_site(db, co["id"], f"Site-ap2-{uuid.uuid4().hex[:6]}")
    admin = _user(db, co, "admin")
    ready, (a1, a2) = _ready_pack(db, site, creator)
    assert ap.set_action_state(db, ready["id"], a1, "applied",
                               applied_by=creator["id"])[0] == "ok"
    archived, _ = _ready_pack(db, site, creator)
    ap.archive(db, archived["id"], True)
    failed = _running(db, site, creator)
    ap.fail(db, failed["id"], "budget")
    other_site = ap.create_queued(db, site_id=site2["id"], created_by=creator["id"],
                                  instruction="x")
    r = _reader(db, admin)

    rows = {p["id"]: p for p in ap.list_for_reader(db, r)}
    assert set(rows) == {ready["id"], failed["id"], other_site["id"]}   # archived hidden
    assert rows[ready["id"]]["action_counts"] == {
        "proposed": 1, "applied": 1, "dismissed": 0, "stale": 0, "failed": 0}
    assert rows[failed["id"]]["action_counts"] == {
        "proposed": 0, "applied": 0, "dismissed": 0, "stale": 0, "failed": 0}
    assert rows[failed["id"]]["failure"] == "budget"
    assert "conclusion" not in rows[ready["id"]]          # the list is the light shape

    assert {p["id"] for p in ap.list_for_reader(db, r, archived=True)} == {archived["id"]}
    assert {p["id"] for p in ap.list_for_reader(db, r, archived=None)} == {
        ready["id"], archived["id"], failed["id"], other_site["id"]}
    assert {p["id"] for p in ap.list_for_reader(db, r, state="failed")} == {failed["id"]}
    assert {p["id"] for p in ap.list_for_reader(db, r, site_id=site2["id"])} == {
        other_site["id"]}
    # one transaction stamps every created_at the same; make the order observable
    db.execute("UPDATE action_packs SET created_at = now() + interval '1 minute' WHERE id=%s",
               (failed["id"],))
    newest_first = [p["id"] for p in ap.list_for_reader(db, r, archived=None)]
    assert newest_first[0] == failed["id"]
    assert len(ap.list_for_reader(db, r, limit=1)) == 1
    with pytest.raises(ValueError):
        ap.list_for_reader(db, r, state="nope")
