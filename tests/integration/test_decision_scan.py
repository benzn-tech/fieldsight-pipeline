"""Integration: the site decision loop scan Lambda (Task 4) against real Postgres.

Emitters (todo.overdue, checklist.gap) and the shadow `process` loop. Time is controlled
through the `now` / `today_nz` arguments; S3 is a fake client fed to the REAL
checklist_reports.read_result_status, so the moved reader runs here too.

decision_ledger rows are written ONLY through the rollback `db` fixture (the append-only
trigger blocks their cleanup). The overlap test commits its seed rows (a second connection
cannot see uncommitted ones) and deletes them in `finally`.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as dt
import io
import json
import uuid

import psycopg
import pytest

import checklist_reports
import nz_time
import decision_hub
import lambda_decision_scan as lds
from repositories import action_items, companies, memberships, sites, topics, users
from repositories import decision_cards as dc
from repositories import decision_ledger as dl
from repositories import site_events as se

pytestmark = pytest.mark.integration

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 10, 1, 0, tzinfo=UTC)          # 14:00 NZDT, Tue 10 Nov
TODAY = dt.date(2026, 11, 10)
DATE = "2026-11-10"


# ---------------------------------------------------------------- helpers

def _tag():
    return uuid.uuid4().hex[:8]


def _company(conn):
    return companies.create_company(conn, f"Co-ds-{_tag()}")


def _site(conn, co):
    return sites.create_site(conn, co["id"], f"Site-ds-{_tag()}")


def _user(conn, co, role="worker"):
    t = _tag()
    return users.upsert_user(conn, f"sub-ds-{t}", f"ds-{t}@example.com",
                             company_id=co["id"], global_role=role)


def _world(conn):
    """A company, its site, a pm member of the site and a company admin."""
    co = _company(conn)
    site = _site(conn, co)
    pm = _user(conn, co)
    memberships.add_membership(conn, pm["id"], site["id"], "pm")
    admin = _user(conn, co, "admin")
    return co, site, pm, admin


def _record_on(conn, co, site, folder, session, date=DATE):
    """A recording row of `session` on `site` -- what session_site_companies reads."""
    u = _user(conn, co)
    conn.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, "
        "started_at) VALUES (%s,%s,%s,'audio',%s,%s,now())",
        (co["id"], u["id"], site["id"], f"users/{folder}/audio/{date}/{session}_{uuid.uuid4().hex[:6]}.m4a",
         uuid.uuid4().hex))


def _report(conn, company_id, folder, session, result_key, date=DATE):
    return conn.execute(
        "INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
        "template_name, check_name, start_at, request_id, result_key) "
        "VALUES (%s,%s,%s,%s,'Pre-pour','pre-pour',%s,%s,%s) RETURNING id",
        (company_id, folder, date, session, f"10:{_tag()[:2]}", _tag(), result_key)
    ).fetchone()[0]


def _gaps(nc=(), failed=()):
    return {"not_covered": [{"n": n, "text": t} for n, t in nc],
            "failed": [{"n": n, "text": t, "comment": "north side", "responsible": "Mike",
                        "due": ""} for n, t in failed]}


def _done(**checklists):
    """A done result: {title: gaps}."""
    return {"status": "done",
            "checklists": {title: {"items": 9, "gaps": g} for title, g in checklists.items()}}


class FakeS3:
    """key -> body dict | 'missing' | '403' | raw str (malformed)."""

    def __init__(self, bodies=None):
        self.bodies = dict(bodies or {})
        self.calls = []

    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        self.calls.append(Key)
        body = self.bodies.get(Key, "missing")
        if body == "missing":
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        if body == "403":
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
        raw = body if isinstance(body, str) else json.dumps(body)
        return {"Body": io.BytesIO(raw.encode("utf-8"))}


def _reader(fake):
    return lambda key: checklist_reports.read_result_status(key, fake, "lake")


def _events(conn, company_id, type_=None):
    sql = "SELECT * FROM site_events WHERE company_id = %s"
    args = [company_id]
    if type_:
        sql += " AND type = %s"
        args.append(type_)
    return conn.cursor(row_factory=psycopg.rows.dict_row).execute(
        sql + " ORDER BY seq", args).fetchall()


def _cards(conn, company_id, status=None):
    sql = "SELECT * FROM decision_cards WHERE company_id = %s"
    args = [company_id]
    if status:
        sql += " AND status = %s"
        args.append(status)
    return conn.cursor(row_factory=psycopg.rows.dict_row).execute(
        sql + " ORDER BY created_at, card_id", args).fetchall()


def _runs(conn, company_id):
    return conn.cursor(row_factory=psycopg.rows.dict_row).execute(
        "SELECT * FROM event_runs WHERE company_id = %s ORDER BY started_at", (company_id,)
    ).fetchall()


def _seen(conn, report_id):
    return conn.execute("SELECT count(*) FROM decision_scan_seen WHERE report_id = %s",
                        (report_id,)).fetchone()[0]


#: Default deadline: NEWLY overdue on NOW's NZ day and the next (rev 6: carded while
#: today - (deadline + 1) <= 2). STALE_DEADLINE is past that on NOW's day -> late + digest.
FRESH_DEADLINE = dt.date(2026, 11, 8)
STALE_DEADLINE = dt.date(2026, 11, 3)


def _overdue_item(conn, site, text="Order the rebar", deadline=FRESH_DEADLINE,
                  responsible="Mike"):
    t = topics.upsert_topic(conn, site["id"], DATE, f"T-{_tag()}",
                            source_s3_key=f"extractions/x/{DATE}/{_tag()}.json")
    return action_items.insert_item(conn, t["id"], site["id"], text,
                                    responsible=responsible, deadline=deadline)


def _queued(conn, co, site, **over):
    """A queued todo.overdue event inserted directly (process tests)."""
    subject = over.pop("subject_ref", str(uuid.uuid4()))
    ev = {"company_id": co["id"], "site_id": site["id"], "source": "action_items",
          "type": "todo.overdue", "subject_ref": subject, "bucket": DATE,
          "occurred_at": NOW, "received_at": NOW,
          "payload": {"text": "Order the rebar", "deadline": "2026-11-03",
                      "responsible": "Mike"},
          "dedupe_key": f"{co['id']}|{site['id']}|todo.overdue|{subject}|{_tag()}",
          "status": "queued"}
    ev.update(over)
    return se.insert(conn, ev)[0]


def _gap_world(conn, folder=None, session=None):
    co, site, pm, admin = _world(conn)
    folder = folder or f"F_{_tag()}"
    session = session or f"sess{_tag()}"
    _record_on(conn, co, site, folder, session)
    return co, site, folder, session


# ---------------------------------------------------------------- flag

def test_flag_off_opens_no_connection(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("get_connection called with the flag off")
    monkeypatch.setattr(lds, "get_connection", boom)
    for value in (None, "off", "on", "SHADOW"):
        if value is None:
            monkeypatch.delenv("SITE_DECISION_LOOP", raising=False)
        else:
            monkeypatch.setenv("SITE_DECISION_LOOP", value)
        assert lds.lambda_handler({"scan": "process"}, None) == {"skipped": "flag off"}


class _Borrowed:
    """get_connection() stand-in: hands out the rollback fixture's connection and never
    commits or closes it."""

    def __init__(self, conn):
        self.conn = conn

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def test_overdue_scan_emits_and_processes_in_one_call(db, monkeypatch):
    """There is no process schedule (Aurora must be able to pause): a scan call emits AND
    runs the hub in the same invocation and connection, and reports both counts."""
    co, site, pm, admin = _world(db)
    # lambda_handler reads the real clock, so the item is due yesterday in REAL NZ time: a
    # fixed date would turn stale (rev 6: no per-item card, plus a backlog digest) as the
    # calendar moves on.
    real_today = nz_time.to_nz(dt.datetime.now(UTC)).date()
    _overdue_item(db, site, deadline=real_today - dt.timedelta(days=1))
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    monkeypatch.setattr(lds, "get_connection", _Borrowed(db))
    out = lds.lambda_handler({"scan": "todo_overdue"}, None)
    assert set(out) == {"emitted", "duplicates", "skipped_ambiguous_site", "skipped_pending",
                        "processed", "cards", "faults", "errors"}
    assert (out["emitted"], out["processed"], out["cards"], out["errors"]) == (1, 1, 1, 0)
    assert len(_cards(db, co["id"], "open")) == 1
    assert [e["status"] for e in _events(db, co["id"], "todo.overdue")] == ["done"]
    # The manual process invoke still works, and finds nothing left to do.
    out = lds.lambda_handler({"scan": "process"}, None)
    assert (out["emitted"], out["processed"], out["cards"]) == (0, 0, 0)


def test_gap_scan_also_processes_in_the_same_call(db, monkeypatch):
    calls = []
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    monkeypatch.setenv("LAKE_BUCKET", "lake")
    monkeypatch.setattr(lds, "get_connection", _Borrowed(db))
    monkeypatch.setattr(lds, "emit_checklist_gaps",
                        lambda conn, read, now: calls.append(("emit", conn)) or
                        dict(lds._counts(), emitted=2, errors=1))
    monkeypatch.setattr(lds, "process",
                        lambda conn, now: calls.append(("process", conn)) or
                        dict(lds._counts(), processed=2, cards=1, errors=1))
    out = lds.lambda_handler({"scan": "checklist_gap"}, None)
    assert calls == [("emit", db), ("process", db)]
    assert (out["emitted"], out["processed"], out["cards"], out["errors"]) == (2, 2, 1, 2)


# ---------------------------------------------------------------- todo.overdue

def test_overdue_item_becomes_one_open_card_per_item(db):
    co, site, pm, admin = _world(db)
    a = _overdue_item(db, site, "Order the rebar")
    b = _overdue_item(db, site, "Book the pump", deadline=dt.date(2026, 11, 9))
    # due on day 2 itself: not overdue on either day (deadline < today, strictly)
    _overdue_item(db, site, "Due day 2", deadline=TODAY + dt.timedelta(days=1))
    _overdue_item(db, site, "No date", deadline=None)                  # no deadline
    closed = _overdue_item(db, site, "Closed one")
    db.execute("UPDATE action_items SET status = 'done' WHERE id = %s", (closed["id"],))

    day1 = lds.emit_todo_overdue(db, NOW)
    assert (day1["emitted"], day1["duplicates"]) == (2, 0)
    again = lds.emit_todo_overdue(db, NOW)                      # same day: dedupe
    assert (again["emitted"], again["duplicates"]) == (0, 2)
    p1 = lds.process(db, NOW)
    assert p1["cards"] == 2

    now2 = NOW + dt.timedelta(days=1)
    day2 = lds.emit_todo_overdue(db, now2)
    assert (day2["emitted"], day2["duplicates"]) == (2, 0)              # new bucket
    p2 = lds.process(db, now2)
    assert p2["cards"] == 0

    for item in (a, b):
        sid = str(item["stable_id"])
        evs = [e for e in _events(db, co["id"], "todo.overdue") if e["status"] != "duplicate"
               and e["subject_ref"] == sid]
        assert len(evs) == 2
        assert sorted(e["bucket"] for e in evs) == ["2026-11-10", "2026-11-11"]
        assert [e["status"] for e in evs] == ["done", "nothing"]
        open_cards = [c for c in _cards(db, co["id"], "open") if c["subject_ref"] == sid]
        assert len(open_cards) == 1
    ev = next(e for e in _events(db, co["id"]) if e["subject_ref"] == str(a["stable_id"]))
    assert ev["payload"] == {"text": "Order the rebar", "deadline": "2026-11-08",
                             "responsible": "Mike"}
    # occurred_at = 00:00 NZDT on 9 Nov (the day after the deadline) = 11:00Z on 8 Nov
    assert ev["occurred_at"] == dt.datetime(2026, 11, 8, 11, 0, tzinfo=UTC)
    assert ev["received_at"] == NOW
    assert ev["company_id"] == co["id"] and ev["site_id"] == site["id"]
    card = next(c for c in _cards(db, co["id"]) if c["subject_ref"] == str(a["stable_id"]))
    assert card["recipients"] == [{"user_id": str(pm["id"]), "role": "pm"}]
    assert card["rule_id"] == "todo.overdue" and card["status"] == "open"


def _live_events(conn, company_id, type_):
    return [e for e in _events(conn, company_id, type_) if e["status"] != "duplicate"]


def test_overdue_new_items_carded_stale_ones_late_and_digested(db):
    """Rev 6 (owner option B). NOW's NZ day is Tue 10 Nov: due 9 Nov (today-1) and 7 Nov
    (today-3) are newly overdue and carded; due 6 Nov (today-4) is stale: its own event is
    'late' (no card) and it is listed in the site's one backlog digest."""
    co, site, pm, admin = _world(db)
    d1 = _overdue_item(db, site, "Due yesterday", deadline=dt.date(2026, 11, 9))
    d3 = _overdue_item(db, site, "Due three days ago", deadline=dt.date(2026, 11, 7))
    d4 = _overdue_item(db, site, "Due four days ago", deadline=dt.date(2026, 11, 6),
                       responsible="Ana")
    out = lds.emit_todo_overdue(db, NOW)
    assert (out["emitted"], out["duplicates"], out["errors"]) == (4, 0, 0)  # 3 items + digest
    lds.process(db, NOW)
    by_ref = {e["subject_ref"]: e for e in _live_events(db, co["id"], "todo.overdue")}
    assert by_ref[str(d1["stable_id"])]["status"] == "done"
    assert by_ref[str(d3["stable_id"])]["status"] == "done"
    assert by_ref[str(d4["stable_id"])]["status"] == "late"
    # NZ midnight starting 7 Nov (NZDT, +13) = 11:00Z on 6 Nov
    assert by_ref[str(d4["stable_id"])]["occurred_at"] == dt.datetime(2026, 11, 6, 11, 0,
                                                                       tzinfo=UTC)
    [dg] = _live_events(db, co["id"], "todo.overdue_backlog")
    assert dg["status"] == "done" and dg["subject_ref"] == str(site["id"])
    assert dg["source"] == "action_items" and dg["bucket"] == DATE
    assert dg["occurred_at"] == NOW and dg["received_at"] == NOW
    assert dg["payload"] == {"count": 1, "site_name": site["name"], "items": [
        {"stable_id": str(d4["stable_id"]), "text": "Due four days ago",
         "deadline": "2026-11-06", "responsible": "Ana"}]}
    cards = _cards(db, co["id"], "open")
    assert sorted(c["rule_id"] for c in cards) == ["todo.overdue", "todo.overdue",
                                                   "todo.overdue_backlog"]
    assert str(d4["stable_id"]) not in {c["subject_ref"] for c in cards}
    [digest] = [c for c in cards if c["rule_id"] == "todo.overdue_backlog"]
    assert digest["kind"] == "fact" and digest["purpose"] == "overdue_backlog"
    assert digest["question"] == f"1 to-do on {site['name']} is more than 3 days overdue"
    assert digest["deadline_at"] is None
    assert digest["options"] == [{"id": "open_list", "label": "Open the list",
                                  "recommended": True}]
    assert digest["recipients"] == [{"user_id": str(pm["id"]), "role": "pm"}]


