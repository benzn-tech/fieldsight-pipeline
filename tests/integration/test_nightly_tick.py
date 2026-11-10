"""Integration: the nightly tick, the nightly.due run-rule and the in-VPC handler
(nightly_rematch) against real Postgres.

Spec docs/superpowers/specs/2026-11-10-nightly-rematch-agent-design.md §4 (tick, hub, flag)
and §6 (brakes); plan Task 6.

Every SQL path runs here: due_sites, emit_nightly_due, the run-rule claim, J3 markers, the
J2 reads (sessions, budget, answered keys, live sessions). S3 is an in-memory fake. All text
is synthetic. Nothing here commits; no decision_ledger row is written.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as dt
import io
import json
import uuid

import psycopg
import pytest

import decision_hub
import decision_rules
import lambda_decision_scan as lds
import nightly_rematch as nr
from repositories import (companies, decision_records, event_links, event_runs, nightly,
                          sites, topics, users)
from repositories import site_events as se

pytestmark = pytest.mark.integration

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 10, 12, 30, tzinfo=UTC)        # 01:30 NZDT Wed 11 Nov
NIGHT = dt.date(2026, 11, 10)
DAY = dt.date(2026, 11, 10)
TEXT_COL = {"action_item": "text", "finding": "observation", "decision": "decision",
            "question": "question"}


# ---------------------------------------------------------------- fakes and seeds

class FakeS3:
    """Enough of S3 for the handler: head/get/put/list on one dict."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []
        self.heads = []

    def _missing(self, op):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": "404" if op == "HeadObject" else "NoSuchKey"}},
                           op)

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        if Key not in self.objects:
            raise self._missing("HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._missing("GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        raw = Body if isinstance(Body, bytes) else str(Body).encode("utf-8")
        self.objects[Key] = raw
        self.puts.append(Key)
        return {}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_paginator(self, op):
        """For the worker's transcript key discovery (session_turns)."""
        fake = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield fake.list_objects_v2(Bucket, Prefix)
        return P()

    def requests(self):
        return [k for k in self.puts if k.startswith(nr.PREFIX)]

    def body(self, key):
        return json.loads(self.objects[key])


class Seed:
    def __init__(self, db, co=None):
        self.db = db
        tag = uuid.uuid4().hex[:8]
        self.co = co or companies.create_company(db, f"NightlyT-Co-{tag}")
        self.site = sites.create_site(db, self.co["id"], f"NightlyT-Site-{tag}")
        self.folder = f"NightlyTF-{tag}"
        self.user = users.upsert_field_only_user(db, self.co["id"], self.folder,
                                                 "Fol", "Der", "worker")

    def session(self, base=None, *, items=1, written=NOW - dt.timedelta(hours=3),
                time_range="10:00 – 10:05", texts=None, day=DAY):
        base = base or ("sid" + uuid.uuid4().hex)
        key = f"extractions/{self.folder}/{day.isoformat()}/{base}.json"
        t = topics.upsert_topic(self.db, self.site["id"], day, f"Topic {base}",
                                user_id=self.user["id"], source_s3_key=key, summary="s",
                                time_range=time_range)
        self.db.execute("UPDATE topics SET created_at = %s WHERE id = %s", (written, t["id"]))
        rows = []
        for i in range(items):
            text = (texts[i] if texts else f"synthetic item {i}")
            r = self.db.execute(
                "INSERT INTO action_items (topic_id, site_id, text) VALUES (%s,%s,%s) "
                "RETURNING id::text, stable_id::text", (t["id"], self.site["id"], text)
            ).fetchone()
            rows.append({"row_id": r[0], "stable_id": r[1]})
        return {"base": base, "key": key, "topic": t, "rows": rows}

    def ctx(self, flag="shadow", s3=None, now=NOW):
        run = event_runs.start(self.db, self.co["id"], self.site["id"], [], 1, "nightly.due")
        return decision_hub.RunContext(
            run_id=run["run_id"], company_id=self.co["id"], site_id=self.site["id"],
            now_utc=now, flag=flag, s3_client=s3 if s3 is not None else FakeS3(),
            bucket="lake")

    def event(self, night=NIGHT, now=NOW):
        return lds._new_event(self.co["id"], self.site["id"], "nightly_scan", "nightly.due",
                              str(self.site["id"]), now, now, {"night": night.isoformat()})


def _record(db, s, key, *, created=None, subject=None):
    rec = decision_records.insert(
        db, company_id=s.co["id"], site_id=s.site["id"], kind="nightly_support",
        subject_type="action_item", subject_stable_id=subject or str(uuid.uuid4()),
        provider="anthropic", output={"turn": None, "guard": None},
        auto_outcome="abstained", input_key=key)
    if created is not None:
        db.execute("UPDATE decision_records SET created_at = %s WHERE id = %s",
                   (created, rec["id"]))
    return rec


def _events(db, site_id, type_="nightly.due"):
    return db.cursor(row_factory=psycopg.rows.dict_row).execute(
        "SELECT * FROM site_events WHERE site_id = %s AND type = %s ORDER BY seq",
        (site_id, type_)).fetchall()


def _mine(rows, s):
    return [r for r in rows if str(r["site_id"]) == str(s.site["id"])]


# ---------------------------------------------------------------- the rule

def test_nightly_due_rule_is_a_run_rule_bucketed_by_night():
    rule = decision_rules.RULES["nightly.due"]
    assert rule.run is not None and rule.build_card is None
    assert rule.late_after_days == 1
    ev = decision_hub.Event(event_id="e", company_id="c", site_id="s", source="nightly_scan",
                            type="nightly.due", subject_ref="s", occurred_at=NOW,
                            received_at=NOW, payload={"night": "2026-11-10"}, chain_id=None,
                            chain_depth=0)
    assert rule.bucket(ev, dt.date(2026, 11, 11)) == "2026-11-10"
    assert decision_hub.dedupe_key(ev, dt.date(2026, 11, 11)) == \
        "c|s|nightly.due|s|2026-11-10"


# ---------------------------------------------------------------- due_sites / emit

def test_due_sites_window(db):
    s = Seed(db)

    def due(at):
        return [d for d in nr.due_sites(db, at) if str(d["site_id"]) == str(s.site["id"])]

    at_01 = dt.datetime(2026, 11, 10, 12, 0, tzinfo=UTC)       # 01:00 NZDT 11 Nov
    [d] = due(at_01)
    assert d["night"] == dt.date(2026, 11, 10) and str(d["company_id"]) == str(s.co["id"])
    assert due(dt.datetime(2026, 11, 10, 15, 59, tzinfo=UTC))  # 04:59 still in range
    assert due(dt.datetime(2026, 11, 10, 16, 0, tzinfo=UTC)) == []   # 05:00
    assert due(dt.datetime(2026, 11, 10, 11, 0, tzinfo=UTC)) == []   # 00:00
    out = lds.emit_nightly_due(db, at_01)
    assert out["emitted"] >= 1
    assert due(dt.datetime(2026, 11, 10, 13, 0, tzinfo=UTC)) == []   # emitted tonight
    # the next night is due again
    assert due(dt.datetime(2026, 11, 11, 12, 0, tzinfo=UTC))[0]["night"] == \
        dt.date(2026, 11, 11)


def test_due_sites_nzst_offset(db):
    s = Seed(db)

    def due(at):
        return [d for d in nr.due_sites(db, at) if str(d["site_id"]) == str(s.site["id"])]

    [d] = due(dt.datetime(2026, 7, 1, 13, 0, tzinfo=UTC))      # 01:00 NZST 2 Jul
    assert d["night"] == dt.date(2026, 7, 1)
    assert due(dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)) == []     # 00:00 NZST


