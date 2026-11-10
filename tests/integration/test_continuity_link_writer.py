"""Integration: hop 3 of cross-day continuity -- the in-VPC link writer (spec 2026-11-09 X8, X9).

`write_links(conn, event)` runs on the rolled-back `db` fixture; one test drives
`lambda_handler` against a real committed connection (get_connection monkeypatched, the
pattern of test_decision_records_roundtrip.py) to prove the one-transaction commit.
"""
import datetime as _dt
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, sites, topics

import continuity_xday

pytestmark = pytest.mark.integration

lw = pytest.importorskip("lambda_continuity_link_writer", reason="requires psycopg")

KEY = "extractions/Sam_Site/2026-07-03/Sam_Site_2026-07-03_09-00-00.json"
RUN_HASH = "0123456789abcdef"


def _seed(db, *, dst_date=_dt.date(2026, 7, 2), tag=None):
    tag = tag or uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"Acme-{tag}")
    s = sites.create_site(db, co["id"], f"S1-{tag}")
    earlier = topics.upsert_topic(
        db, s["id"], dst_date, "Concrete pour B2",
        source_s3_key=f"extractions/Sam_Site/{dst_date}/Sam_Site_{dst_date}_09-00-00.json",
        action_items=[{"text": "Order rebar", "item_id": str(uuid.uuid4())}])
    today = topics.upsert_topic(
        db, s["id"], _dt.date(2026, 7, 3), "Concrete pour B2", source_s3_key=KEY,
        action_items=[{"text": "Chase the rebar order", "item_id": str(uuid.uuid4())}])
    dst = db.execute("SELECT id, stable_id::text, item_id::text FROM action_items "
                     "WHERE topic_id=%s", (earlier["id"],)).fetchone()
    src = db.execute("SELECT id, stable_id::text, item_id::text FROM action_items "
                     "WHERE topic_id=%s", (today["id"],)).fetchone()
    return {"company": co, "site": s, "earlier": earlier, "today": today,
            "dst_row": dst[0], "dst": dst[1], "dst_item": dst[2],
            "src_row": src[0], "src": src[1], "src_item": src[2]}


def _event(seed, *, src=None, src_item="same", dst=None, outcome="accepted", guard=None,
           run_hash=RUN_HASH, link=True):
    src = src or seed["src"]
    dst = dst or seed["dst"]
    src_item = seed["src_item"] if src_item == "same" else src_item
    request = {
        "rule_version": continuity_xday.RULE_VERSION,
        "question_set": continuity_xday.XDAY_QUESTION_SET,
        "site_id": str(seed["site"]["id"]), "company_id": str(seed["company"]["id"]),
        "extraction_key": KEY, "extracted_at": "2026-07-03T10:00:00+00:00",
        "date": "2026-07-03", "run_hash": run_hash, "trigger": "final",
        "new_items": [{"ref": "N1", "kind": "action_item", "stable_id": src,
                       "item_id": src_item}],
        "candidates": [{"alias": "A1", "kind": "action_item", "stable_id": dst,
                        "item_id": seed["dst_item"], "first_date": "2026-07-02",
                        "first_seen_recorded_at": None}],
        "precap_counts": {"action_items": 1, "questions": 0, "findings": 0},
    }
    claims = [{"src_stable_id": src, "dst_stable_id": dst, "alias": "A1",
               "outcome": outcome, "guard": guard}]
    links = ([{"src_stable_id": src, "src_item_id": src_item, "src_kind": "action_item",
               "dst_stable_id": dst}] if link and outcome == "accepted" else [])
    return {"request": request, "links": links, "claims": claims,
            "model": {"provider": "openrouter", "model": "muse-spark"}}


def _links(db, site_id):
    return db.execute(
        "SELECT src_stable_id::text, dst_stable_id::text, src_kind, dst_kind, "
        "src_action_item_id, source_s3_key, method, rule_version, decision_record_id "
        "FROM event_links WHERE site_id=%s AND link_type='continues'", (site_id,)).fetchall()


def _records(db, kind):
    return db.execute(
        "SELECT id, subject_type, subject_stable_id::text, object_ref, question_set, "
        "input_key, input_hash, output, auto_outcome, provider, model, company_id, site_id "
        "FROM decision_records WHERE kind=%s AND input_key=%s ORDER BY created_at, id",
        (kind, KEY)).fetchall()


# --- the valid link -------------------------------------------------------------------------------