def test_overdue_digest_routed_to_site_manager_too(db):
    co, site, pm, admin = _world(db)
    sm = _user(db, co)
    memberships.add_membership(db, sm["id"], site["id"], "site_manager")
    worker = _user(db, co)
    memberships.add_membership(db, worker["id"], site["id"], "worker")
    _overdue_item(db, site, deadline=STALE_DEADLINE)
    lds.emit_todo_overdue(db, NOW)
    lds.process(db, NOW)
    [digest] = _cards(db, co["id"], "open")
    assert digest["rule_id"] == "todo.overdue_backlog"
    assert sorted((r["role"], r["user_id"]) for r in digest["recipients"]) == sorted(
        [("pm", str(pm["id"])), ("site_manager", str(sm["id"]))])


def test_overdue_digest_one_per_site_and_none_for_fresh_only_site(db):
    co, site, pm, admin = _world(db)
    site2 = _site(db, co)
    site3 = _site(db, co)
    for s in (site2, site3):
        memberships.add_membership(db, pm["id"], s["id"], "pm")
    _overdue_item(db, site, "S1 old", deadline=STALE_DEADLINE)
    _overdue_item(db, site, "S1 older", deadline=dt.date(2026, 10, 20))
    _overdue_item(db, site, "S1 new", deadline=dt.date(2026, 11, 9))
    _overdue_item(db, site2, "S2 old", deadline=dt.date(2026, 11, 1))
    _overdue_item(db, site3, "S3 new", deadline=FRESH_DEADLINE)        # fresh only
    out = lds.emit_todo_overdue(db, NOW)
    assert (out["emitted"], out["errors"]) == (5 + 2, 0)
    digests = {e["subject_ref"]: e for e in _live_events(db, co["id"], "todo.overdue_backlog")}
    assert set(digests) == {str(site["id"]), str(site2["id"])}
    s1 = digests[str(site["id"])]["payload"]
    assert s1["count"] == 2
    assert [i["text"] for i in s1["items"]] == ["S1 older", "S1 old"]      # deadline ASC
    assert digests[str(site2["id"])]["payload"]["count"] == 1
    assert digests[str(site2["id"])]["site_id"] == site2["id"]
    lds.process(db, NOW)
    open_digests = [c for c in _cards(db, co["id"], "open")
                    if c["rule_id"] == "todo.overdue_backlog"]
    assert sorted(str(c["site_id"]) for c in open_digests) == sorted(
        [str(site["id"]), str(site2["id"])])
    assert {c["question"] for c in open_digests} == {
        f"2 to-dos on {site['name']} are more than 3 days overdue",
        f"1 to-do on {site2['name']} is more than 3 days overdue"}