def test_site_local_now_is_nz():
    assert nr.site_local_now({}, dt.datetime(2026, 11, 10, 12, 0, tzinfo=UTC)) == \
        dt.datetime(2026, 11, 11, 1, 0)
    assert nr.site_local_now({}, dt.datetime(2026, 7, 1, 13, 0, tzinfo=UTC)) == \
        dt.datetime(2026, 7, 2, 1, 0)


def test_emit_writes_the_event_shape(db):
    s = Seed(db)
    at = dt.datetime(2026, 11, 10, 12, 5, tzinfo=UTC)
    lds.emit_nightly_due(db, at)
    [ev] = _events(db, s.site["id"])
    assert (ev["source"], ev["type"], ev["subject_ref"]) == (
        "nightly_scan", "nightly.due", str(s.site["id"]))
    assert ev["occurred_at"] == at and ev["received_at"] == at
    assert ev["payload"] == {"night": "2026-11-10"} and ev["bucket"] == "2026-11-10"
    assert ev["status"] == "queued" and str(ev["company_id"]) == str(s.co["id"])


def test_second_tick_same_night_is_duplicate(db):
    s = Seed(db)
    at = dt.datetime(2026, 11, 10, 12, 5, tzinfo=UTC)
    lds.emit_nightly_due(db, at)
    lds.emit_nightly_due(db, at + dt.timedelta(minutes=30))        # EventBridge retry
    lds.emit_nightly_due(db, at + dt.timedelta(hours=1))           # next tick, same night
    # a racing second emit that passed due_sites still lands as a duplicate row
    ev = s.event(night=dt.date(2026, 11, 10), now=at + dt.timedelta(hours=2))
    assert lds._insert_event(db, ev, dt.date(2026, 11, 11)) is False
    rows = _events(db, s.site["id"])
    assert [r["status"] for r in rows] == ["queued", "duplicate"]


