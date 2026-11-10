"""Integration: nightly re-match J1, catch-up for late arrivals, against real Postgres.

Spec docs/superpowers/specs/2026-11-10-nightly-rematch-agent-design.md §3 J1, §6; plan Task 8.

A later-uploaded recording L (recorded before an already-final session N, final written after
N's) makes N's per-session requests stale. In `on` the nightly handler re-emits N's requests
through the OWNING lines' own builders -- the real `build_xday_request` and
`reconcile_request.build` run here on seeded rows -- and puts them under those lines' prefixes.
S3 is an in-memory fake. All text is synthetic. Nothing here commits.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as dt
import io
import json
import uuid

import pytest

import continuity_xday_request
import decision_hub
import lambda_decision_scan as lds
import nightly_rematch as nr
import reconcile_request
from repositories import companies, event_runs, nightly, sites, topics, users

pytestmark = pytest.mark.integration

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 10, 12, 30, tzinfo=UTC)        # 01:30 NZDT Wed 11 Nov
NIGHT = dt.date(2026, 11, 10)
MON = dt.date(2026, 11, 9)
TUE = dt.date(2026, 11, 10)
XDAY = continuity_xday_request.PREFIX
RECON = reconcile_request.PREFIX


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []
        self.gets = []

    def _missing(self, op):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": "404" if op == "HeadObject" else "NoSuchKey"}},
                           op)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._missing("HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
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

    def under(self, prefix):
        return [k for k in self.puts if k.startswith(prefix)]

    def body(self, key):
        return json.loads(self.objects[key])


class Seed:
    def __init__(self, db):
        self.db = db
        tag = uuid.uuid4().hex[:8]
        self.co = companies.create_company(db, f"CatchupT-Co-{tag}")
        self.site = sites.create_site(db, self.co["id"], f"CatchupT-Site-{tag}")
        self.folder = f"CatchupTF-{tag}"
        self.user = users.upsert_field_only_user(db, self.co["id"], self.folder,
                                                 "Fol", "Der", "worker")
        self.s3 = FakeS3()

    def session(self, day, *, written, opened_at=None, items=("synthetic block pour",),
                folder=None, with_session=True):
        """One final session recorded on `day` (topic report_date), final written at
        `written`. A meeting_session row (status sent) carries `opened_at`; with
        with_session=False there is none (a sid with no row: recording instant unknown).
        N's final extraction JSON goes into the fake S3."""
        hexid = uuid.uuid4().hex
        base = "sid" + hexid
        folder = folder or self.folder
        key = f"extractions/{folder}/{day.isoformat()}/{base}.json"
        if with_session:
            self.db.execute(
                "INSERT INTO meeting_session (session_id, company_id, user_id, site_id, "
                "status, opened_at) VALUES (%s,%s,%s,%s,'sent',%s)",
                (hexid, self.co["id"], self.user["id"], self.site["id"], opened_at))
        t = topics.upsert_topic(self.db, self.site["id"], day, f"Topic {base}",
                                user_id=self.user["id"], source_s3_key=key, summary="s",
                                time_range="10:00 – 10:05")
        self.db.execute("UPDATE topics SET created_at = %s WHERE id = %s", (written, t["id"]))
        for text in items:
            self.db.execute(
                "INSERT INTO action_items (topic_id, site_id, text, created_at) "
                "VALUES (%s,%s,%s,%s)", (t["id"], self.site["id"], text, written))
        self.s3.objects[key] = json.dumps({
            "tier": "final", "extracted_at": written.isoformat(), "topics": []}).encode()
        return {"base": base, "key": key, "day": day}

    def ctx(self, flag="on", now=NOW):
        run = event_runs.start(self.db, self.co["id"], self.site["id"], [], 1, "nightly.due")
        return decision_hub.RunContext(
            run_id=run["run_id"], company_id=self.co["id"], site_id=self.site["id"],
            now_utc=now, flag=flag, s3_client=self.s3, bucket="lake")

    def event(self, night=NIGHT, now=NOW):
        return lds._new_event(self.co["id"], self.site["id"], "nightly_scan", "nightly.due",
                              str(self.site["id"]), now, now, {"night": night.isoformat()})

    def pairs(self, now=NOW, **kw):
        return nr.stale_pairs(self.db, company_id=self.co["id"], site_id=self.site["id"],
                              now_utc=now, **kw)

    def j1_puts(self):
        return self.s3.under(XDAY) + self.s3.under(RECON)

    def reemits(self):
        return self.db.execute(
            "SELECT line, n_extraction_key, l_extraction_key, artifact_key, night "
            "FROM nightly_reemits WHERE site_id = %s ORDER BY line, n_extraction_key",
            (self.site["id"],)).fetchall()


