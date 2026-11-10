"""Integration: the nightly J2 support writer (lambda_nightly_support_writer) on real Postgres.

Every SQL path runs here -- a fake connection never executes SQL. The writer runs on the
`db` fixture's connection, so its per-row `conn.transaction()` blocks are savepoints and the
fixture rolls everything back. No decision_ledger row is written. All text is synthetic.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as _dt
import uuid

import pytest

import model_registry
import nightly_turn_pick as ntp
from repositories import companies, event_links, event_runs, sites, topics, users

pytestmark = pytest.mark.integration

writer = pytest.importorskip("lambda_nightly_support_writer")

DAY = _dt.date(2026, 11, 9)
TEXT_COL = {"action_item": "text", "finding": "observation", "decision": "decision",
            "question": "question"}
MODEL = model_registry.get("nightly_turn_pick")["model"]


def _locator(start=12.0):
    return {"source_stem": "stem-a", "start_sec": start, "end_sec": start + 3.0,
            "speaker_label": "spk_0", "quote_status": None}


class Seed:
    def __init__(self, db, co=None, user_co=None):
        self.db = db
        tag = uuid.uuid4().hex[:8]
        self.co = co or companies.create_company(db, f"NightlyW-Co-{tag}")
        self.site = sites.create_site(db, self.co["id"], f"NightlyW-Site-{tag}")
        self.folder = f"NightlyWF-{tag}"
        home = user_co or self.co
        self.user = users.upsert_field_only_user(db, home["id"], self.folder,
                                                 "Fol", "Der", "worker")
        self.base = "sid" + uuid.uuid4().hex
        self.key = f"extractions/{self.folder}/{DAY.isoformat()}/{self.base}.json"
        self.run = event_runs.start(db, self.co["id"], self.site["id"], [], 1,
                                    "nightly_rematch")
        self.topic = topics.upsert_topic(db, self.site["id"], DAY, "Topic", user_id=self.user["id"],
                                         source_s3_key=self.key, summary="s",
                                         time_range="10:00 – 10:05")

    def item(self, kind, topic=None):
        table = event_links.KIND_TABLE[kind]
        row = self.db.execute(
            f"INSERT INTO {table} (topic_id, site_id, {TEXT_COL[kind]}) VALUES (%s,%s,%s) "
            f"RETURNING id::text, stable_id::text",
            ((topic or self.topic)["id"], self.site["id"], f"synthetic {kind}")).fetchone()
        return {"row_id": row[0], "stable_id": row[1], "kind": kind}

    def row(self, it, *, outcome="accepted", turn="T2", guard=None, cross=False, start=12.0):
        return {"alias": "I1", "stable_id": it["stable_id"], "row_id": it["row_id"],
                "kind": it["kind"], "turn": turn if outcome != "abstained" else None,
                "outcome": outcome, "guard": guard, "cross_language": cross,
                "locator": _locator(start) if outcome != "abstained" else None}

    def event(self, rows, *, run_id=None, site_id=None):
        return {"site_id": str(site_id or self.site["id"]),
                "run_id": str(run_id or self.run["run_id"]),
                "extraction_key": self.key, "session_base": self.base,
                "question_set": ntp.PROMPT_HASH,
                "input_key": f"nightly_requests/{self.site['id']}/{DAY}/abc.json",
                "rows": rows,
                "usage": {"tokens_in": 1200, "tokens_out": 300, "model": MODEL,
                          "cost_usd": None}}


def _records(db, s):
    return db.execute(
        "SELECT id, company_id::text, site_id::text, kind, subject_type, "
        "subject_stable_id::text, object_ref, provider, model, question_set, input_key, "
        "output, auto_outcome FROM decision_records WHERE input_key = %s "
        "ORDER BY subject_type, created_at", (s.event([])["input_key"],)).fetchall()


def _links(db, it):
    col = event_links.KIND_SRC_COLUMN[it["kind"]]
    return db.execute(
        f"SELECT id, method, score, rule_version, decision_record_id, source_s3_key, dst_turn "
        f"FROM event_links WHERE {col} = %s AND link_type = 'supported_by' ORDER BY created_at",
        (it["row_id"],)).fetchall()


def test_writer_writes_record_per_row_and_link_per_accept(db):
    s = Seed(db)
    acc, rej, abst = s.item("action_item"), s.item("finding"), s.item("decision")
    out = writer.write_support(db, s.event([
        s.row(acc), s.row(rej, outcome="rejected", guard="twin"),
        s.row(abst, outcome="abstained")]))
    assert out["written"] == 1 and out["records"] == 3
    assert out["skipped"] == {"gone": 0, "linked": 0, "moved_site": 0}

    recs = {r[5]: r for r in _records(db, s)}
    assert set(recs) == {acc["stable_id"], rej["stable_id"], abst["stable_id"]}
    a = recs[acc["stable_id"]]
    assert a[1:5] == (str(s.co["id"]), str(s.site["id"]), "nightly_support", "action_item")
    assert a[6] == "stem-a@12.0"
    assert a[7:11] == ("anthropic", MODEL, ntp.PROMPT_HASH, s.event([])["input_key"])
    assert a[11] == {"turn": "T2", "guard": None} and a[12] == "accepted"
    assert recs[rej["stable_id"]][11] == {"turn": "T2", "guard": "twin"}
    assert recs[rej["stable_id"]][12] == "rejected"
    assert recs[abst["stable_id"]][6] is None
    assert recs[abst["stable_id"]][11] == {"turn": None, "guard": None}
    assert recs[abst["stable_id"]][12] == "abstained"

    links = _links(db, acc)
    assert len(links) == 1
    lid, method, score, rv, drid, key, dst = links[0]
    assert (method, score, rv, drid, key) == (ntp.METHOD, None, ntp.RULE_VERSION, a[0], s.key)
    assert dst == _locator()
    assert _links(db, rej) == [] and _links(db, abst) == []

    run = db.execute("SELECT tokens_in, tokens_out, cost_usd, model_calls, models "
                     "FROM event_runs WHERE run_id = %s", (s.run["run_id"],)).fetchone()
    assert run == (1200, 300, None, 1, [MODEL])


def test_writer_skips_superseded_row(db):
    s = Seed(db)
    it = s.item("action_item")
    topics.supersede_topics_for_source(db, s.key, "final:x")
    out = writer.write_support(db, s.event([s.row(it)]))
    assert out["written"] == 0 and out["records"] == 0
    assert out["skipped"]["gone"] == 1
    assert _links(db, it) == [] and _records(db, s) == []


def test_writer_never_touches_lexical_link(db):
    s = Seed(db)
    it = s.item("finding")
    lex = event_links.insert_supported_by(
        db, site_id=s.site["id"], src_kind="finding", src_row_id=it["row_id"],
        source_s3_key=s.key, dst_turn=_locator(1.0), score=0.7, rule_version="v1",
        method="lexical_v1")
    before = _links(db, it)
    out = writer.write_support(db, s.event([s.row(it)]))
    assert out["written"] == 0 and out["skipped"]["linked"] == 1
    assert _links(db, it) == before and before[0][0] == lex
    rec = _records(db, s)
    assert len(rec) == 1 and rec[0][11] == {"turn": "T2", "guard": "already_linked"}
    assert rec[0][12] == "rejected"


def test_writer_site_mismatch_skipped(db):
    s = Seed(db)
    other = Seed(db, co=s.co)
    it = other.item("question")                    # the row lives on the other site
    out = writer.write_support(db, s.event([s.row(it)]))
    assert out["written"] == 0 and out["records"] == 0
    assert out["skipped"]["moved_site"] == 1
    assert _links(db, it) == [] and _records(db, s) == []


def test_writer_rejects_cross_language_when_disallowed(db):
    assert model_registry.get("nightly_turn_pick")["allow_cross_language"] is False
    s = Seed(db)
    it = s.item("action_item")
    out = writer.write_support(db, s.event([s.row(it, cross=True)]))
    assert out["written"] == 0 and out["records"] == 1
    rec = _records(db, s)[0]
    assert rec[11] == {"turn": "T2", "guard": "cross_language_off"} and rec[12] == "rejected"
    assert _links(db, it) == []


def test_writer_record_company_is_site_company(db):
    tag = uuid.uuid4().hex[:8]
    co_a = companies.create_company(db, f"NightlyW-A-{tag}")
    co_b = companies.create_company(db, f"NightlyW-B-{tag}")
    s = Seed(db, co=co_b, user_co=co_a)             # site owned by B, recorder in A
    it = s.item("decision")
    out = writer.write_support(db, s.event([s.row(it)]))
    assert out["written"] == 1
    assert _records(db, s)[0][1] == str(co_b["id"])


def test_writer_recomputes_claim_type_when_written(db, monkeypatch):
    s = Seed(db)
    it = s.item("action_item")
    out = writer.write_support(db, s.event([s.row(it)]))
    assert out["written"] == 1
    link_id = _links(db, it)[0][0]
    basis = db.execute("SELECT claim_basis FROM action_items WHERE id = %s",
                       (it["row_id"],)).fetchone()[0]
    assert basis is not None and basis.get("link_id") == str(link_id)

    # Nothing written -> no recompute.
    calls = []
    monkeypatch.setattr(writer.claim_type_store, "recompute_session",
                        lambda *a, **k: calls.append(k))
    s2 = Seed(db)
    it2 = s2.item("finding")
    writer.write_support(db, s2.event([s2.row(it2, outcome="rejected", guard="window")]))
    assert calls == []


def test_writer_fails_closed_on_foreign_run(db):
    s = Seed(db)
    other = Seed(db)
    it = s.item("action_item")
    with pytest.raises(RuntimeError, match="run"):
        writer.write_support(db, s.event([s.row(it)], run_id=other.run["run_id"]))
    assert _links(db, it) == [] and _records(db, s) == []


def test_writer_one_bad_row_does_not_lose_the_others(db):
    s = Seed(db)
    good = s.item("action_item")
    bad = dict(s.row(s.item("finding")), kind="not_a_kind")
    out = writer.write_support(db, s.event([bad, s.row(good)]))
    assert out["written"] == 1 and out["errors"] == 1
    assert len(_links(db, good)) == 1


def test_writer_lost_race_corrects_its_own_record(db, monkeypatch):
    """insert_supported_by skipped on the nightly unique index (a concurrent nightly link):
    the record written in the same transaction is corrected to already_linked."""
    s = Seed(db)
    it = s.item("action_item")
    monkeypatch.setattr(writer.event_links, "insert_supported_by", lambda *a, **k: None)
    out = writer.write_support(db, s.event([s.row(it)]))
    assert out["written"] == 0 and out["skipped"]["linked"] == 1 and out["records"] == 1
    rec = _records(db, s)[0]
    assert rec[11] == {"turn": "T2", "guard": "already_linked"} and rec[12] == "rejected"


def test_writer_moved_site_at_insert_rolls_the_row_back(db, monkeypatch):
    s = Seed(db)
    it, other = s.item("action_item"), s.item("finding")

    real = event_links.insert_supported_by

    def flaky(conn, **kw):
        if kw["src_row_id"] == it["row_id"]:
            raise ValueError("site_id is not the row's site")
        return real(conn, **kw)

    monkeypatch.setattr(writer.event_links, "insert_supported_by", flaky)
    out = writer.write_support(db, s.event([s.row(it), s.row(other)]))
    assert out["skipped"]["moved_site"] == 1 and out["written"] == 1 and out["records"] == 1
    assert [r[5] for r in _records(db, s)] == [other["stable_id"]]