def test_inactive_company_gets_no_requests(db, monkeypatch):
    s = Seed(db)
    s.session()
    db.execute("UPDATE companies SET status = 'suspended' WHERE id = %s", (s.co["id"],))
    at = dt.datetime(2026, 11, 10, 12, 5, tzinfo=UTC)
    for enforce in ("off", "on"):
        monkeypatch.setenv("COMPANY_STATUS_ENFORCE", enforce)
        assert [d for d in nr.due_sites(db, at)
                if str(d["site_id"]) == str(s.site["id"])] == []
    # an event that got in anyway (emitted before the suspension) is blocked by the hub
    lds._insert_event(db, s.event(now=at), dt.date(2026, 11, 11))
    monkeypatch.setenv("NIGHTLY_REMATCH", "on")
    fake = FakeS3()
    lds.process(db, at, s3_client=fake, bucket="lake")
    [row] = _events(db, s.site["id"])
    assert row["status"] == "blocked"
    assert fake.requests() == []


# ---------------------------------------------------------------- process: one per txn

def test_run_rule_events_are_claimed_one_per_transaction(db, monkeypatch):
    s = Seed(db)
    s2 = Seed(db)
    for seed in (s, s2):
        lds._insert_event(db, seed.event(), dt.date(2026, 11, 11))
    calls = []
    real = se.claim_due

    def spy(conn, now, limit=50, **kw):
        calls.append((limit, kw))
        return real(conn, now, limit, **kw)
    monkeypatch.setattr(se, "claim_due", spy)
    monkeypatch.setenv("NIGHTLY_REMATCH", "shadow")
    out = lds.process(db, NOW, s3_client=FakeS3(), bucket="lake")
    assert out["processed"] >= 2
    run_calls = [c for c in calls if "only_types" in c[1]]
    batch_calls = [c for c in calls if "exclude_types" in c[1]]
    assert run_calls and all(lim == 1 for lim, _ in run_calls)
    assert all("nightly.due" in kw["only_types"] for _, kw in run_calls)
    assert batch_calls and all("nightly.due" in kw["exclude_types"] for _, kw in batch_calls)
    for seed in (s, s2):
        [row] = _events(db, seed.site["id"])
        assert row["status"] == "nothing"          # no sessions -> nothing


def test_claim_due_type_filters(db):
    s = Seed(db)
    lds._insert_event(db, s.event(), dt.date(2026, 11, 11))
    other = se.insert(db, {
        "company_id": s.co["id"], "site_id": s.site["id"], "source": "x",
        "type": "todo.overdue", "subject_ref": str(uuid.uuid4()), "bucket": "b",
        "occurred_at": NOW, "received_at": NOW, "payload": {},
        "dedupe_key": f"k-{uuid.uuid4()}", "status": "queued"})[0]
    mine = {str(r["event_id"]) for r in _events(db, s.site["id"])} | {str(other["event_id"])}
    assert se.claim_due(db, NOW, 100, only_types=[]) == []
    got = [r for r in se.claim_due(db, NOW, 100, exclude_types=["nightly.due"])
           if str(r["event_id"]) in mine]
    assert [r["type"] for r in got] == ["todo.overdue"]
    got = [r for r in se.claim_due(db, NOW, 100, only_types=["nightly.due"])
           if str(r["event_id"]) in mine]
    assert [r["type"] for r in got] == ["nightly.due"]
    assert [r for r in se.claim_due(db, NOW, 100) if str(r["event_id"]) in mine] == []


# ---------------------------------------------------------------- the handler

def test_run_returns_nothing_without_new_finals(db):
    s = Seed(db)
    old = s.session(written=NOW - dt.timedelta(days=8))       # outside the 7-day window
    fake = FakeS3()
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on", fake))
    assert outcome == "nothing" and old["base"]
    assert fake.puts == [] and counts["j2_sessions"] == 0