@pytest.fixture
def lines_on(monkeypatch):
    monkeypatch.setenv("CROSS_DAY_CONTINUITY", "on")
    monkeypatch.setenv("RECONCILE_STATUS", "true")


def _at(day, hh, mm=0):
    """NZ wall clock -> aware UTC (NZDT in November, +13)."""
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC) - dt.timedelta(
        hours=13)


# ---------------------------------------------------------------- detection

def test_late_monday_upload_makes_tuesday_stale(db, lines_on):
    s = Seed(db)
    n = s.session(TUE, written=NOW - dt.timedelta(hours=20), opened_at=_at(TUE, 9),
                  items=("synthetic scaffold check",))
    late = s.session(MON, written=NOW - dt.timedelta(hours=2), opened_at=_at(MON, 9),
                     items=("synthetic scaffold check tomorrow",))
    # an on-time Monday session (written before N) makes nothing stale
    s.session(MON, written=NOW - dt.timedelta(hours=30), opened_at=_at(MON, 7))

    pairs = s.pairs()
    assert [(p["line"], p["n_extraction_key"], p["l_extraction_key"]) for p in pairs] == [
        ("xday", n["key"], late["key"])]

    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert outcome == "done"
    assert counts["j1_stale"] == 1 and counts["j1_reemitted"] == 1
    [key] = s.s3.under(XDAY)
    assert s.s3.under(RECON) == []
    body = s.s3.body(key)
    assert body["extraction_key"] == n["key"] and body["trigger"] == "nightly"
    assert body["nonce"] == NIGHT.isoformat() and body["mode"] == "on"
    assert key == continuity_xday_request.key_for(str(s.site["id"]), TUE, body["run_hash"])
    # the late Monday item is now a candidate of Tuesday's request
    assert any(c["first_date"] == MON.isoformat() for c in body["candidates"])
    assert s.reemits() == [("xday", n["key"], late["key"], key, NIGHT)]


def test_same_day_ordering_uses_recorded_at(db, lines_on):
    """R0: same recorder, same NZ date -- the recording instants order the pair."""
    s = Seed(db)
    n = s.session(TUE, written=NOW - dt.timedelta(hours=10), opened_at=_at(TUE, 10),
                  items=("synthetic rebar delivery",))
    late = s.session(TUE, written=NOW - dt.timedelta(hours=1), opened_at=_at(TUE, 8),
                     items=("synthetic rebar delivery booked",))
    # recorded AFTER N (11:00 > 10:00), though written later: not stale
    s.session(TUE, written=NOW - dt.timedelta(hours=1), opened_at=_at(TUE, 11))

    got = sorted((p["line"], p["n_extraction_key"], p["l_extraction_key"])
                 for p in s.pairs())
    assert got == [("reconcile", n["key"], late["key"]), ("xday", n["key"], late["key"])]

    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    [rkey] = s.s3.under(RECON)
    assert rkey == reconcile_request.request_key(str(s.site["id"]), TUE.isoformat(), n["key"])
    body = s.s3.body(rkey)
    assert body["trigger"] == "nightly" and body["extraction_key"] == n["key"]
    assert body["user_folder"] == s.folder and body["session_base"] == n["base"]
    # the late item is now a candidate (which candidates qualify is the status line's rule)
    assert "synthetic rebar delivery booked" in [c["text"] for c in body["candidates"]]
    assert ("reconcile", n["key"], late["key"], rkey, NIGHT) in s.reemits()