def test_a_valid_link_inserts_one_row_and_one_record_it_points_at(db):
    seed = _seed(db)
    out = lw.write_links(db, _event(seed))
    assert out["inserted"] == 1
    assert out["skipped"] == {}

    rows = _links(db, seed["site"]["id"])
    assert len(rows) == 1
    src, dst, src_kind, dst_kind, fk, source_key, method, rule, record_id = rows[0]
    assert (src, dst, src_kind, dst_kind) == (seed["src"], seed["dst"], "action_item",
                                              "action_item")
    assert fk == seed["src_row"]
    assert source_key == KEY
    assert method == "llm_echo"
    assert rule == continuity_xday.RULE_VERSION

    recs = _records(db, continuity_xday.DECISION_KIND)
    assert len(recs) == 1
    rid, stype, subject, obj, qset, ikey, ihash, output, auto, provider, model, co, site = recs[0]
    assert record_id == rid
    assert (stype, subject, obj) == ("action_item", seed["src"], seed["dst"])
    assert (qset, ikey, ihash) == (continuity_xday.XDAY_QUESTION_SET, KEY, RUN_HASH)
    assert output == {"src_stable_id": seed["src"], "dst_stable_id": seed["dst"],
                      "outcome": "accepted", "guard": None}
    assert auto == "accepted"
    assert (provider, model) == ("openrouter", "muse-spark")
    assert (co, site) == (seed["company"]["id"], seed["site"]["id"])


def test_redelivery_keeps_one_row_and_one_record(db):
    seed = _seed(db)
    first = lw.write_links(db, _event(seed))
    second = lw.write_links(db, _event(seed))
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["skipped"] == {"duplicate": 1}
    assert second["records"] == 0
    assert len(_links(db, seed["site"]["id"])) == 1
    assert len(_records(db, continuity_xday.DECISION_KIND)) == 1
    assert len(_records(db, "item_continuity_xday_run")) == 1


# --- src resolution -------------------------------------------------------------------------------

def test_a_recarried_src_resolves_through_its_item_id(db):
    seed = _seed(db)
    # the request's stable_id no longer names a live row; the row (same item_id) has a new one
    moved = db.execute("UPDATE action_items SET stable_id=gen_random_uuid() WHERE id=%s "
                       "RETURNING stable_id::text", (seed["src_row"],)).fetchone()[0]
    out = lw.write_links(db, _event(seed))
    assert out["inserted"] == 1, out
    rows = _links(db, seed["site"]["id"])
    assert len(rows) == 1
    assert rows[0][0] == moved          # the row's CURRENT stable_id
    assert rows[0][4] == seed["src_row"]
    # the record keeps hop 2's src_stable_id in output (the re-delivery dedupe key)
    recs = _records(db, continuity_xday.DECISION_KIND)
    assert recs[0][7]["src_stable_id"] == seed["src"]


def test_a_gone_src_without_an_item_id_is_skipped(db):
    seed = _seed(db)
    db.execute("UPDATE action_items SET stable_id=gen_random_uuid() WHERE id=%s",
               (seed["src_row"],))
    out = lw.write_links(db, _event(seed, src_item=None))
    assert out["inserted"] == 0
    assert out["skipped"] == {"src_gone": 1}
    assert _links(db, seed["site"]["id"]) == []
    # the claim is still recorded
    assert len(_records(db, continuity_xday.DECISION_KIND)) == 1


def test_a_src_from_another_session_is_not_linked(db):
    seed = _seed(db)
    # the request names the EARLIER session's item as its src: not under the extraction key
    out = lw.write_links(db, _event(seed, src=seed["dst"], src_item=seed["dst_item"],
                                    dst=seed["src"]))
    assert out["inserted"] == 0
    assert out["skipped"] == {"src_gone": 1}


# --- dst checks -----------------------------------------------------------------------------------

def test_a_dst_not_live_on_the_site_is_skipped(db):
    seed = _seed(db)
    db.execute("UPDATE topics SET superseded_at=now() WHERE id=%s", (seed["earlier"]["id"],))
    out = lw.write_links(db, _event(seed))
    assert out["skipped"] == {"dst_gone": 1}
    assert _links(db, seed["site"]["id"]) == []


def test_a_dst_on_another_site_is_skipped(db):
    seed = _seed(db)
    other = _seed(db)
    out = lw.write_links(db, _event(seed, dst=other["dst"]))
    assert out["skipped"] == {"dst_gone": 1}


@pytest.mark.parametrize("dst_date", [_dt.date(2026, 7, 3), _dt.date(2026, 7, 4)])
def test_a_dst_not_recorded_earlier_is_skipped(db, dst_date):
    seed = _seed(db, dst_date=dst_date)
    out = lw.write_links(db, _event(seed))
    assert out["skipped"] == {"time_order": 1}
    assert _links(db, seed["site"]["id"]) == []


def test_the_dst_order_comes_from_the_db_not_the_request(db):
    # the request claims the candidate was first seen 07-02; the DB says 07-05
    seed = _seed(db, dst_date=_dt.date(2026, 7, 5))
    ev = _event(seed)
    assert ev["request"]["candidates"][0]["first_date"] == "2026-07-02"
    assert lw.write_links(db, ev)["skipped"] == {"time_order": 1}


# --- records --------------------------------------------------------------------------------------

