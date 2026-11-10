"""Integration: the site decision loop repositories against real Postgres (migration 0099).

site_events, event_runs, decision_cards and decision_ledger. Every SQL path runs here -- a fake
connection never executes SQL. The SKIP LOCKED test commits its seed rows (the `db` fixture
rolls back, so a second connection could not see them) and deletes them in `finally`.

decision_ledger rows are written ONLY through the rollback `db` fixture: the ledger trigger
rejects DELETE/TRUNCATE, so a committed ledger row could never be cleaned up.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as dt
import uuid

import psycopg
import pytest

from repositories import companies, sites, users
from repositories import decision_cards as dc
from repositories import decision_ledger as dl
from repositories import event_runs as er
from repositories import site_events as se

pytestmark = pytest.mark.integration

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 10, 1, 0, tzinfo=UTC)


# ---------------------------------------------------------------- helpers

def _world(conn, tag="a"):
    co = companies.create_company(conn, f"Co-dl-{tag}-{uuid.uuid4().hex[:8]}")
    site = sites.create_site(conn, co["id"], f"Site-dl-{tag}-{uuid.uuid4().hex[:6]}")
    return co, site


def _user(conn, co, role="admin"):
    tag = uuid.uuid4().hex[:8]
    return users.upsert_user(conn, f"sub-dl-{tag}", f"dl-{tag}@example.com",
                             company_id=co["id"], global_role=role)


def _ev(co, site, **over):
    ev = {
        "company_id": co["id"], "site_id": site["id"],
        "source": "checklist", "type": "checklist_gap",
        "subject_ref": f"report:{uuid.uuid4()}:item:3",
        "bucket": "2026-11-10",
        "occurred_at": dt.datetime(2026, 11, 9, 11, 0, tzinfo=UTC),
        "received_at": NOW,
        "summary": "Item 3 missing", "link": "/reports/x", "payload": {"item": 3},
        "dedupe_key": f"gap:{uuid.uuid4()}",
        "status": "queued",
    }
    ev.update(over)
    return ev


def _card(co, site, **over):
    card = {
        "card_key": f"key:{uuid.uuid4()}", "rule_id": "checklist_gap",
        "purpose": "close the gap", "subject_ref": f"report:{uuid.uuid4()}:item:3",
        "kind": "approve", "question": "Item 3 is missing. Chase it?",
        "consequence": "Inspection may fail", "options": [{"id": "yes", "label": "Yes"}],
        "evidence": [{"ref": "R1", "text": "report"}], "urgency": "normal",
        "deadline_at": NOW + dt.timedelta(days=1), "timeout_default": "yes",
        "recipients": [{"user_id": str(uuid.uuid4()), "role": "pm"}],
        "event_id": None, "company_id": co["id"], "site_id": site["id"],
    }
    card.update(over)
    return card


def _ledger(co, site, subject_ref, **over):
    row = {"company_id": co["id"], "site_id": site["id"], "card_id": None,
           "subject_ref": subject_ref, "decision": "yes", "kind": "decided",
           "decided_by": None, "authority_basis": "pm", "evidence": [],
           "supersedes_entry_id": None, "source": "card"}
    row.update(over)
    return row


# ---------------------------------------------------------------- site_events

def test_insert_dedupes_into_duplicate_row(db):
    co, site = _world(db)
    ev = _ev(co, site, dedupe_key="gap:same-key-" + uuid.uuid4().hex)
    first, new1 = se.insert(db, dict(ev))
    assert new1 is True
    assert first["status"] == "queued" and first["dedupe_key"] == ev["dedupe_key"]
    assert first["seq"] is not None and first["payload"] == {"item": 3}
    assert first["received_at"] == NOW
    second, new2 = se.insert(db, dict(ev))
    assert new2 is False
    assert second["event_id"] != first["event_id"]
    assert second["status"] == "duplicate"
    assert second["dedupe_key"] == f"{ev['dedupe_key']}|dup|{second['event_id']}"
    # a third copy is also kept as its own duplicate row
    third, new3 = se.insert(db, dict(ev))
    assert new3 is False and third["status"] == "duplicate"
    n = db.execute("SELECT count(*) FROM site_events WHERE dedupe_key LIKE %s",
                   (ev["dedupe_key"] + "%",)).fetchone()[0]
    assert n == 3


def test_insert_uses_given_event_id(db):
    co, site = _world(db)
    eid = uuid.uuid4()
    row, new = se.insert(db, _ev(co, site, event_id=eid))
    assert new is True and row["event_id"] == eid


def test_claim_due_respects_not_before(db):
    co, site = _world(db)
    due, _ = se.insert(db, _ev(co, site, not_before=NOW - dt.timedelta(minutes=1)))
    null_nb, _ = se.insert(db, _ev(co, site))
    later, _ = se.insert(db, _ev(co, site, not_before=NOW + dt.timedelta(minutes=5)))
    done, _ = se.insert(db, _ev(co, site, status="done"))
    claimed = [r for r in se.claim_due(db, NOW, limit=1000) if r["company_id"] == co["id"]]
    assert [r["event_id"] for r in claimed] == [due["event_id"], null_nb["event_id"]]  # by seq
    assert all(r["status"] == "running" for r in claimed)
    assert all(r["attempts"] == 0 for r in claimed)   # claim never bumps attempts
    st = dict(db.execute("SELECT event_id, status FROM site_events WHERE company_id=%s",
                         (co["id"],)).fetchall())
    assert st[later["event_id"]] == "queued" and st[done["event_id"]] == "done"
    # running rows are not claimed again
    again = [r for r in se.claim_due(db, NOW, limit=1000) if r["company_id"] == co["id"]]
    assert again == []
    later_claim = [r for r in se.claim_due(db, NOW + dt.timedelta(minutes=5), limit=1000)
                   if r["company_id"] == co["id"]]
    assert [r["event_id"] for r in later_claim] == [later["event_id"]]


@pytest.fixture
def committed(migrated_db_url):
    """Autocommit connection + committed seed world; everything deleted afterwards.
    Never writes decision_ledger (the trigger blocks its cleanup)."""
    conn = psycopg.connect(migrated_db_url, autocommit=True)
    co, site = _world(conn, "lock")
    try:
        yield migrated_db_url, conn, co, site
    finally:
        conn.execute("DELETE FROM decision_cards WHERE company_id=%s", (co["id"],))
        conn.execute("DELETE FROM event_runs WHERE company_id=%s", (co["id"],))
        conn.execute("DELETE FROM site_events WHERE company_id=%s", (co["id"],))
        conn.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
        conn.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
        conn.close()


def test_claim_due_skips_locked_rows(committed):
    url, conn, co, site = committed
    mine = [se.insert(conn, _ev(co, site))[0]["event_id"] for _ in range(3)]
    a = psycopg.connect(url)
    b = psycopg.connect(url)
    # A SKIP LOCKED regression would make the second claimer wait on the row locks: fail fast.
    a.execute("SET lock_timeout = '2s'")
    b.execute("SET lock_timeout = '2s'")
    a.commit()
    b.commit()
    mine_set = set(mine)
    try:
        # Global claims: other tests' leftover queued rows may come back too, so every
        # assertion is about the intersection with this test's own ids.
        got_a = {r["event_id"] for r in se.claim_due(a, NOW, limit=100000)} & mine_set
        assert got_a == mine_set                           # holds row locks, uncommitted
        got_b = {r["event_id"] for r in se.claim_due(b, NOW, limit=100000)} & mine_set
        assert got_b == set()                              # skipped, did not block
        a.rollback()                                       # a's locks released
        b.rollback()
        got_b2 = {r["event_id"] for r in se.claim_due(b, NOW, limit=100000)} & mine_set
        assert got_b2 == mine_set                          # now b holds them
        got_a2 = {r["event_id"] for r in se.claim_due(a, NOW, limit=100000)} & mine_set
        assert got_a2 == set()                             # second claim gets none of b's
    finally:
        a.rollback()
        b.rollback()
        a.close()
        b.close()


def test_set_status_whitelists_fields_and_updates(db):
    co, site = _world(db)
    ev, _ = se.insert(db, _ev(co, site))
    nb = NOW + dt.timedelta(minutes=10)
    row = se.set_status(db, ev["event_id"], co["id"], site["id"], "queued",
                        attempts=2, not_before=nb, last_error="boom", rule_id="checklist_gap")
    assert row["status"] == "queued" and row["attempts"] == 2
    assert row["not_before"] == nb and row["last_error"] == "boom"
    assert row["rule_id"] == "checklist_gap"
    with pytest.raises(ValueError):
        se.set_status(db, ev["event_id"], co["id"], site["id"], "done", seq=1)
    with pytest.raises(ValueError):
        se.set_status(db, ev["event_id"], co["id"], site["id"], "done",
                      **{"status = 'done'; --": 1})
    plain = se.set_status(db, ev["event_id"], co["id"], site["id"], "done")
    assert plain["status"] == "done" and plain["attempts"] == 2


def test_queued_same_subject(db):
    co, site = _world(db)
    subj = "report:r1:item:3"
    a, _ = se.insert(db, _ev(co, site, subject_ref=subj))
    b, _ = se.insert(db, _ev(co, site, subject_ref=subj))
    se.insert(db, _ev(co, site, subject_ref=subj, status="done"))
    se.insert(db, _ev(co, site, subject_ref="report:r1:item:4"))
    se.insert(db, _ev(co, site, subject_ref=subj, type="checklist_overdue"))
    other_co, other_site = _world(db, "b")
    se.insert(db, _ev(other_co, other_site, subject_ref=subj))
    rows = se.queued_same_subject(db, co["id"], site["id"], "checklist_gap", subj)
    assert [r["event_id"] for r in rows] == [a["event_id"], b["event_id"]]


def test_reads_are_tenant_scoped(db):
    co, site = _world(db)
    other_co, other_site = _world(db, "b")
    ev, _ = se.insert(db, _ev(co, site))
    assert se.set_status(db, ev["event_id"], other_co["id"], site["id"], "done") is None
    assert se.set_status(db, ev["event_id"], co["id"], other_site["id"], "done") is None
    card = dc.insert_open(db, _card(co, site))
    assert dc.find_open(db, other_co["id"], site["id"], card["card_key"]) is None
    assert dc.find_open(db, co["id"], other_site["id"], card["card_key"]) is None
    assert dc.supersede(db, other_co["id"], site["id"], card["card_key"]) == 0
    assert dc.bump(db, card["card_id"], other_co["id"], site["id"], "x") is None
    assert dc.open_for_subject_prefix(db, other_co["id"], site["id"], "checklist_gap",
                                      "report:") == []
    assert se.queued_same_subject(db, other_co["id"], site["id"], ev["type"],
                                  ev["subject_ref"]) == []
    # untouched
    assert db.execute("SELECT status FROM site_events WHERE event_id=%s",
                      (ev["event_id"],)).fetchone()[0] == "queued"
    assert dc.find_open(db, co["id"], site["id"], card["card_key"])["card_id"] == card["card_id"]


# ---------------------------------------------------------------- event_runs

def test_runs_start_add_event_finish(db):
    co, site = _world(db)
    e1, _ = se.insert(db, _ev(co, site))
    e2, _ = se.insert(db, _ev(co, site))
    e3, _ = se.insert(db, _ev(co, site))
    run = er.start(db, co["id"], site["id"], [e1["event_id"], e2["event_id"]], 2, "checklist")
    assert run["attempt"] == 2 and run["handler"] == "checklist"
    assert run["company_id"] == co["id"] and run["site_id"] == site["id"]
    assert run["started_at"] is not None and run["ended_at"] is None
    er.add_event(db, run["run_id"], e3["event_id"], co["id"], site["id"])
    er.add_event(db, run["run_id"], e3["event_id"], co["id"], site["id"])   # idempotent
    links = db.execute("SELECT event_id, company_id, site_id FROM run_events WHERE run_id=%s",
                       (run["run_id"],)).fetchall()
    assert {r[0] for r in links} == {e1["event_id"], e2["event_id"], e3["event_id"]}
    assert all(r[1] == co["id"] and r[2] == site["id"] for r in links)
    done = er.finish(db, run["run_id"], co["id"], site["id"], "failed", error="boom",
                     result_hash="h1")
    assert done["outcome"] == "failed" and done["error"] == "boom"
    assert done["result_hash"] == "h1" and done["ended_at"] is not None


def test_runs_are_tenant_scoped(db):
    co, site = _world(db)
    other_co, other_site = _world(db, "b")
    e1, _ = se.insert(db, _ev(co, site))
    run = er.start(db, co["id"], site["id"], [], 1, "checklist")
    assert er.finish(db, run["run_id"], other_co["id"], site["id"], "done") is None
    # an event of another tenant is not linked
    assert er.add_event(db, run["run_id"], e1["event_id"], other_co["id"], site["id"]) == 0
    assert db.execute("SELECT count(*) FROM run_events WHERE run_id=%s",
                      (run["run_id"],)).fetchone()[0] == 0
    assert er.add_event(db, run["run_id"], e1["event_id"], co["id"], site["id"]) == 1


# ---------------------------------------------------------------- decision_cards

def test_insert_open_returns_none_when_open_card_exists(db):
    co, site = _world(db)
    ev, _ = se.insert(db, _ev(co, site))
    card = _card(co, site, event_id=ev["event_id"])
    first = dc.insert_open(db, card)
    assert first["status"] == "open" and first["version"] == 1
    assert first["options"] == card["options"] and first["recipients"] == card["recipients"]
    assert first["event_id"] == ev["event_id"]
    assert dc.insert_open(db, dict(card)) is None
    # superseding frees the key
    assert dc.supersede(db, co["id"], site["id"], card["card_key"]) == 1
    assert dc.find_open(db, co["id"], site["id"], card["card_key"]) is None
    again = dc.insert_open(db, dict(card))
    assert again is not None and again["card_id"] != first["card_id"]
    assert dc.find_open(db, co["id"], site["id"], card["card_key"])["card_id"] == again["card_id"]


def test_bump_increments_version(db):
    co, site = _world(db)
    card = dc.insert_open(db, _card(co, site))
    b1 = dc.bump(db, card["card_id"], co["id"], site["id"], "Now 2 days late")
    assert b1["version"] == 2 and b1["consequence"] == "Now 2 days late"
    b2 = dc.bump(db, card["card_id"], co["id"], site["id"], "Now 3 days late")
    assert b2["version"] == 3


def test_open_for_subject_prefix_escapes_wildcards(db):
    co, site = _world(db)
    a = dc.insert_open(db, _card(co, site, subject_ref="report:r_1:item:1"))
    b = dc.insert_open(db, _card(co, site, subject_ref="report:r_1:item:2"))
    dc.insert_open(db, _card(co, site, subject_ref="report:rX1:item:1"))      # '_' is literal
    dc.insert_open(db, _card(co, site, subject_ref="report:r%1:item:1"))
    dc.insert_open(db, _card(co, site, subject_ref="report:r_1:item:9",
                             rule_id="checklist_overdue"))                    # other rule
    gone = dc.insert_open(db, _card(co, site, subject_ref="report:r_1:item:3"))
    dc.supersede(db, co["id"], site["id"], gone["card_key"])                 # not open
    rows = dc.open_for_subject_prefix(db, co["id"], site["id"], "checklist_gap", "report:r_1:")
    assert sorted(r["card_id"] for r in rows) == sorted([a["card_id"], b["card_id"]])
    pct = dc.open_for_subject_prefix(db, co["id"], site["id"], "checklist_gap", "report:r%")
    assert [r["subject_ref"] for r in pct] == ["report:r%1:item:1"]
    bs = dc.open_for_subject_prefix(db, co["id"], site["id"], "checklist_gap", "report:r\\")
    assert bs == []


def test_set_label_does_not_change_status(db):
    co, site = _world(db)
    admin = _user(db, co, "platform_admin")
    card = dc.insert_open(db, _card(co, site))
    row = dc.set_label(db, card["card_id"], "not_needed", "noise", admin["id"])
    assert row["shadow_label"] == "not_needed" and row["label_note"] == "noise"
    assert row["labelled_by"] == admin["id"] and row["labelled_at"] is not None
    assert row["status"] == "open" and row["version"] == 1
    assert dc.set_label(db, uuid.uuid4(), "needed", None, admin["id"]) is None


def test_list_shadow_and_summary_rows_span_companies(db):
    co, site = _world(db)
    other_co, other_site = _world(db, "b")
    c1 = dc.insert_open(db, _card(co, site))
    c2 = dc.insert_open(db, _card(other_co, other_site, kind="fault"))
    db.execute("UPDATE decision_cards SET created_at = created_at + interval '1 second' "
               "WHERE card_id=%s", (c2["card_id"],))
    admin = _user(db, co, "platform_admin")
    dc.set_label(db, c1["card_id"], "needed", None, admin["id"])
    rows = [r for r in dc.list_shadow(db, 1000, 0)
            if r["card_id"] in (c1["card_id"], c2["card_id"])]
    assert [r["card_id"] for r in rows] == [c2["card_id"], c1["card_id"]]   # newest first
    assert rows[0]["company_name"] == other_co["name"]
    assert rows[0]["site_name"] == other_site["name"]
    assert rows[1]["company_name"] == co["name"] and rows[1]["site_name"] == site["name"]
    labelled = [r["card_id"] for r in dc.list_shadow(db, 1000, 0, label="needed")]
    assert c1["card_id"] in labelled and c2["card_id"] not in labelled
    paged = dc.list_shadow(db, 1, 0)
    assert len(paged) == 1
    summ = dc.shadow_summary_rows(db)
    assert set(summ[0].keys()) == {"shadow_label", "kind", "recipients", "created_at"}
    mine = [r for r in summ if r["created_at"] in (c1["created_at"],)]
    assert any(r["kind"] == "approve" and r["shadow_label"] == "needed" for r in mine)


# ---------------------------------------------------------------- decision_ledger

def test_ledger_decided_ignores_defaulted_and_superseded(db):
    co, site = _world(db)
    other_co, other_site = _world(db, "b")
    subj = "report:r1:item:3"
    assert dl.decided(db, co["id"], site["id"], subj) is None
    d = dl.append(db, _ledger(co, site, subj, kind="defaulted", source="timeout_default"))
    assert d["kind"] == "defaulted"
    assert dl.decided(db, co["id"], site["id"], subj) is None          # defaulted never counts
    first = dl.append(db, _ledger(co, site, subj, decision="yes",
                                  decided_at=NOW - dt.timedelta(hours=2)))
    assert dl.decided(db, co["id"], site["id"], subj)["entry_id"] == first["entry_id"]
    # a later default does not displace the decision
    dl.append(db, _ledger(co, site, subj, kind="defaulted", source="timeout_default",
                          decided_at=NOW))
    assert dl.decided(db, co["id"], site["id"], subj)["entry_id"] == first["entry_id"]
    # changing one's mind: a declined row that supersedes the first
    second = dl.append(db, _ledger(co, site, subj, decision="no", kind="declined",
                                   supersedes_entry_id=first["entry_id"],
                                   decided_at=NOW - dt.timedelta(hours=1),
                                   evidence=[{"ref": "R1"}]))
    got = dl.decided(db, co["id"], site["id"], subj)
    assert got["entry_id"] == second["entry_id"] and got["evidence"] == [{"ref": "R1"}]
    # other tenants and subjects see nothing
    assert dl.decided(db, other_co["id"], site["id"], subj) is None
    assert dl.decided(db, co["id"], other_site["id"], subj) is None
    assert dl.decided(db, co["id"], site["id"], "report:r1:item:4") is None



def test_ledger_superseded_row_loses_even_when_it_is_newest(db):
    """A decided t1, B decided t3, C decided t2 superseding B -> C. ORDER BY alone would
    pick B (the newest); only the supersession clause makes C win."""
    co, site = _world(db)
    subj = "report:r3:item:1"
    t1, t2, t3 = (NOW - dt.timedelta(hours=h) for h in (3, 2, 1))
    dl.append(db, _ledger(co, site, subj, decision="a", decided_at=t1))
    b = dl.append(db, _ledger(co, site, subj, decision="b", decided_at=t3))
    c = dl.append(db, _ledger(co, site, subj, decision="c", kind="declined", decided_at=t2,
                              supersedes_entry_id=b["entry_id"]))
    assert dl.decided(db, co["id"], site["id"], subj)["entry_id"] == c["entry_id"]


def test_ledger_defaulted_or_foreign_pointer_does_not_supersede(db):
    co, site = _world(db)
    other_co, other_site = _world(db, "b")
    subj = "report:r4:item:1"
    a = dl.append(db, _ledger(co, site, subj, decided_at=NOW - dt.timedelta(hours=2)))
    # a default pointing at A is not a change of mind
    dl.append(db, _ledger(co, site, subj, kind="defaulted", source="timeout_default",
                          supersedes_entry_id=a["entry_id"]))
    assert dl.decided(db, co["id"], site["id"], subj)["entry_id"] == a["entry_id"]
    # another tenant's decided row pointing at A does not hide it
    dl.append(db, _ledger(other_co, other_site, subj, decision="no", kind="declined",
                          supersedes_entry_id=a["entry_id"]))
    assert dl.decided(db, co["id"], site["id"], subj)["entry_id"] == a["entry_id"]


def test_runs_start_raises_when_an_event_cannot_be_linked(db):
    co, site = _world(db)
    other_co, other_site = _world(db, "b")
    mine, _ = se.insert(db, _ev(co, site))
    foreign, _ = se.insert(db, _ev(other_co, other_site))
    with pytest.raises(ValueError):
        er.start(db, co["id"], site["id"], [mine["event_id"], foreign["event_id"]], 1, "x")
    with pytest.raises(ValueError):
        er.start(db, co["id"], site["id"], [uuid.uuid4()], 1, "x")
    # the same event listed twice counts once
    run = er.start(db, co["id"], site["id"], [mine["event_id"], mine["event_id"]], 1, "x")
    assert run["run_id"] is not None


def test_insert_rejects_duplicate_status(db):
    co, site = _world(db)
    with pytest.raises(ValueError):
        se.insert(db, _ev(co, site, status="duplicate"))