def test_null_recorded_at_is_unordered_and_skipped(db, lines_on):
    s = Seed(db)
    s.session(TUE, written=NOW - dt.timedelta(hours=10), opened_at=_at(TUE, 10))
    # no meeting_session row: the sid carries no wall clock, recording instant unknown
    s.session(TUE, written=NOW - dt.timedelta(hours=1), with_session=False)
    # a row with NULL opened_at is the same
    s.session(TUE, written=NOW - dt.timedelta(hours=1), opened_at=None)
    assert s.pairs() == []
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert counts["j1_stale"] == 0 and s.j1_puts() == [] and s.reemits() == []


def test_l_older_than_window_or_written_long_ago_is_not_stale(db, lines_on):
    s = Seed(db)
    s.session(TUE, written=NOW - dt.timedelta(hours=20), opened_at=_at(TUE, 9))
    # recorded 29 days before N: outside cross-day's 28-day window
    s.session(TUE - dt.timedelta(days=29), written=NOW - dt.timedelta(hours=2),
              opened_at=_at(TUE - dt.timedelta(days=29), 9))
    # final written 73 h ago: outside the 72 h look-back, not a late arrival tonight
    s.session(MON, written=NOW - dt.timedelta(hours=73), opened_at=_at(MON, 9))
    assert s.pairs() == []


def test_reconcile_needs_the_same_recorder(db, lines_on):
    s = Seed(db)
    other = f"{s.folder}-b"
    users.upsert_field_only_user(db, s.co["id"], other, "Oth", "Er", "worker")
    n = s.session(TUE, written=NOW - dt.timedelta(hours=10), opened_at=_at(TUE, 10))
    late = s.session(TUE, written=NOW - dt.timedelta(hours=1), opened_at=_at(TUE, 8),
                     folder=other)
    assert [(p["line"], p["n_extraction_key"], p["l_extraction_key"]) for p in s.pairs()] \
        == [("xday", n["key"], late["key"])]


# ---------------------------------------------------------------- action and brakes

def test_pair_reemitted_once_only(db, lines_on):
    s = Seed(db)
    n = s.session(TUE, written=NOW - dt.timedelta(hours=20), opened_at=_at(TUE, 9),
                  items=("synthetic crane hire",))
    s.session(MON, written=NOW - dt.timedelta(hours=2), opened_at=_at(MON, 9),
              items=("synthetic crane hire quote",))
    nr.run_counts(db, s.event(), s.ctx("on"))
    assert len(s.s3.under(XDAY)) == 1 and len(s.reemits()) == 1
    # a hub retry or a second tick the same night: the pair is recorded, nothing re-emitted
    gets = len(s.s3.gets)
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert len(s.s3.under(XDAY)) == 1 and len(s.reemits()) == 1
    assert counts["j1_reemitted"] == 0 and len(s.s3.gets) == gets
    assert s.pairs() == []
    # record_reemit is the guard even when detection races: a recorded pair is skipped
    # before any S3 read
    assert nightly.record_reemit(db, site_id=s.site["id"], line="xday", n_key=n["key"],
                                 l_key=s.reemits()[0][2], artifact_key="x",
                                 night=NIGHT) is False


def test_record_false_skips_without_s3(db, lines_on, monkeypatch):
    s = Seed(db)
    s.session(TUE, written=NOW - dt.timedelta(hours=20), opened_at=_at(TUE, 9))
    s.session(MON, written=NOW - dt.timedelta(hours=2), opened_at=_at(MON, 9))
    monkeypatch.setattr(nightly, "record_reemit", lambda *a, **k: False)
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert counts["j1_already"] == 1 and counts["j1_reemitted"] == 0
    assert s.s3.gets == [] and s.j1_puts() == []