def test_overdue_digest_lists_50_items_and_counts_all(db):
    co, site, pm, admin = _world(db)
    first = dt.date(2026, 9, 1)
    for i in range(55):
        _overdue_item(db, site, f"Old {i:02d}", deadline=first + dt.timedelta(days=i))
    out = lds.emit_todo_overdue(db, NOW)
    assert (out["emitted"], out["errors"]) == (56, 0)
    [dg] = _live_events(db, co["id"], "todo.overdue_backlog")
    p = dg["payload"]
    assert p["count"] == 55 and len(p["items"]) == 50
    assert [i["text"] for i in p["items"]] == [f"Old {i:02d}" for i in range(50)]
    assert [i["deadline"] for i in p["items"]] == sorted(i["deadline"] for i in p["items"])


def test_overdue_digest_ties_on_deadline_break_by_stable_id(db):
    co, site, pm, admin = _world(db)
    items = [_overdue_item(db, site, f"Same {i}", deadline=STALE_DEADLINE) for i in range(4)]
    lds.emit_todo_overdue(db, NOW)
    [dg] = _live_events(db, co["id"], "todo.overdue_backlog")
    assert [i["stable_id"] for i in dg["payload"]["items"]] == sorted(
        str(i["stable_id"]) for i in items)


def test_overdue_second_scan_same_day_is_all_duplicates(db):
    co, site, pm, admin = _world(db)
    _overdue_item(db, site, "Fresh")
    _overdue_item(db, site, "Stale", deadline=STALE_DEADLINE)
    first = lds.emit_todo_overdue(db, NOW)
    assert (first["emitted"], first["duplicates"]) == (3, 0)
    lds.process(db, NOW)
    again = lds.emit_todo_overdue(db, NOW + dt.timedelta(hours=3))
    assert (again["emitted"], again["duplicates"]) == (0, 3)
    assert lds.process(db, NOW + dt.timedelta(hours=3))["cards"] == 0
    # next NZ day: new bucket, the open digest card absorbs it ('nothing'), no second card
    now2 = NOW + dt.timedelta(days=1)
    day2 = lds.emit_todo_overdue(db, now2)
    assert (day2["emitted"], day2["duplicates"]) == (3, 0)
    assert lds.process(db, now2)["cards"] == 0
    assert [e["status"] for e in _live_events(db, co["id"], "todo.overdue_backlog")] == [
        "done", "nothing"]
    assert len([c for c in _cards(db, co["id"])
                if c["rule_id"] == "todo.overdue_backlog"]) == 1