def test_every_invocation_writes_one_run_record_deduped_on_key_and_hash(db):
    seed = _seed(db)
    lw.write_links(db, _event(seed))
    lw.write_links(db, _event(seed))
    runs = _records(db, "item_continuity_xday_run")
    assert len(runs) == 1
    out = runs[0][7]
    assert out["claims"] == 1
    assert runs[0][8] == "processed"
    assert runs[0][6] == RUN_HASH
    # a deliberate re-emit (new run_hash) is its own run
    lw.write_links(db, _event(seed, run_hash="fedcba9876543210"))
    assert len(_records(db, "item_continuity_xday_run")) == 2


def test_a_run_with_no_claims_still_writes_its_run_record(db):
    seed = _seed(db)
    ev = _event(seed)
    ev["claims"], ev["links"] = [], []
    out = lw.write_links(db, ev)
    assert out == {"inserted": 0, "skipped": {}, "records": 1}
    runs = _records(db, "item_continuity_xday_run")
    assert len(runs) == 1 and runs[0][7]["claims"] == 0


@pytest.mark.parametrize("guard", ["echo_mismatch", "echo_ambiguous", "one_to_one", "fuse",
                                   "existence", "self"])
def test_a_rejected_claim_writes_a_record_and_no_link(db, guard):
    seed = _seed(db)
    out = lw.write_links(db, _event(seed, outcome="rejected", guard=guard))
    assert out["inserted"] == 0
    assert _links(db, seed["site"]["id"]) == []
    recs = _records(db, continuity_xday.DECISION_KIND)
    assert len(recs) == 1
    assert recs[0][8] == "rejected"
    assert recs[0][7] == {"src_stable_id": seed["src"], "dst_stable_id": seed["dst"],
                          "outcome": "rejected", "guard": guard}


def test_a_rejected_claim_with_no_dst_is_recorded_and_deduped(db):
    seed = _seed(db)
    ev = _event(seed, outcome="rejected", guard="existence")
    ev["claims"][0]["dst_stable_id"] = None
    lw.write_links(db, ev)
    lw.write_links(db, ev)
    recs = _records(db, continuity_xday.DECISION_KIND)
    assert len(recs) == 1
    assert recs[0][3] is None


def test_records_and_logs_carry_no_text(db, caplog):
    seed = _seed(db)
    ev = _event(seed)
    caplog.set_level("INFO")
    lw.write_links(db, ev)
    for r in _records(db, continuity_xday.DECISION_KIND) + _records(
            db, "item_continuity_xday_run"):
        assert "rebar" not in str(r[7]).lower()
    assert "rebar" not in caplog.text.lower()


# --- deletion -------------------------------------------------------------------------------------

def test_deleting_the_src_session_removes_the_link(db):
    seed = _seed(db)
    lw.write_links(db, _event(seed))
    assert len(_links(db, seed["site"]["id"])) == 1
    topics.delete_topics_for_source(db, KEY)
    assert _links(db, seed["site"]["id"]) == []


# --- the handler: one committed transaction -------------------------------------------------------

def test_the_handler_commits_one_transaction(monkeypatch, migrated_db_url):
    seed_conn = get_connection(migrated_db_url, autocommit=True)
    seed = None
    try:
        seed = _seed(seed_conn)
        monkeypatch.setattr(lw, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        out = lw.lambda_handler(_event(seed), None)
        assert out == {"inserted": 1, "skipped": {}, "records": 2}
        assert len(_links(seed_conn, seed["site"]["id"])) == 1
    finally:
        if seed is not None:
            seed_conn.execute("DELETE FROM decision_records WHERE site_id=%s",
                              (seed["site"]["id"],))
            seed_conn.execute("DELETE FROM sites WHERE id=%s", (seed["site"]["id"],))
            seed_conn.execute("DELETE FROM companies WHERE id=%s", (seed["company"]["id"],))
        seed_conn.close()


def test_the_handler_rolls_back_everything_on_an_error(monkeypatch, migrated_db_url):
    seed_conn = get_connection(migrated_db_url, autocommit=True)
    seed = None
    try:
        seed = _seed(seed_conn)
        monkeypatch.setattr(lw, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))

        def boom(*a, **k):
            raise RuntimeError("after the records")
        monkeypatch.setattr(lw.event_links, "insert_continues", boom)
        with pytest.raises(RuntimeError):
            lw.lambda_handler(_event(seed), None)
        n = seed_conn.execute("SELECT count(*) FROM decision_records WHERE site_id=%s",
                              (seed["site"]["id"],)).fetchone()[0]
        assert n == 0
    finally:
        if seed is not None:
            seed_conn.execute("DELETE FROM decision_records WHERE site_id=%s",
                              (seed["site"]["id"],))
            seed_conn.execute("DELETE FROM sites WHERE id=%s", (seed["site"]["id"],))
            seed_conn.execute("DELETE FROM companies WHERE id=%s", (seed["company"]["id"],))
        seed_conn.close()


def test_a_request_with_no_site_raises(db):
    seed = _seed(db)
    ev = _event(seed)
    del ev["request"]["site_id"]
    with pytest.raises(ValueError):
        lw.write_links(db, ev)