def test_builder_none_or_failure_leaves_no_record(db, lines_on, monkeypatch):
    """Nothing emitted = nothing recorded (the record's savepoint rolls back)."""
    s = Seed(db)
    n = s.session(TUE, written=NOW - dt.timedelta(hours=20), opened_at=_at(TUE, 9),
                  items=("synthetic alpha",))
    s.session(MON, written=NOW - dt.timedelta(hours=2), opened_at=_at(MON, 9),
              items=("synthetic beta",))
    del s.s3.objects[n["key"]]          # N's final is gone from S3: the read raises
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert counts["j1_failed"] == 1 and s.reemits() == [] and s.j1_puts() == []

    import lambda_item_writer
    monkeypatch.setattr(lambda_item_writer, "build_xday_request", lambda *a, **k: None)
    s.s3.objects[n["key"]] = json.dumps({"tier": "final", "extracted_at": "x"}).encode()
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert counts["j1_built_none"] == 1 and s.reemits() == [] and s.j1_puts() == []


def test_line_off_is_not_reemitted(db, monkeypatch):
    monkeypatch.setenv("CROSS_DAY_CONTINUITY", "off")
    monkeypatch.setenv("RECONCILE_STATUS", "false")
    s = Seed(db)
    s.session(TUE, written=NOW - dt.timedelta(hours=20), opened_at=_at(TUE, 9))
    s.session(MON, written=NOW - dt.timedelta(hours=2), opened_at=_at(MON, 9))
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert counts["j1_stale"] == 1 and counts["j1_line_off"] == 1
    assert s.s3.gets == [] and s.j1_puts() == [] and s.reemits() == []


def test_cap_10_newest_first(db, lines_on):
    s = Seed(db)
    start = MON - dt.timedelta(days=12)
    late = s.session(start, written=NOW - dt.timedelta(hours=2), opened_at=_at(start, 9),
                     items=("synthetic formwork strip",))
    ns = [s.session(start + dt.timedelta(days=i), written=NOW - dt.timedelta(hours=20),
                    opened_at=_at(start + dt.timedelta(days=i), 9),
                    items=(f"synthetic formwork strip level {i}",))
          for i in range(1, 13)]
    newest10 = [x["key"] for x in reversed(ns)][:10]

    pairs = s.pairs()
    assert [p["n_extraction_key"] for p in pairs] == newest10
    assert all(p["l_extraction_key"] == late["key"] for p in pairs)
    assert len(s.pairs(limit=None)) == 12

    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert counts["j1_stale"] == 12 and counts["j1_reemitted"] == 10
    assert counts["j1_over_cap"] == 2
    assert sorted(r[1] for r in s.reemits()) == sorted(newest10)
    emitted = {s.s3.body(k)["extraction_key"] for k in s.s3.under(XDAY)}
    assert emitted == set(newest10)


def test_time_budget_stops_j1_and_counts(db, lines_on):
    s = Seed(db)
    s.session(TUE, written=NOW - dt.timedelta(hours=20), opened_at=_at(TUE, 9))
    s.session(MON, written=NOW - dt.timedelta(hours=2), opened_at=_at(MON, 9))
    ticks = iter([0.0] + [nr.RUN_BUDGET_S + 1.0] * 1000)
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"), clock=lambda: next(ticks))
    assert counts["j1_time_paused"] == 1 and counts["j1_reemitted"] == 0
    assert s.s3.under(XDAY) == [] and s.reemits() == []