def test_overdue_bad_site_digest_counted_in_errors(db, monkeypatch):
    co, site, pm, admin = _world(db)
    site2 = _site(db, co)
    _overdue_item(db, site, "Stale A", deadline=STALE_DEADLINE)
    _overdue_item(db, site2, "Stale B", deadline=STALE_DEADLINE)
    real = lds._emit_overdue_backlog

    def flaky(conn, company_id, site_id, items, today_nz, now, out):
        if site_id == site["id"]:
            raise RuntimeError("boom")
        return real(conn, company_id, site_id, items, today_nz, now, out)
    monkeypatch.setattr(lds, "_emit_overdue_backlog", flaky)
    out = lds.emit_todo_overdue(db, NOW)
    assert (out["emitted"], out["errors"]) == (3, 1)
    assert [e["subject_ref"] for e in _live_events(db, co["id"], "todo.overdue_backlog")] == [
        str(site2["id"])]


def test_overdue_skips_superseded_topic_and_archived_site(db):
    co, site, pm, admin = _world(db)
    item = _overdue_item(db, site)
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (item["topic_id"],))
    site2 = _site(db, co)
    _overdue_item(db, site2)
    db.execute("UPDATE sites SET archived_at = now() WHERE id = %s", (site2["id"],))
    rows = [r for r in action_items.overdue_open(db, TODAY) if r["company_id"] == co["id"]]
    assert rows == []


# ---------------------------------------------------------------- checklist.gap

def test_gap_rerun_same_gaps_makes_no_second_card(db):
    co, site, folder, session = _gap_world(db)
    body = _done(**{"Pre-pour": _gaps(nc=[(3, "Cover to steel checked")],
                                      failed=[(2, "Edge protection installed")])})
    fake = FakeS3({"k1": body, "k2": body})
    rid = _report(db, co["id"], folder, session, "k1")
    first = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert (first["emitted"], first["duplicates"]) == (2, 0)
    assert _seen(db, rid) == 1
    lds.process(db, NOW)
    assert len(_cards(db, co["id"], "open")) == 2

    # the re-run replaces result_key (and re-stamps created_at) with the same gaps
    db.execute("UPDATE checklist_reports SET result_key = 'k2', created_at = now() "
               "WHERE id = %s", (rid,))
    second = lds.emit_checklist_gaps(db, _reader(fake), NOW + dt.timedelta(hours=1))
    assert (second["emitted"], second["duplicates"]) == (0, 2)
    p = lds.process(db, NOW + dt.timedelta(hours=1))
    assert p["cards"] == 0
    assert len(_cards(db, co["id"], "open")) == 2
    assert len(_cards(db, co["id"])) == 2


def test_gap_event_shape(db):
    co, site, folder, session = _gap_world(db)
    fake = FakeS3({"k1": _done(**{"Pre-pour": _gaps(failed=[(2, "Edge protection")])})})
    _report(db, co["id"], folder, session, "k1")
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    [ev] = _events(db, co["id"], "checklist.gap")
    assert ev["subject_ref"] == f"{session}|Pre-pour|2|Edge protection"
    assert ev["bucket"] == "" and ev["status"] == "queued" and ev["received_at"] == NOW
    # report_date 00:00 NZDT = 11:00Z the day before
    assert ev["occurred_at"] == dt.datetime(2026, 11, 9, 11, 0, tzinfo=UTC)
    assert ev["payload"]["checklist"] == "Pre-pour"
    assert ev["payload"]["item"] == "Edge protection"
    assert ev["payload"]["kind"] == "failed"
    assert ev["payload"]["comment"] == "north side"
    assert ev["payload"]["session"] == session
    assert ev["payload"]["report_date"] == DATE


def test_gap_disappearing_supersedes_its_card(db):
    co, site, folder, session = _gap_world(db)
    before = _done(**{"Pre-pour": _gaps(nc=[(3, "Cover checked"), (5, "Bar chairs")]),
                      "Housekeeping": _gaps(nc=[(1, "Bins emptied")])})
    after = _done(**{"Pre-pour": _gaps(nc=[(5, "Bar chairs")]),
                     "Housekeeping": _gaps()})                      # all covered now
    fake = FakeS3({"k1": before, "k2": after})
    rid = _report(db, co["id"], folder, session, "k1")
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    lds.process(db, NOW)
    assert len(_cards(db, co["id"], "open")) == 3

    db.execute("UPDATE checklist_reports SET result_key = 'k2', created_at = now() "
               "WHERE id = %s", (rid,))
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    open_refs = sorted(c["subject_ref"] for c in _cards(db, co["id"], "open"))
    assert open_refs == [f"{session}|Pre-pour|5|Bar chairs"]
    gone = sorted(c["subject_ref"] for c in _cards(db, co["id"], "superseded"))
    assert gone == [f"{session}|Housekeeping|1|Bins emptied",
                    f"{session}|Pre-pour|3|Cover checked"]


def test_gap_supersede_leaves_other_sessions_and_checklists_alone(db):
    co, site, folder, session = _gap_world(db)
    other_session = f"sess{_tag()}"
    _record_on(db, co, site, folder, other_session)
    fake = FakeS3({"a1": _done(**{"Pre-pour": _gaps(nc=[(1, "One")])}),
                   "b1": _done(**{"Pre-pour": _gaps(nc=[(1, "One")])}),
                   "a2": _done(**{"Other": _gaps()})})               # Pre-pour absent
    ra = _report(db, co["id"], folder, session, "a1")
    _report(db, co["id"], folder, other_session, "b1")
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    lds.process(db, NOW)
    assert len(_cards(db, co["id"], "open")) == 2
    db.execute("UPDATE checklist_reports SET result_key = 'a2', created_at = now() "
               "WHERE id = %s", (ra,))
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    # a checklist missing from the result says nothing about its gaps: nothing superseded
    assert len(_cards(db, co["id"], "open")) == 2


