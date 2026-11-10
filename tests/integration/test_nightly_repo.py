"""Integration: repositories.nightly, the event_links.insert_supported_by extension, and the
voiceprint writer's recompute-failure marker, against a real Postgres (migration 0102).

Every SQL path runs here -- a fake connection never executes SQL. Tests that need a second
connection or the writer's own connections commit their seed rows and delete the company /
site in `finally` (cascade removes the rest). No decision_ledger row is written. All text
is synthetic.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as _dt
import uuid

import psycopg
import pytest

from db.connection import get_connection
from repositories import (companies, decision_records, event_links, nightly, redactions,
                          sites, topics, users)

pytestmark = pytest.mark.integration

vw = pytest.importorskip("lambda_voiceprint_writer")

UTC = _dt.timezone.utc
DAY = _dt.date(2026, 11, 9)
NOW = _dt.datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
TEXT_COL = {"action_item": "text", "finding": "observation", "decision": "decision",
            "question": "question"}


def _turn(start=1.0):
    return {"source_stem": "stem-a", "start_sec": start, "end_sec": start + 3.0,
            "speaker_label": "spk_0", "quote_status": "verified"}


class Seed:
    def __init__(self, db, co=None):
        self.db = db
        tag = uuid.uuid4().hex[:8]
        self.co = co or companies.create_company(db, f"Nightly-Co-{tag}")
        self.site = sites.create_site(db, self.co["id"], f"Nightly-Site-{tag}")
        self.folder = f"NightlyF-{tag}"
        self.user = users.upsert_field_only_user(db, self.co["id"], self.folder,
                                                 "Fol", "Der", "worker")

    def key(self, base, day=DAY):
        return f"extractions/{self.folder}/{day.isoformat()}/{base}.json"

    def topic(self, base, *, written=NOW - _dt.timedelta(hours=1), time_range="10:00 – 10:05",
              day=DAY, key=None):
        t = topics.upsert_topic(self.db, self.site["id"], day, f"Topic {base}",
                                user_id=self.user["id"],
                                source_s3_key=key or self.key(base, day),
                                summary="s", time_range=time_range)
        self.db.execute("UPDATE topics SET created_at = %s WHERE id = %s", (written, t["id"]))
        return t

    def item(self, kind, topic, text=None):
        table = event_links.KIND_TABLE[kind]
        row = self.db.execute(
            f"INSERT INTO {table} (topic_id, site_id, {TEXT_COL[kind]}) VALUES (%s,%s,%s) "
            f"RETURNING id, stable_id",
            (topic["id"], self.site["id"], text or f"synthetic {kind}")).fetchone()
        return {"id": row[0], "stable_id": row[1], "kind": kind}

    def link(self, item, method="lexical_v1", score=0.7):
        return event_links.insert_supported_by(
            self.db, site_id=self.site["id"], src_kind=item["kind"], src_row_id=item["id"],
            source_s3_key="k", dst_turn=_turn(), score=score, rule_version="v1",
            method=method)


def _needing(db, s, company_id=None, since=NOW - _dt.timedelta(days=7), limit=20):
    return nightly.sessions_needing_support(
        db, company_id=company_id or s.co["id"], site_id=s.site["id"], since_utc=since,
        limit=limit)


# ---------------------------------------------------------------- insert_supported_by

def test_insert_supported_by_writes_decision_record_id(db):
    s = Seed(db)
    it = s.item("finding", s.topic("sid" + "1" * 32))
    drid = uuid.uuid4()
    lid = event_links.insert_supported_by(
        db, site_id=s.site["id"], src_kind="finding", src_row_id=it["id"], source_s3_key="k",
        dst_turn=_turn(), score=None, rule_version="nightly_turn_pick_v1",
        method="nightly_turn_pick_v1", decision_record_id=drid)
    row = db.execute("SELECT decision_record_id, score, method FROM event_links WHERE id = %s",
                     (lid,)).fetchone()
    assert row == (drid, None, "nightly_turn_pick_v1")
    # Existing callers (no decision_record_id) still write NULL.
    lex = s.link(it)
    assert db.execute("SELECT decision_record_id FROM event_links WHERE id = %s",
                      (lex,)).fetchone()[0] is None


def test_insert_supported_by_skip_returns_none_on_conflict(db):
    s = Seed(db)
    it = s.item("action_item", s.topic("sid" + "2" * 32))
    kw = dict(site_id=s.site["id"], src_kind="action_item", src_row_id=it["id"],
              source_s3_key="k", dst_turn=_turn(), score=None,
              rule_version="nightly_turn_pick_v1", method="nightly_turn_pick_v1",
              on_conflict="skip")
    first = event_links.insert_supported_by(db, **kw)
    assert first is not None
    assert event_links.insert_supported_by(db, **kw) is None
    # The default still raises on the same conflict.
    kw.pop("on_conflict")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            event_links.insert_supported_by(db, **kw)
    with pytest.raises(ValueError):
        event_links.insert_supported_by(db, **{**kw, "on_conflict": "ignore"})


def test_insert_supported_by_site_mismatch_still_raises_with_skip(db):
    s = Seed(db)
    other = Seed(db)
    it = s.item("decision", s.topic("sid" + "3" * 32))
    with pytest.raises(ValueError, match="site"):
        event_links.insert_supported_by(
            db, site_id=other.site["id"], src_kind="decision", src_row_id=it["id"],
            source_s3_key="k", dst_turn=_turn(), score=None, rule_version="v",
            method="nightly_turn_pick_v1", on_conflict="skip")


# ---------------------------------------------------------------- sessions_needing_support

def test_sessions_needing_support_excludes_linked_and_superseded(db):
    s = Seed(db)
    base = "sid" + "4" * 32
    t = s.topic(base)
    bare = s.item("action_item", t)
    lexical = s.item("finding", t)
    s.link(lexical)                                         # linked by another method
    nightly_linked = s.item("question", t)
    s.link(nightly_linked, method="nightly_turn_pick_v1", score=None)
    old = s.topic("sid" + "5" * 32)
    s.item("action_item", old)
    topics.supersede_topics_for_source(db, s.key("sid" + "5" * 32), "final:x")
    # A session whose only item is linked is omitted entirely.
    done = s.topic("sid" + "6" * 32)
    s.link(s.item("decision", done))

    out = _needing(db, s)
    assert [r["extraction_key"] for r in out] == [s.key(base)]
    assert [(i["row_id"], i["kind"]) for i in out[0]["items"]] == [(str(bare["id"]),
                                                                    "action_item")]


def test_superseded_row_link_does_not_count_for_the_live_row(db):
    """A link on a superseded pass's row is history; the live carrier still needs one."""
    s = Seed(db)
    base = "sid" + "7" * 32
    t1 = s.topic(base)
    old = s.item("action_item", t1)
    s.link(old)
    topics.supersede_topics_for_source(db, s.key(base), "final:x")
    t2 = s.topic(base)
    live = db.execute("INSERT INTO action_items (topic_id, site_id, text, stable_id) "
                      "VALUES (%s,%s,'synthetic carried',%s) RETURNING id",
                      (t2["id"], s.site["id"], old["stable_id"])).fetchone()[0]
    out = _needing(db, s)
    assert [i["row_id"] for i in out[0]["items"]] == [str(live)]