def test_shadow_counts_without_put(db, lines_on):
    s = Seed(db)
    s.session(TUE, written=NOW - dt.timedelta(hours=10), opened_at=_at(TUE, 10))
    s.session(TUE, written=NOW - dt.timedelta(hours=1), opened_at=_at(TUE, 8))
    before = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("decision_records", "event_links", "nightly_reemits",
                        "nightly_requested_items")}
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("shadow"))
    assert counts["j1_stale"] == 2                    # (N, L) on both lines
    assert counts["j1_reemitted"] == 0
    assert s.s3.under(XDAY) == [] and s.s3.under(RECON) == [] and s.s3.gets == []
    after = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert after == before


# ---------------------------------------------------------------- fix round 1

def test_detection_raising_puts_no_j2_artifact(db, lines_on, monkeypatch):
    """J1 detection runs before any J2 put: a DB error there fails the attempt while no
    J2 artifact exists (the J2 artifacts stay the run's last writes)."""
    s = Seed(db)
    s.session(TUE, written=NOW - dt.timedelta(hours=3), opened_at=_at(TUE, 9))

    def boom(*a, **k):
        raise RuntimeError("detection failed")
    monkeypatch.setattr(nightly, "site_finals", boom)
    with pytest.raises(RuntimeError):
        nr.run_counts(db, s.event(), s.ctx("on"))
    assert s.s3.under(nr.PREFIX) == [] and s.j1_puts() == []
    # without the fault the same run writes its J2 request
    monkeypatch.undo()
    monkeypatch.setenv("CROSS_DAY_CONTINUITY", "on")
    monkeypatch.setenv("RECONCILE_STATUS", "true")
    nr.run_counts(db, s.event(), s.ctx("on"))
    assert len(s.s3.under(nr.PREFIX)) == 1


def test_over_cap_pair_is_reemitted_the_next_night(db, lines_on):
    s = Seed(db)
    start = MON - dt.timedelta(days=12)
    s.session(start, written=NOW - dt.timedelta(hours=2), opened_at=_at(start, 9),
              items=("synthetic formwork strip",))
    ns = [s.session(start + dt.timedelta(days=i), written=NOW - dt.timedelta(hours=20),
                    opened_at=_at(start + dt.timedelta(days=i), 9),
                    items=(f"synthetic formwork strip level {i}",))
          for i in range(1, 13)]
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert counts["j1_reemitted"] == 10 and counts["j1_over_cap"] == 2
    first = set(s.s3.under(XDAY))

    nxt, nxt_night = NOW + dt.timedelta(hours=24), NIGHT + dt.timedelta(days=1)
    outcome, counts = nr.run_counts(db, s.event(nxt_night, nxt), s.ctx("on", now=nxt))
    assert counts["j1_stale"] == 2 and counts["j1_reemitted"] == 2
    assert counts["j1_over_cap"] == 0
    second = [k for k in s.s3.under(XDAY) if k not in first]
    # the two oldest N, never one of the ten already recorded
    assert {s.s3.body(k)["extraction_key"] for k in second} == {ns[0]["key"], ns[1]["key"]}
    assert len(s.reemits()) == 12
    assert sorted(r[4] for r in s.reemits()) == [NIGHT] * 10 + [nxt_night] * 2


def test_line_in_shadow_is_not_reemitted(db, monkeypatch):
    """Only a line whose own flag is exactly on: a shadow line would consume the pair."""
    monkeypatch.setenv("CROSS_DAY_CONTINUITY", "shadow")
    monkeypatch.setenv("RECONCILE_STATUS", "true")
    s = Seed(db)
    s.session(TUE, written=NOW - dt.timedelta(hours=20), opened_at=_at(TUE, 9))
    s.session(MON, written=NOW - dt.timedelta(hours=2), opened_at=_at(MON, 9))
    outcome, counts = nr.run_counts(db, s.event(), s.ctx("on"))
    assert counts["j1_stale"] == 1 and counts["j1_line_off"] == 1
    assert s.s3.gets == [] and s.j1_puts() == [] and s.reemits() == []