def test_run_raises_without_storage(db):
    s = Seed(db)
    ctx = s.ctx("shadow")
    ctx = decision_hub.RunContext(**{**ctx.__dict__, "s3_client": None})
    with pytest.raises(RuntimeError):
        nr.run(db, s.event(), ctx)


def test_shadow_writes_artifacts_and_no_db_rows_besides_hub(db, monkeypatch):
    s = Seed(db)
    sess = s.session(items=2)
    nightly.mark_recompute_failed(db, s.co["id"], "sid" + uuid.uuid4().hex, "failed")
    # J1: a late Monday final makes both Tuesday finals (sess, written 3 h ago, and an
    # item-less one) stale; shadow only counts -- no nightly_reemits row, no S3 read, no
    # re-emitted request
    s.session(items=0, written=NOW - dt.timedelta(hours=20))
    s.session(items=0, written=NOW - dt.timedelta(hours=1), day=DAY - dt.timedelta(days=1))
    assert len(nr.stale_pairs(db, company_id=s.co["id"], site_id=s.site["id"],
                              now_utc=NOW)) == 2
    lds._insert_event(db, s.event(), dt.date(2026, 11, 11))
    monkeypatch.setenv("NIGHTLY_REMATCH", "shadow")
    monkeypatch.setenv("CROSS_DAY_CONTINUITY", "on")
    monkeypatch.setenv("RECONCILE_STATUS", "true")
    fake = FakeS3()
    before = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("decision_records", "event_links", "nightly_reemits",
                        "nightly_requested_items")}
    lds.process(db, NOW, s3_client=fake, bucket="lake")

    [row] = _events(db, s.site["id"])
    assert row["status"] == "done"
    [key] = fake.requests()
    # no continuity_xday_requests/ or reconcile_requests/ (lake_put adds a manifest line)
    assert [k for k in fake.puts if not k.startswith("lake_manifest/")] == [key]
    assert key == nr.request_key(s.site["id"], NIGHT, sess["key"])
    assert key.startswith(f"nightly_requests/{s.site['id']}/2026-11-10/")
    body = fake.body(key)
    run_row = db.execute("SELECT run_id, outcome FROM event_runs WHERE site_id = %s",
                         (s.site["id"],)).fetchall()
    assert [r[1] for r in run_row] == ["done"]
    assert body["flag"] == "shadow" and body["run_id"] == str(run_row[0][0])
    assert (body["site_id"], body["session_base"], body["extraction_key"]) == (
        str(s.site["id"]), sess["base"], sess["key"])
    assert (body["user_folder"], body["date"]) == (s.folder, DAY.isoformat())
    assert sorted(i["stable_id"] for i in body["items"]) ==         sorted(r["stable_id"] for r in sess["rows"])
    assert set(body["items"][0]) == {"stable_id", "row_id", "kind", "text", "topic_index",
                                     "time_range"}
    after = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert after == before
    [marker] = nightly.failed_recomputes(db, company_id=s.co["id"])
    assert marker["resolved_at"] is None


def test_run_respects_20_sessions_and_budget(db, monkeypatch):
    s = Seed(db)
    for i in range(22):
        s.session(written=NOW - dt.timedelta(hours=3, minutes=i))
    fake = FakeS3()
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("shadow", fake))
    assert outcome == "done" and len(fake.requests()) == 20
    assert counts["j2_sessions"] == 20 and counts["j2_items"] == 20

    b = Seed(db)
    sessions = [b.session(items=2, written=NOW - dt.timedelta(hours=3, minutes=i))
                for i in range(3)]
    _record(db, b, "nightly_requests/other.json")               # one record today
    monkeypatch.setenv("NIGHTLY_MAX_ITEMS_PER_COMPANY_DAY", "3")
    fake = FakeS3()
    outcome, counts = nr.run_counts(db, b.event(), b.ctx("shadow", fake))
    # 1 used + 2 = 3 fits; the next session (+2) would exceed it: stop, the rest wait
    assert fake.requests() == [nr.request_key(b.site["id"], NIGHT, sessions[0]["key"])]
    assert counts["budget_paused"] == 2 and counts["j2_items"] == 2


def test_items_without_window_or_text_are_not_sent_and_cap_is_60(db):
    s = Seed(db)
    s.session(time_range=None, written=NOW - dt.timedelta(hours=2))
    big = s.session(items=61, written=NOW - dt.timedelta(hours=3))
    fake = FakeS3()
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("shadow", fake))
    assert fake.requests() == [nr.request_key(s.site["id"], NIGHT, big["key"])]
    assert len(fake.body(fake.requests()[0])["items"]) == 60
    assert counts["skipped_no_window"] == 1 and counts["items_over_cap"] == 1