def test_ambiguous_site_is_skipped_and_counted(db):
    co, site, pm, admin = _world(db)
    site2 = _site(db, co)
    folder = f"F_{_tag()}"
    two, none = f"sess{_tag()}", f"sess{_tag()}"
    _record_on(db, co, site, folder, two)
    _record_on(db, co, site2, folder, two)
    body = _done(**{"Pre-pour": _gaps(nc=[(1, "One")])})
    fake = FakeS3({"k-two": body, "k-none": body})
    r2 = _report(db, co["id"], folder, two, "k-two")
    r0 = _report(db, co["id"], folder, none, "k-none")
    out = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert out["skipped_ambiguous_site"] == 2
    assert out["emitted"] == 0
    assert _events(db, co["id"]) == []
    assert _seen(db, r2) == 0 and _seen(db, r0) == 0


def test_pending_report_is_picked_up_later(db):
    co, site, folder, session = _gap_world(db)
    fake = FakeS3({})                                               # result not written yet
    rid = _report(db, co["id"], folder, session, "k1")
    out = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert (out["skipped_pending"], out["emitted"], out["errors"]) == (1, 0, 0)
    assert _seen(db, rid) == 0
    fake.bodies["k1"] = {"status": "pending"}
    out = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert (out["skipped_pending"], out["emitted"]) == (1, 0)
    assert _seen(db, rid) == 0
    fake.bodies["k1"] = _done(**{"Pre-pour": _gaps(nc=[(1, "One")])})
    out = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert (out["skipped_pending"], out["emitted"]) == (0, 1)
    assert _seen(db, rid) == 1
    calls = len(fake.calls)
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert len(fake.calls) == calls                                 # seen: not read again


def test_s3_403_counts_error_and_continues(db):
    co, site, folder, session = _gap_world(db)
    s2, s3_ = f"sess{_tag()}", f"sess{_tag()}"
    _record_on(db, co, site, folder, s2)
    _record_on(db, co, site, folder, s3_)
    fake = FakeS3({"k-denied": "403", "k-bad": "{not json",
                   "k-ok": _done(**{"Pre-pour": _gaps(nc=[(1, "One")])})})
    r_denied = _report(db, co["id"], folder, session, "k-denied")
    r_bad = _report(db, co["id"], folder, s2, "k-bad")
    r_ok = _report(db, co["id"], folder, s3_, "k-ok")
    out = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert out["errors"] == 2
    assert out["emitted"] == 1
    assert (_seen(db, r_denied), _seen(db, r_bad), _seen(db, r_ok)) == (0, 0, 1)
    # fixed on the next scan
    fake.bodies["k-denied"] = _done(**{"Pre-pour": _gaps(nc=[(2, "Two")])})
    out = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert (out["errors"], out["emitted"]) == (1, 1)
    assert _seen(db, r_denied) == 1


def test_walk_gap_keys_survive_a_vanished_gap(db):
    co, site, folder, session = _gap_world(db)
    before = _done(**{"Unit 1": _gaps(nc=[(2, "Wall linings"), (4, "Skirting"),
                                          (6, "Doors hung")]),
                      "Unit 2": _gaps(nc=[(4, "Skirting")])})
    after = _done(**{"Unit 1": _gaps(nc=[(2, "Wall linings"), (6, "Doors hung")]),
                     "Unit 2": _gaps(nc=[(4, "Skirting")])})
    fake = FakeS3({"k1": before, "k2": after})
    rid = _report(db, co["id"], folder, session, "k1")
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    lds.process(db, NOW)
    first = {c["subject_ref"]: c["card_id"] for c in _cards(db, co["id"], "open")}
    assert len(first) == 4
    db.execute("UPDATE checklist_reports SET result_key = 'k2', created_at = now() "
               "WHERE id = %s", (rid,))
    out = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert (out["emitted"], out["duplicates"]) == (0, 3)            # same keys as before
    lds.process(db, NOW)
    now_open = {c["subject_ref"]: c["card_id"] for c in _cards(db, co["id"], "open")}
    assert now_open == {k: v for k, v in first.items()
                        if k != f"{session}|Unit 1|4|Skirting"}


def test_event_company_is_the_sites_not_the_recorders(db):
    home = _company(db)                                             # recorder's company A
    co, site, pm, admin = _world(db)                                # site's company B
    folder, session = f"F_{_tag()}", f"sess{_tag()}"
    _record_on(db, co, site, folder, session)
    fake = FakeS3({"k1": _done(**{"Pre-pour": _gaps(nc=[(1, "One")])})})
    _report(db, home["id"], folder, session, "k1")                  # row under company A
    out = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert out["emitted"] == 1
    assert _events(db, home["id"]) == []
    [ev] = _events(db, co["id"])
    assert ev["site_id"] == site["id"]
    lds.process(db, NOW)
    [card] = _cards(db, co["id"])
    assert card["recipients"] == [{"user_id": str(pm["id"]), "role": "pm"}]
    assert _cards(db, home["id"]) == []


def test_late_gap_is_not_carded(db):
    co, site, pm, admin = _world(db)
    folder, session = f"F_{_tag()}", f"sess{_tag()}"
    old = "2026-11-02"                                              # 8 days before
    _record_on(db, co, site, folder, session, date=old)
    fake = FakeS3({"k1": _done(**{"Pre-pour": _gaps(nc=[(1, "One")])})})
    _report(db, co["id"], folder, session, "k1", date=old)
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    lds.process(db, NOW)
    [ev] = _events(db, co["id"])
    assert ev["status"] == "late"
    assert _cards(db, co["id"]) == [] and _runs(db, co["id"]) == []


# ---------------------------------------------------------------- process

@pytest.mark.parametrize("status", ["suspended", "exited"])
def test_inactive_company_blocks(db, monkeypatch, status):
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    co, site, pm, admin = _world(db)
    _overdue_item(db, site)
    db.execute("UPDATE companies SET status = %s WHERE id = %s", (status, co["id"]))
    assert lds.emit_todo_overdue(db, NOW)["emitted"] == 1
    out = lds.process(db, NOW)
    assert out["processed"] == 1 and out["cards"] == 0
    [ev] = _events(db, co["id"])
    assert ev["status"] == "blocked"
    assert _cards(db, co["id"]) == [] and _runs(db, co["id"]) == []