def test_sessions_needing_support_shape_order_since_and_limit(db):
    s = Seed(db)
    older = s.topic("sid" + "8" * 32, written=NOW - _dt.timedelta(days=2),
                    time_range="09:00 – 09:10")
    a = s.item("action_item", older, "synthetic older task")
    newer_key = "sid" + "9" * 32
    n1 = s.topic(newer_key, written=NOW - _dt.timedelta(hours=2), time_range="11:00 – 11:05")
    n2 = s.topic(newer_key, written=NOW - _dt.timedelta(hours=2), time_range="11:05 – 11:09")
    b = s.item("finding", n1, "synthetic finding")
    c = s.item("decision", n2, "synthetic decision")
    too_old = s.topic("sid" + "0" * 32, written=NOW - _dt.timedelta(days=9))
    s.item("action_item", too_old)

    out = _needing(db, s)
    assert [r["extraction_key"] for r in out] == [s.key(newer_key), s.key("sid" + "8" * 32)]
    first = out[0]
    assert first["session_base"] == newer_key
    assert first["user_folder"] == s.folder
    assert first["date"] == DAY.isoformat()
    assert {k for k in first} == {"extraction_key", "session_base", "user_folder", "date",
                                  "items"}
    by_row = {i["row_id"]: i for i in first["items"]}
    assert set(by_row) == {str(b["id"]), str(c["id"])}
    ib, ic = by_row[str(b["id"])], by_row[str(c["id"])]
    assert {k for k in ib} == {"stable_id", "row_id", "kind", "text", "topic_index",
                               "time_range"}
    assert ib["stable_id"] == str(b["stable_id"]) and ib["text"] == "synthetic finding"
    assert ib["time_range"] == "11:00 – 11:05" and ic["time_range"] == "11:05 – 11:09"
    assert ib["topic_index"] != ic["topic_index"]
    assert out[1]["items"][0]["row_id"] == str(a["id"])

    assert [r["extraction_key"] for r in _needing(db, s, limit=1)] == [s.key(newer_key)]
    assert _needing(db, s, since=NOW - _dt.timedelta(hours=1)) == []