def test_group_and_live_sessions_are_skipped(db):
    s = Seed(db)
    grp = s.session(base="grp" + uuid.uuid4().hex[:12])
    live = s.session()
    sent = s.session()
    for sess, status in ((live, "finalizing"), (sent, "sent")):
        db.execute(
            "INSERT INTO meeting_session (session_id, company_id, user_id, site_id, status) "
            "VALUES (%s,%s,%s,%s,%s)",
            (sess["base"][3:], s.co["id"], s.user["id"], s.site["id"], status))
    fake = FakeS3()
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("shadow", fake))
    assert fake.requests() == [nr.request_key(s.site["id"], NIGHT, sent["key"])]
    assert counts["skipped_group"] == 1 and counts["skipped_live"] == 1
    assert grp["base"]


def test_hub_retry_does_not_rewrite_existing_artifact(db):
    s = Seed(db)
    a, b = s.session(), s.session(written=NOW - dt.timedelta(hours=4))
    key_a = nr.request_key(s.site["id"], NIGHT, a["key"])
    key_b = nr.request_key(s.site["id"], NIGHT, b["key"])
    fake = FakeS3({key_a: b'{"flag": "on"}'})
    _record(db, s, key_b)                                      # b already answered
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on", fake))
    assert fake.requests() == [] and outcome == "nothing"
    assert counts["skipped_existing"] == 2
    assert key_b not in fake.heads                             # answered: no S3 call at all


def test_head_object_denied_is_raised_not_treated_as_missing(db):
    from botocore.exceptions import ClientError
    s = Seed(db)
    s.session()

    class Denied(FakeS3):
        def head_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "403"}}, "HeadObject")
    fake = Denied()
    with pytest.raises(ClientError):
        nr.run_counts(db, s.event(), s.ctx("shadow", fake))
    assert fake.puts == []


def _prev_night_artifacts(s, n, flag):
    prev = NIGHT - dt.timedelta(days=1)
    return {f"nightly_requests/{s.site['id']}/{prev}/{i:04d}.json":
            json.dumps({"flag": flag}).encode() for i in range(n)}


def test_breaker_opens_after_5_unanswered_requests(db, caplog):
    s = Seed(db)
    s.session()
    fake = FakeS3(_prev_night_artifacts(s, 5, "on"))
    with caplog.at_level("WARNING"):
        outcome, counts = nr.run_counts(db, s.event(), s.ctx("on", fake))
    assert counts["j2_breaker_open"] == 1 and fake.requests() == []
    assert f"nightly breaker site={s.site['id']} missing=5" in caplog.text

    # one of the five answered -> 4 missing -> closed
    t = Seed(db)
    t.session()
    objs = _prev_night_artifacts(t, 5, "on")
    _record(db, t, sorted(objs)[0])
    fake = FakeS3(objs)
    outcome, counts = nr.run_counts(db, t.event(), t.ctx("on", fake))
    assert counts["j2_breaker_open"] == 0 and len(fake.requests()) == 1


def test_breaker_ignores_shadow_artifacts(db):
    s = Seed(db)
    s.session()
    fake = FakeS3(_prev_night_artifacts(s, 8, "shadow"))
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on", fake))
    assert counts["j2_breaker_open"] == 0 and len(fake.requests()) == 1
    # and in shadow the breaker is not consulted at all, even with 8 unanswered "on" ones
    t = Seed(db)
    t.session()
    fake = FakeS3(_prev_night_artifacts(t, 8, "on"))
    outcome, counts = nr.run_counts(db, t.event(), t.ctx("shadow", fake))
    assert counts["j2_breaker_open"] == 0 and len(fake.requests()) == 1