def test_unmatched_event_type(db):
    co, site, pm, admin = _world(db)
    _queued(db, co, site, type="something.else")
    lds.process(db, NOW)
    [ev] = _events(db, co["id"])
    assert ev["status"] == "unmatched"
    assert _cards(db, co["id"]) == [] and _runs(db, co["id"]) == []


def test_decided_subject_gets_no_card(db):
    co, site, pm, admin = _world(db)
    item = _overdue_item(db, site)
    dl.append(db, {"company_id": co["id"], "site_id": site["id"], "card_id": None,
                   "subject_ref": str(item["stable_id"]), "decision": "done",
                   "kind": "decided", "decided_by": None, "authority_basis": "pm",
                   "evidence": [], "supersedes_entry_id": None, "source": "card"})
    lds.emit_todo_overdue(db, NOW)
    out = lds.process(db, NOW)
    assert out["cards"] == 0
    [ev] = _events(db, co["id"])
    assert ev["status"] == "done"
    [run] = _runs(db, co["id"])
    assert run["outcome"] == "nothing"
    assert _cards(db, co["id"]) == []


def test_same_subject_queued_events_merge(db):
    co, site, pm, admin = _world(db)
    subject = str(uuid.uuid4())
    later = _queued(db, co, site, subject_ref=subject,
                    not_before=NOW + dt.timedelta(hours=1))          # not due yet
    due = _queued(db, co, site, subject_ref=subject)
    out = lds.process(db, NOW)
    assert out["cards"] == 1
    st = {e["event_id"]: e["status"] for e in _events(db, co["id"])}
    assert st == {due["event_id"]: "done", later["event_id"]: "merged"}
    [run] = _runs(db, co["id"])
    assert run["outcome"] == "done"
    linked = {r[0] for r in db.execute("SELECT event_id FROM run_events WHERE run_id = %s",
                                       (run["run_id"],)).fetchall()}
    assert linked == {due["event_id"], later["event_id"]}


def test_failures_back_off_fault_at_3_stop_at_5(db, monkeypatch):
    co, site, pm, admin = _world(db)
    ops = _user(db, co, "platform_admin")
    ev = _queued(db, co, site)

    def broken(*a, **k):
        raise RuntimeError("handler exploded")
    monkeypatch.setattr(lds, "_handle", broken)

    t = NOW
    not_befores = []
    for attempt in range(1, 6):
        if attempt > 1:
            # not due yet a second before its not_before
            assert lds.process(db, t - dt.timedelta(seconds=1))["processed"] == 0
        out = lds.process(db, t)
        assert out["processed"] == 1
        [row] = _events(db, co["id"])
        assert row["attempts"] == attempt
        assert "handler exploded" in row["last_error"]
        faults = [c for c in _cards(db, co["id"]) if c["rule_id"] == "hub.fault"]
        if attempt < 5:
            assert row["status"] == "queued"
            expected = t + dt.timedelta(seconds=decision_hub.backoff_seconds(attempt))
            assert row["not_before"] == expected
            not_befores.append(row["not_before"] - t)
            t = row["not_before"]
        else:
            assert row["status"] == "failed"
        assert out["faults"] == (1 if attempt == 3 else 0)
        assert len(faults) == (0 if attempt < 3 else 1)
    assert not_befores == sorted(not_befores) and not_befores[0] < not_befores[-1]

    [fault] = [c for c in _cards(db, co["id"]) if c["rule_id"] == "hub.fault"]
    assert fault["version"] == 2
    assert "stopped" in fault["consequence"].lower()
    assert fault["purpose"] == "fault" and fault["subject_ref"] == str(ev["event_id"])
    assert fault["event_id"] == ev["event_id"] and fault["kind"] == "fault"
    assert fault["card_key"] == decision_hub.fault_card_key(
        co["id"], site["id"], ev["event_id"], "fault")
    roles = {(r["user_id"], r["role"]) for r in fault["recipients"]}
    assert (str(admin["id"]), "company_admin") in roles
    assert (str(ops["id"]), "ops") in roles
    assert (str(pm["id"]), "pm") not in roles
    # terminal: never claimed again
    assert lds.process(db, t + dt.timedelta(days=30))["processed"] == 0
    runs = _runs(db, co["id"])
    assert len(runs) == 5 and all(r["outcome"] == "failed" for r in runs)
    assert sorted(r["attempt"] for r in runs) == [1, 2, 3, 4, 5]
    assert _cards(db, co["id"], "open") == [fault]


def test_a_failure_rolls_back_only_its_own_event(db, monkeypatch):
    co, site, pm, admin = _world(db)
    bad = _queued(db, co, site)
    good = _queued(db, co, site)
    real = lds._handle

    def flaky(conn, event, rule, now):
        if str(event.event_id) == str(bad["event_id"]):
            real(conn, event, rule, now)          # writes its card, then fails
            raise RuntimeError("late failure")
        return real(conn, event, rule, now)
    monkeypatch.setattr(lds, "_handle", flaky)
    out = lds.process(db, NOW)
    assert out["processed"] == 2 and out["cards"] == 1
    st = {e["event_id"]: e["status"] for e in _events(db, co["id"])}
    assert st == {bad["event_id"]: "queued", good["event_id"]: "done"}
    [card] = _cards(db, co["id"])
    assert card["subject_ref"] == good["subject_ref"]          # bad's card rolled back


def test_too_deep_event_not_dispatched_and_faults_once(db):
    co, site, pm, admin = _world(db)
    chain = uuid.uuid4()
    _queued(db, co, site, chain_id=chain, chain_depth=4)
    _queued(db, co, site, chain_id=chain, chain_depth=5)
    _queued(db, co, site, chain_id=uuid.uuid4(), chain_depth=3)       # at the limit: fine
    out = lds.process(db, NOW)
    assert out["faults"] == 1
    statuses = sorted(e["status"] for e in _events(db, co["id"]))
    assert statuses == ["done", "too_deep", "too_deep"]
    faults = [c for c in _cards(db, co["id"]) if c["rule_id"] == "hub.fault"]
    assert len(faults) == 1
    assert faults[0]["purpose"] == "too_deep" and faults[0]["subject_ref"] == str(chain)
    assert faults[0]["card_key"] == decision_hub.fault_card_key(
        co["id"], site["id"], chain, "too_deep")
    assert len(_runs(db, co["id"])) == 1                             # only the depth-3 one