def test_sessions_needing_support_scoped_by_current_company(db):
    s = Seed(db)
    s.item("action_item", s.topic("sid" + "a1" * 16))
    assert len(_needing(db, s)) == 1
    new_owner = companies.create_company(db, f"Nightly-New-{uuid.uuid4().hex[:8]}")
    db.execute("UPDATE sites SET company_id = %s WHERE id = %s",
               (new_owner["id"], s.site["id"]))
    assert _needing(db, s) == []                                 # old company: nothing
    assert len(_needing(db, s, company_id=new_owner["id"])) == 1  # current owner sees it


def test_sessions_needing_support_is_site_scoped(db):
    s = Seed(db)
    other = Seed(db, co=s.co)
    other.item("action_item", other.topic("sid" + "b1" * 16))
    assert _needing(db, s) == []


def test_redacted_topic_items_excluded(db):
    s = Seed(db)
    keep = s.topic("sid" + "c1" * 16)
    kept = s.item("action_item", keep)
    for scope in ("analysis", "all", "deleted"):
        t = s.topic("sid" + uuid.uuid4().hex)
        s.item("action_item", t)
        db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope) "
                   "VALUES (%s, 'topic', %s, 'test', %s)", (s.co["id"], t["id"], scope))
    # A recording tombstone (source-prefix arm).
    rec = s.topic("sid" + "c2" * 16)
    s.item("finding", rec)
    redactions.create_recording_tombstone(db, s.co["id"], s.key("sid" + "c2" * 16), "test",
                                          None, "admin")
    # A reverted redaction hides nothing.
    back = s.topic("sid" + "c3" * 16)
    back_item = s.item("question", back)
    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope, "
               "reverted_at) VALUES (%s, 'topic', %s, 'test', 'all', now())",
               (s.co["id"], back["id"]))
    # A non_work (life-conversation) topic never goes to a model either.
    nw = s.topic("sid" + "c4" * 16)
    s.item("action_item", nw)
    db.execute("UPDATE topics SET work_class = 'non_work' WHERE id = %s", (nw["id"],))

    rows = {i["row_id"] for r in _needing(db, s) for i in r["items"]}
    assert rows == {str(kept["id"]), str(back_item["id"])}


def test_non_extraction_topics_are_ignored(db):
    s = Seed(db)
    t = s.topic("x", key=None)
    db.execute("UPDATE topics SET source_s3_key = NULL WHERE id = %s", (t["id"],))
    s.item("action_item", t)
    agent = s.topic("y", key=f"action_packs/{uuid.uuid4()}/pack.json")
    s.item("action_item", agent)
    assert _needing(db, s) == []


# ---------------------------------------------------------------- budget count