def test_failed_recompute_resolved(db, monkeypatch):
    from repositories import claim_type_store
    s = Seed(db)
    ok_base, bad_base = "sid" + uuid.uuid4().hex, "sid" + uuid.uuid4().hex
    nightly.mark_recompute_failed(db, s.co["id"], ok_base, "timed_out")
    nightly.mark_recompute_failed(db, s.co["id"], bad_base, "failed")

    # shadow: counted, nothing resolved
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("shadow"))
    assert counts["j3_pending"] == 2 and counts["j3_recomputed"] == 0
    assert len(nightly.failed_recomputes(db, company_id=s.co["id"])) == 2

    real = claim_type_store.recompute_session
    seen = []

    def recompute(conn, **kw):
        seen.append(kw)
        if kw["session_base"] == bad_base:
            return {"rows": 0, "changed": 0, "claim": {}, "sites": 1, "failed": 1}
        return real(conn, **kw)
    monkeypatch.setattr(claim_type_store, "recompute_session", recompute)
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert outcome == "done"
    assert counts["j3_recomputed"] == 1 and counts["j3_failed"] == 1
    assert {k["session_base"] for k in seen} == {ok_base, bad_base}
    assert all(k["site_id"] is None and k["company_id"] == s.co["id"] for k in seen)
    [left] = nightly.failed_recomputes(db, company_id=s.co["id"])
    assert left["session_base"] == bad_base
    # the hub's statement_timeout is not left behind for the rest of the transaction
    assert db.execute("SHOW statement_timeout").fetchone()[0] in ("0", "0ms")


# ---------------------------------------------------------------- repository reads

def test_answered_input_keys_scoped_by_tenant(db):
    s, other = Seed(db), Seed(db)
    _record(db, s, "nightly_requests/a.json")
    _record(db, other, "nightly_requests/b.json")
    assert nightly.answered_input_keys(
        db, s.co["id"], s.site["id"],
        ["nightly_requests/a.json", "nightly_requests/b.json", "x"]) == \
        {"nightly_requests/a.json"}
    assert nightly.answered_input_keys(db, s.co["id"], s.site["id"], []) == set()


def test_live_session_ids(db):
    s = Seed(db)
    ids = {st: uuid.uuid4().hex for st in ("open", "pending_close", "finalizing", "sent",
                                           "failed")}
    for st, sid in ids.items():
        db.execute("INSERT INTO meeting_session (session_id, company_id, user_id, status) "
                   "VALUES (%s,%s,%s,%s)", (sid, s.co["id"], s.user["id"], st))
    got = nightly.live_session_ids(db, list(ids.values()) + [uuid.uuid4().hex])
    assert got == {ids["open"], ids["pending_close"], ids["finalizing"]}
    assert nightly.live_session_ids(db, []) == set()


# ---------------------------------------------------------------- the Lambda handler

def test_handler_nightly_tick_emits_then_processes(db, monkeypatch):
    real_now = dt.datetime.now(UTC)
    s = Seed(db)
    s.session(written=real_now - dt.timedelta(hours=3))
    # the handler reads the real clock: make every site's local time 01:30 tonight
    local = nr.site_local_now
    monkeypatch.setattr(nr, "site_local_now",
                        lambda site, now: local(site, now).replace(hour=1, minute=30))
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    monkeypatch.setenv("NIGHTLY_REMATCH", "shadow")
    monkeypatch.setenv("LAKE_BUCKET", "lake")
    fake = FakeS3()

    class Borrowed:
        def __call__(self, *a, **k):
            return self

        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(lds, "get_connection", Borrowed())
    monkeypatch.setattr(lds, "_s3", lambda: fake)
    out = lds.lambda_handler({"scan": "nightly_tick"}, None)
    assert out["emitted"] >= 1 and out["processed"] >= 1
    [row] = _events(db, s.site["id"])
    assert row["status"] == "done" and len(fake.requests()) == 1

    monkeypatch.delenv("LAKE_BUCKET")
    with pytest.raises(RuntimeError):
        lds.lambda_handler({"scan": "nightly_tick"}, None)


# ---------------------------------------------------------------- fix round 1

def test_answered_items_are_not_asked_again(db):
    """Spec §2.2: a night with no new finals costs no call. An item with a nightly_support
    record since its row was written is not requested again; a row written after its last
    record (a re-extraction) is asked once more."""
    s = Seed(db)
    answered = s.session(written=NOW - dt.timedelta(hours=3))
    row = answered["rows"][0]
    _record(db, s, "nightly_requests/last-night.json", subject=row["stable_id"],
            created=NOW - dt.timedelta(hours=1))
    db.execute("UPDATE action_items SET created_at = %s WHERE id = %s",
               (NOW - dt.timedelta(hours=3), row["row_id"]))
    fake = FakeS3()
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on", fake))
    assert outcome == "nothing" and fake.puts == [] and counts["j2_sessions"] == 0

    # the row was rewritten after the record: asked once more
    db.execute("UPDATE action_items SET created_at = %s WHERE id = %s",
               (NOW - dt.timedelta(minutes=30), row["row_id"]))
    fake = FakeS3()
    nr.run_counts(db, s.event(), s.ctx("on", fake))
    assert fake.requests() == [nr.request_key(s.site["id"], NIGHT, answered["key"])]

    # a record of ANOTHER company for the same stable id does not count
    t = Seed(db)
    other = t.session()
    decision_records.insert(
        db, company_id=s.co["id"], site_id=s.site["id"], kind="nightly_support",
        subject_type="action_item", subject_stable_id=other["rows"][0]["stable_id"],
        provider="anthropic", output={}, auto_outcome="abstained", input_key="x")
    fake = FakeS3()
    nr.run_counts(db, t.event(), t.ctx("on", fake))
    assert len(fake.requests()) == 1