def test_mark_seen_is_idempotent(db):
    co = _company(db)
    rid = _report(db, co["id"], "F", "s", "k")
    checklist_reports.mark_seen(db, rid, "k")
    checklist_reports.mark_seen(db, rid, "k")
    assert _seen(db, rid) == 1
    assert all(r["id"] != rid for r in checklist_reports.unseen_reports(db))


def test_unseen_reports_window(db):
    co = _company(db)
    fresh = _report(db, co["id"], "F", "s1", "k1")
    old = _report(db, co["id"], "F", "s2", "k2")
    db.execute("UPDATE checklist_reports SET created_at = now() - interval '49 hours' "
               "WHERE id = %s", (old,))
    ids = {r["id"] for r in checklist_reports.unseen_reports(db)}
    assert fresh in ids and old not in ids
    checklist_reports.mark_seen(db, fresh, "k1")
    db.execute("UPDATE checklist_reports SET result_key = 'k1b' WHERE id = %s", (fresh,))
    assert fresh in {r["id"] for r in checklist_reports.unseen_reports(db)}  # new key


# ---------------------------------------------------------------- overlap (committed)

@pytest.fixture
def committed(migrated_db_url):
    conn = psycopg.connect(migrated_db_url, autocommit=True)
    co = _company(conn)
    site = _site(conn, co)
    try:
        yield migrated_db_url, conn, co, site
    finally:
        conn.execute("DELETE FROM decision_cards WHERE company_id=%s", (co["id"],))
        conn.execute("DELETE FROM event_runs WHERE company_id=%s", (co["id"],))
        conn.execute("DELETE FROM site_events WHERE company_id=%s", (co["id"],))
        conn.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
        conn.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
        conn.close()


def test_overlapping_process_runs_do_not_double_process(committed, monkeypatch):
    url, conn, co, site = committed
    mine = {_queued(conn, co, site)["event_id"] for _ in range(3)}
    a = psycopg.connect(url)
    b = psycopg.connect(url)
    b.execute("SET statement_timeout = '5s'")
    b.commit()
    real = lds._handle
    inner = {}

    def handle_then_overlap(c, event, rule, now):
        if not inner:
            inner["out"] = lds.process(b, now)    # a second scan while a holds its claim
        return real(c, event, rule, now)
    monkeypatch.setattr(lds, "_handle", handle_then_overlap)
    try:
        out_a = lds.process(a, NOW)
        assert inner["out"]["processed"] == 0
        assert out_a["processed"] >= 3
        evs = [e for e in _events(conn, co["id"])]
        assert {e["event_id"] for e in evs} == mine
        assert all(e["status"] == "done" for e in evs)
        assert len(_runs(conn, co["id"])) == 3
        assert len(_cards(conn, co["id"])) == 3
        # a third scan afterwards finds nothing left
        monkeypatch.setattr(lds, "_handle", real)
        lds.process(b, NOW)
        assert all(e["status"] == "done" for e in _events(conn, co["id"]))
        assert len(_runs(conn, co["id"])) == 3
        assert len(_cards(conn, co["id"])) == 3
    finally:
        a.rollback()
        b.rollback()
        a.close()
        b.close()


# ---------------------------------------------------------------- fix round 1

def test_unknown_scan_raises(monkeypatch):
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    monkeypatch.setattr(lds, "get_connection", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("connection opened for an unknown scan")))
    for bad in ({"scan": "nope"}, {}, None):
        with pytest.raises(ValueError):
            lds.lambda_handler(bad, None)


def test_gap_scan_without_lake_bucket_raises_before_scanning(monkeypatch):
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    monkeypatch.setattr(lds, "get_connection", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("connection opened without LAKE_BUCKET")))
    for value in (None, ""):
        if value is None:
            monkeypatch.delenv("LAKE_BUCKET", raising=False)
        else:
            monkeypatch.setenv("LAKE_BUCKET", value)
        with pytest.raises(RuntimeError, match="LAKE_BUCKET"):
            lds.lambda_handler({"scan": "checklist_gap"}, None)


def test_fault_card_failure_does_not_stall_the_batch(db, monkeypatch):
    co, site, pm, admin = _world(db)
    bad = _queued(db, co, site)
    good_subjects = []
    real = lds._handle

    def handle(conn, event, rule, now):
        if str(event.event_id) == str(bad["event_id"]):
            raise RuntimeError("handler exploded")
        return real(conn, event, rule, now)

    def broken_route(*a, **k):
        raise RuntimeError("recipient lookup exploded")
    monkeypatch.setattr(lds, "_handle", handle)
    monkeypatch.setattr(lds, "_insert_fault", broken_route)

    t = NOW
    last_nb = None
    for attempt in range(1, 4):
        good = _queued(db, co, site, received_at=t, occurred_at=t)   # healthy, same batch
        good_subjects.append(good["subject_ref"])
        out = lds.process(db, t)
        assert out["processed"] == 2
        row = next(e for e in _events(db, co["id"]) if e["event_id"] == bad["event_id"])
        assert row["attempts"] == attempt and row["status"] == "queued"
        assert last_nb is None or row["not_before"] > last_nb
        last_nb = row["not_before"]
        t = row["not_before"]
        st = next(e for e in _events(db, co["id"]) if e["event_id"] == good["event_id"])
        assert st["status"] == "done"
    open_refs = {c["subject_ref"] for c in _cards(db, co["id"], "open")}
    assert open_refs == set(good_subjects)                    # healthy cards; no fault card
    failed_runs = [r for r in _runs(db, co["id"]) if r["outcome"] == "failed"]
    assert len(failed_runs) == 3


def test_one_bad_overdue_item_keeps_the_rest(db, monkeypatch):
    co, site, pm, admin = _world(db)
    a = _overdue_item(db, site, "Good one")
    b = _overdue_item(db, site, "Bad one")
    real = lds._insert_event

    def insert(conn, ev, today_nz):
        if ev.subject_ref == str(b["stable_id"]):
            conn.execute("SELECT 1/0")                        # aborts like a real SQL error
        return real(conn, ev, today_nz)
    monkeypatch.setattr(lds, "_insert_event", insert)
    out = lds.emit_todo_overdue(db, NOW)
    assert (out["emitted"], out["errors"]) == (1, 1)
    assert [e["subject_ref"] for e in _events(db, co["id"])] == [str(a["stable_id"])]