def test_nightly_support_count_today(db):
    s = Seed(db)
    other = Seed(db)
    today = _dt.date(2026, 11, 10)

    def rec(company, created_at, kind="nightly_support"):
        r = decision_records.insert(
            db, company_id=company["id"], site_id=None, kind=kind, subject_type="finding",
            subject_stable_id=uuid.uuid4(), provider="test", output={"turn": None},
            auto_outcome="abstained")
        db.execute("UPDATE decision_records SET created_at = %s WHERE id = %s",
                   (created_at, r["id"]))

    # 2026-11-10 NZDT (UTC+13) runs from 2026-11-09T11:00Z to 2026-11-10T11:00Z.
    rec(s.co, _dt.datetime(2026, 11, 9, 11, 0, tzinfo=UTC))       # first NZ instant: in
    rec(s.co, _dt.datetime(2026, 11, 10, 10, 59, tzinfo=UTC))     # last NZ minute: in
    rec(s.co, _dt.datetime(2026, 11, 9, 10, 59, tzinfo=UTC))      # NZ yesterday: out
    rec(s.co, _dt.datetime(2026, 11, 10, 11, 0, tzinfo=UTC))      # NZ tomorrow: out
    rec(s.co, _dt.datetime(2026, 11, 10, 1, 0, tzinfo=UTC), kind="programme_match")
    rec(other.co, _dt.datetime(2026, 11, 10, 1, 0, tzinfo=UTC))
    assert nightly.nightly_support_count_today(db, s.co["id"], today) == 2


# ---------------------------------------------------------------- record_reemit

def test_record_reemit_unique_per_pair(db):
    s = Seed(db)
    kw = dict(site_id=s.site["id"], line="xday", n_key=s.key("n"), l_key=s.key("l"),
              artifact_key="item_continuity_xday_requests/x.json", night=DAY)
    assert nightly.record_reemit(db, **kw) is True
    assert nightly.record_reemit(db, **{**kw, "night": DAY + _dt.timedelta(days=1)}) is False
    assert nightly.record_reemit(db, **{**kw, "line": "reconcile"}) is True
    dropped = {**kw, "l_key": None}
    assert nightly.record_reemit(db, **dropped) is True
    assert nightly.record_reemit(db, **dropped) is False
    assert nightly.record_reemit(db, **{**dropped, "night": DAY + _dt.timedelta(days=1)}) is True


# ---------------------------------------------------------------- recompute markers

def test_failed_recomputes_mark_and_resolve(db):
    s = Seed(db)
    other = Seed(db)
    nightly.mark_recompute_failed(db, s.co["id"], "sidA", "timed_out")
    nightly.mark_recompute_failed(db, s.co["id"], "sidB", "failed")
    nightly.mark_recompute_failed(db, other.co["id"], "sidC", "failed")
    got = nightly.failed_recomputes(db, company_id=s.co["id"])
    assert sorted((r["session_base"], r["reason"]) for r in got) == [
        ("sidA", "timed_out"), ("sidB", "failed")]
    assert nightly.failed_recomputes(db, company_id=s.co["id"], limit=1)[0]["session_base"] \
        in {"sidA", "sidB"}
    nightly.resolve_recompute(db, s.co["id"], "sidA")
    assert [r["session_base"] for r in nightly.failed_recomputes(db, company_id=s.co["id"])] \
        == ["sidB"]
    # A new failure reopens the resolved marker with the new reason.
    nightly.mark_recompute_failed(db, s.co["id"], "sidA", "failed")
    reopened = {r["session_base"]: r["reason"]
                for r in nightly.failed_recomputes(db, company_id=s.co["id"])}
    assert reopened == {"sidA": "failed", "sidB": "failed"}
    with pytest.raises(ValueError):
        nightly.mark_recompute_failed(db, s.co["id"], "sidD", "boom")


@pytest.fixture
def committed(migrated_db_url):
    seed = get_connection(migrated_db_url, autocommit=True)
    made = []

    def company():
        co = companies.create_company(seed, f"Nightly-Commit-{uuid.uuid4().hex[:8]}")
        made.append(co["id"])
        return co

    try:
        yield migrated_db_url, seed, company
    finally:
        for co_id in made:
            seed.execute("DELETE FROM claim_recompute_failures WHERE company_id = %s", (co_id,))
            seed.execute("DELETE FROM companies WHERE id = %s", (co_id,))
        seed.close()