def test_run_rule_raise_keeps_the_committed_run_named_in_the_artifact(db, monkeypatch):
    s = Seed(db)
    s.session(written=NOW - dt.timedelta(hours=3))
    s.session(written=NOW - dt.timedelta(hours=4))
    lds._insert_event(db, s.event(), dt.date(2026, 11, 11))

    class SecondPutFails(FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            if Key.startswith(nr.PREFIX) and self.requests():
                raise RuntimeError("s3 put failed")
            return super().put_object(Bucket, Key, Body, **kw)
    fake = SecondPutFails()
    monkeypatch.setenv("NIGHTLY_REMATCH", "shadow")
    lds.process(db, NOW, s3_client=fake, bucket="lake")
    [key] = fake.requests()                                  # the first put landed
    run_id = fake.body(key)["run_id"]
    [ev] = _events(db, s.site["id"])
    assert ev["status"] == "queued" and ev["attempts"] == 1
    runs = db.execute(
        "SELECT r.run_id::text, r.outcome, r.ended_at IS NOT NULL FROM event_runs r "
        "JOIN run_events re ON re.run_id = r.run_id WHERE re.event_id = %s",
        (ev["event_id"],)).fetchall()
    assert runs == [(run_id, "failed", True)]
    assert sum(1 for r in runs if r[1] == "failed") == ev["attempts"]


def test_abandoned_run_rule_run_is_recorded_as_a_failed_attempt(db, monkeypatch):
    s = Seed(db)
    lds._insert_event(db, s.event(), dt.date(2026, 11, 11))
    [ev] = _events(db, s.site["id"])
    se.set_status(db, ev["event_id"], s.co["id"], s.site["id"], "running")
    run = event_runs.start(db, s.co["id"], s.site["id"], [ev["event_id"]], 1, "nightly.due")
    event_runs.set_lease(db, run["run_id"], s.co["id"], s.site["id"],
                         NOW - dt.timedelta(minutes=1))
    # a live run (lease in the future) is left alone
    t = Seed(db)
    lds._insert_event(db, t.event(), dt.date(2026, 11, 11))
    [tev] = _events(db, t.site["id"])
    se.set_status(db, tev["event_id"], t.co["id"], t.site["id"], "running")
    trun = event_runs.start(db, t.co["id"], t.site["id"], [tev["event_id"]], 1, "nightly.due")
    event_runs.set_lease(db, trun["run_id"], t.co["id"], t.site["id"],
                         NOW + dt.timedelta(minutes=5))
    monkeypatch.setenv("NIGHTLY_REMATCH", "shadow")
    lds.process(db, NOW, s3_client=FakeS3(), bucket="lake")
    [ev] = _events(db, s.site["id"])
    assert ev["status"] == "queued" and ev["attempts"] == 1
    assert "abandoned" in ev["last_error"]
    outcomes = db.execute(
        "SELECT r.run_id, r.outcome FROM event_runs r JOIN run_events re "
        "ON re.run_id = r.run_id WHERE re.event_id = %s", (ev["event_id"],)).fetchall()
    assert outcomes == [(run["run_id"], "failed")]
    [tev] = _events(db, t.site["id"])
    assert tev["status"] == "running" and tev["attempts"] == 0


def test_budget_counts_the_companys_other_sites_tonight(db, monkeypatch):
    a = Seed(db)
    b = Seed(db, co=a.co)
    a.session(items=2)
    b.session(items=2)
    monkeypatch.setenv("NIGHTLY_MAX_ITEMS_PER_COMPANY_DAY", "3")
    fa = FakeS3()
    nr.run_counts(db, a.event(), a.ctx("on", fa))
    assert len(fa.requests()) == 1
    assert db.execute("SELECT site_id::text, night, company_id::text, items "
                      "FROM nightly_requested_items WHERE company_id = %s",
                      (a.co["id"],)).fetchall() == [
        (str(a.site["id"]), NIGHT, str(a.co["id"]), 2)]
    fb = FakeS3()
    outcome, counts = nr.run_counts(db, b.event(), b.ctx("on", fb))
    assert fb.requests() == [] and counts["budget_paused"] == 1
    # a site's own row never counts against itself
    assert nightly.requested_items_other_sites(db, a.co["id"], a.site["id"], NIGHT) == 0
    assert nightly.requested_items_other_sites(db, a.co["id"], b.site["id"], NIGHT) == 2


def test_shadow_writes_no_requested_items_row(db):
    s = Seed(db)
    s.session(items=2)
    fake = FakeS3()
    nr.run_counts(db, s.event(), s.ctx("shadow", fake))
    assert len(fake.requests()) == 1
    assert db.execute("SELECT count(*) FROM nightly_requested_items WHERE site_id = %s",
                      (s.site["id"],)).fetchone()[0] == 0



# ------------------------------------------------- unanswerable requests are answered

class InProcessWriter:
    """A Lambda client whose invoke runs the real in-VPC writer on the test connection."""

    def __init__(self, db):
        self.db, self.payloads = db, []

    def invoke(self, FunctionName, InvocationType, Payload):
        import lambda_nightly_support_writer as writer
        event = json.loads(Payload)
        self.payloads.append(event)
        out = writer.write_support(self.db, event)
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(out).encode("utf-8"))}