@pytest.mark.parametrize("order", ["first_then_second", "second_then_first"])
def test_two_reports_of_one_template_do_not_supersede_each_other(db, order):
    co, site, folder, session = _gap_world(db)
    fake = FakeS3({"k-first": _done(**{"Pre-pour": _gaps(nc=[(3, "Cover checked")])}),
                   "k-second": _done(**{"Pre-pour": _gaps()})})          # all covered
    first = _report(db, co["id"], folder, session, "k-first")
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    lds.process(db, NOW)
    second = _report(db, co["id"], folder, session, "k-second")          # other start_at
    if order == "second_then_first":
        # re-queue the first too, so the scan reads second, then first
        db.execute("UPDATE checklist_reports SET result_key = 'k-first2', "
                   "created_at = now() + interval '1 second' WHERE id = %s", (first,))
        fake.bodies["k-first2"] = fake.bodies["k-first"]
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert _seen(db, second) == 1
    [card] = _cards(db, co["id"], "open")
    assert card["subject_ref"] == f"{session}|Pre-pour|3|Cover checked"
    assert _cards(db, co["id"], "superseded") == []
    [ev] = [e for e in _events(db, co["id"]) if e["status"] != "duplicate"]
    assert ev["payload"]["report_id"] == str(first)


def test_report_scoped_supersede_still_closes_its_own_card(db):
    co, site, folder, session = _gap_world(db)
    fake = FakeS3({"k1": _done(**{"Pre-pour": _gaps(nc=[(3, "Cover checked")])}),
                   "k2": _done(**{"Pre-pour": _gaps()})})
    rid = _report(db, co["id"], folder, session, "k1")
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    lds.process(db, NOW)
    db.execute("UPDATE checklist_reports SET result_key = 'k2', created_at = now() "
               "WHERE id = %s", (rid,))
    lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert _cards(db, co["id"], "open") == []
    assert len(_cards(db, co["id"], "superseded")) == 1


def test_malformed_claimed_row_is_a_failed_attempt(db):
    co, site, pm, admin = _world(db)
    bad = _queued(db, co, site, payload=["not", "an", "object"])
    good = _queued(db, co, site)
    out = lds.process(db, NOW)
    assert out["processed"] == 2 and out["cards"] == 1
    st = {e["event_id"]: e for e in _events(db, co["id"])}
    assert st[bad["event_id"]]["status"] == "queued"
    assert st[bad["event_id"]]["attempts"] == 1
    assert "payload" in st[bad["event_id"]]["last_error"]
    assert st[good["event_id"]]["status"] == "done"


# ---------------------------------------------------------------- final fix wave

def test_overdue_uses_the_newest_live_row_per_stable_id(db):
    co, site, pm, admin = _world(db)
    old = _overdue_item(db, site, "Order the rebar")
    newer = _overdue_item(db, site, "Order the rebar")
    # newer live row of the same item (same stable_id), marked done
    db.execute("UPDATE action_items SET stable_id = %s, status = 'done', "
               "created_at = now() + interval '1 minute' WHERE id = %s",
               (old["stable_id"], newer["id"]))
    rows = [r for r in action_items.overdue_open(db, TODAY) if r["company_id"] == co["id"]]
    assert rows == []
    assert lds.emit_todo_overdue(db, NOW)["emitted"] == 0
    # and the other way round: the newer row is open -> one row, the newer one
    db.execute("UPDATE action_items SET status = 'open', text = 'Order the rebar (v2)' "
               "WHERE id = %s", (newer["id"],))
    db.execute("UPDATE action_items SET status = 'done' WHERE id = %s", (old["id"],))
    rows = [r for r in action_items.overdue_open(db, TODAY) if r["company_id"] == co["id"]]
    assert [(r["stable_id"], r["text"]) for r in rows] == [
        (old["stable_id"], "Order the rebar (v2)")]


def test_overdue_bucket_is_the_nz_day_of_now(db):
    co, site, pm, admin = _world(db)
    item = _overdue_item(db, site, deadline=dt.date(2026, 11, 5))
    late_utc = dt.datetime(2026, 11, 6, 11, 30, tzinfo=UTC)         # 00:30 NZDT 7 Nov
    assert lds.emit_todo_overdue(db, late_utc)["emitted"] == 1
    [ev] = _events(db, co["id"])
    assert ev["bucket"] == "2026-11-07" and ev["received_at"] == late_utc
    assert ev["subject_ref"] == str(item["stable_id"])
# ---------------------------------------------------------------- counts follow the savepoint
# A raise after the insert in the same savepoint rolls the row back; the count must go too.

def test_overdue_item_raise_after_insert_is_not_counted(db, monkeypatch):
    co, site, pm, admin = _world(db)
    _overdue_item(db, site)

    def boom(*a, **k):
        raise RuntimeError("after the insert")
    monkeypatch.setattr(lds, "is_late", boom)
    out = lds.emit_todo_overdue(db, NOW)
    assert out["emitted"] == 0 and out["duplicates"] == 0 and out["errors"] >= 1
    assert _events(db, co["id"], "todo.overdue") == []


def test_overdue_backlog_raise_after_insert_is_not_counted(db, monkeypatch):
    co, site, pm, admin = _world(db)
    _overdue_item(db, site, "Stale A", deadline=STALE_DEADLINE)
    real = lds._emit_overdue_backlog

    def insert_then_raise(conn, company_id, site_id, items, today_nz, now, out):
        real(conn, company_id, site_id, items, today_nz, now, out)
        if site_id == site["id"]:
            raise RuntimeError("after the insert")
    monkeypatch.setattr(lds, "_emit_overdue_backlog", insert_then_raise)
    out = lds.emit_todo_overdue(db, NOW)
    # the item event is kept and counted; the digest is rolled back and not counted
    assert len(_live_events(db, co["id"], "todo.overdue")) == 1
    assert _live_events(db, co["id"], "todo.overdue_backlog") == []
    mine = [e for e in _events(db, co["id"]) if e["status"] != "duplicate"]
    assert out["emitted"] == len(mine) == 1 and out["errors"] == 1


def test_gap_raise_after_insert_is_not_counted(db, monkeypatch):
    co, site, folder, session = _gap_world(db)
    fake = FakeS3({"k-1": _done(**{"Pre-pour": _gaps(nc=[(1, "One"), (2, "Two")])})})
    rid = _report(db, co["id"], folder, session, "k-1")

    def boom(*a, **k):
        raise RuntimeError("after the inserts")
    monkeypatch.setattr(lds.checklist_reports, "mark_seen", boom)
    out = lds.emit_checklist_gaps(db, _reader(fake), NOW)
    assert (out["emitted"], out["duplicates"], out["errors"]) == (0, 0, 1)
    assert _events(db, co["id"]) == [] and _seen(db, rid) == 0