def test_claim_one_recompute_skip_locked(committed):
    url, seed, company = committed
    co = company()
    nightly.mark_recompute_failed(seed, co["id"], "sidX", "timed_out")
    a = psycopg.connect(url)
    b = psycopg.connect(url)
    # A SKIP LOCKED regression would make the second claimer wait on the row lock: fail fast.
    a.execute("SET lock_timeout = '2s'")
    b.execute("SET lock_timeout = '2s'")
    a.commit()
    b.commit()
    try:
        got = nightly.claim_one_recompute(a, co["id"])
        assert got["session_base"] == "sidX" and got["reason"] == "timed_out"
        assert nightly.claim_one_recompute(b, co["id"]) is None      # skipped, not blocked
        nightly.resolve_recompute(a, co["id"], "sidX")
        a.commit()
        assert nightly.claim_one_recompute(b, co["id"]) is None      # resolved: gone
        b.rollback()
        # The exclusion list lets one transaction move past a marker it could not resolve.
        nightly.mark_recompute_failed(seed, co["id"], "sidY", "failed")
        nightly.mark_recompute_failed(seed, co["id"], "sidZ", "failed")
        first = nightly.claim_one_recompute(a, co["id"])
        second = nightly.claim_one_recompute(a, co["id"], exclude=[first["session_base"]])
        assert {first["session_base"], second["session_base"]} == {"sidY", "sidZ"}
        assert nightly.claim_one_recompute(
            a, co["id"], exclude=["sidY", "sidZ"]) is None
        a.rollback()
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------- voiceprint writer marker

def _markers(seed, co_id):
    return seed.execute("SELECT session_base, reason, resolved_at FROM claim_recompute_failures "
                        "WHERE company_id = %s ORDER BY session_base", (co_id,)).fetchall()


@pytest.fixture
def writer(committed, monkeypatch):
    url, seed, company = committed
    monkeypatch.setattr(vw, "get_connection", lambda *a, **k: get_connection(url))
    return seed, company


def test_voiceprint_writer_marks_timed_out_recompute(writer, monkeypatch):
    seed, company = writer
    co = company()
    seen = []

    def timed_out(conn, **kw):
        seen.append(kw["session_base"])
        return {"rows": 0, "sites": 1, "failed": 0, "errors": [], "timed_out": True,
                "changed": 0}
    monkeypatch.setattr(vw.claim_type_store, "recompute_session", timed_out)
    base = "sid" + "d" * 32
    out = vw._recompute_claims(co["id"], base + "-r2")     # a reopen round normalises
    assert out["timed_out"] is True
    assert seen == [base]
    assert _markers(seed, co["id"]) == [(base, "timed_out", None)]


def test_voiceprint_writer_marks_failed_sites(writer, monkeypatch):
    seed, company = writer
    co = company()
    monkeypatch.setattr(vw.claim_type_store, "recompute_session",
                        lambda conn, **kw: {"rows": 1, "sites": 1, "failed": 1,
                                            "errors": ["UndefinedColumn"],
                                            "timed_out": False, "changed": 0})
    vw._recompute_claims(co["id"], "s-failed")
    assert _markers(seed, co["id"]) == [("s-failed", "failed", None)]


def test_voiceprint_writer_marks_on_exception(writer, monkeypatch, caplog):
    seed, company = writer
    co = company()

    def boom(conn, **kw):
        conn.execute("SELECT 1/0")                      # leaves this connection aborted
    monkeypatch.setattr(vw.claim_type_store, "recompute_session", boom)
    with caplog.at_level("WARNING"):
        assert vw._recompute_claims(co["id"], "s-raised") is None
    assert "claim recompute failed for s-raised: DivisionByZero" in caplog.text
    assert _markers(seed, co["id"]) == [("s-raised", "failed", None)]


def test_voiceprint_writer_success_writes_no_marker(writer, monkeypatch):
    seed, company = writer
    co = company()
    monkeypatch.setattr(vw.claim_type_store, "recompute_session",
                        lambda conn, **kw: {"rows": 2, "sites": 1, "failed": 0, "errors": [],
                                            "timed_out": False, "changed": 1})
    vw._recompute_claims(co["id"], "s-ok")
    assert _markers(seed, co["id"]) == []


def test_voiceprint_writer_marker_failure_does_not_raise(writer, monkeypatch):
    """A company the marker cannot reference (FK) logs and returns the counts unchanged."""
    _seed, _company = writer
    monkeypatch.setattr(vw.claim_type_store, "recompute_session",
                        lambda conn, **kw: {"rows": 0, "sites": 0, "failed": 0, "errors": [],
                                            "timed_out": True, "changed": 0})
    out = vw._recompute_claims(str(uuid.uuid4()), "s-orphan")
    assert out["timed_out"] is True