def test_request_with_no_transcript_is_answered_and_not_resent(db):
    """Final review I1: a request that can never produce a pick (its transcripts are gone)
    is answered with abstained records and no model call, so the next night's breaker
    counts it answered and sessions_needing_support does not re-send its items."""
    import lambda_nightly_turn_pick as worker
    s = Seed(db)
    sessions = [s.session() for _ in range(nr.BREAKER_AT)]
    fake = FakeS3()
    ctx = s.ctx("on", fake)
    nr.run_counts(db, s.event(), ctx)
    keys = fake.requests()
    assert len(keys) == nr.BREAKER_AT

    lam = InProcessWriter(db)
    for key in keys:                     # no transcripts in the fake: every one is no_transcript
        out = worker.process_request("lake", key, fake.body(key), s3_client=fake,
                                     lambda_client=lam, model_client=object())
        assert out["no_transcript"] == 1 and out["writer"]["records"] == 1
    recs = db.execute(
        "SELECT subject_stable_id::text, auto_outcome, output, input_key FROM decision_records "
        "WHERE site_id = %s AND kind = 'nightly_support'", (s.site["id"],)).fetchall()
    assert sorted(r[0] for r in recs) == sorted(x["rows"][0]["stable_id"] for x in sessions)
    assert {(r[1], json.dumps(r[2], sort_keys=True)) for r in recs} == {
        ("abstained", json.dumps({"guard": "no_transcript", "turn": None}, sort_keys=True))}
    assert sorted(r[3] for r in recs) == sorted(keys)
    run = db.execute("SELECT model_calls, models, tokens_in, cost_usd FROM event_runs "
                     "WHERE run_id = %s", (ctx.run_id,)).fetchone()
    assert run == (0, None, 0, None)                  # no model call was counted

    # the following night: breaker closed (all answered), nothing re-sent
    nxt = NOW + dt.timedelta(days=1)
    assert nightly.sessions_needing_support(
        db, company_id=s.co["id"], site_id=s.site["id"],
        since_utc=nxt - dt.timedelta(days=7)) == []
    before = set(fake.requests())
    outcome, counts = nr.run_counts(db, s.event(night=NIGHT + dt.timedelta(days=1), now=nxt),
                                    s.ctx("on", fake, now=nxt))
    assert counts["j2_breaker_open"] == 0
    assert counts["j2_sessions"] == 0 and set(fake.requests()) == before


def test_breaker_still_opens_when_requests_go_unanswered(db):
    """The control for the test above: the same five requests with no writer invoke leave
    the breaker open the next night."""
    s = Seed(db)
    for _ in range(nr.BREAKER_AT):
        s.session()
    fake = FakeS3()
    nr.run_counts(db, s.event(), s.ctx("on", fake))
    assert len(fake.requests()) == nr.BREAKER_AT
    nxt = NOW + dt.timedelta(days=1)
    outcome, counts = nr.run_counts(db, s.event(night=NIGHT + dt.timedelta(days=1), now=nxt),
                                    s.ctx("on", fake, now=nxt))
    assert counts["j2_breaker_open"] == 1
