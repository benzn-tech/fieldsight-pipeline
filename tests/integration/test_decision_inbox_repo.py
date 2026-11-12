"""Integration: the decision-card inbox repository reads/writes (spec 2026-11-12 rev 2 §4, §5).

decision_cards.inbox_rows / answer / get_for_answer against real Postgres (migration 0103).
Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as dt
import uuid

import pytest

from repositories import companies, sites, users
from repositories import decision_cards as dc
from repositories import site_events as se

pytestmark = pytest.mark.integration

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 12, 1, 0, tzinfo=UTC)


def _world(conn):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(conn, f"Co-inbox-{tag}")
    site = sites.create_site(conn, co["id"], f"Site-inbox-{tag}")
    return co, site


def _user(conn, co):
    tag = uuid.uuid4().hex[:8]
    return users.upsert_user(conn, f"sub-inbox-{tag}", f"inbox-{tag}@example.com",
                             company_id=co["id"])


def _event(conn, co, site, payload):
    row, _ = se.insert(conn, {
        "company_id": co["id"], "site_id": site["id"], "source": "action_items",
        "type": "todo.overdue", "subject_ref": f"item:{uuid.uuid4()}", "bucket": "",
        "occurred_at": NOW, "received_at": NOW, "summary": "s", "link": None,
        "payload": payload, "dedupe_key": f"k:{uuid.uuid4()}", "status": "done"})
    return row["event_id"]


def _card(conn, co, site, user_ids, *, event_id=None, status="open", rule_id="todo.overdue",
          kind="choose"):
    card = dc.insert_open(conn, {
        "card_key": f"key:{uuid.uuid4()}", "rule_id": rule_id, "purpose": "p",
        "subject_ref": f"stable-{uuid.uuid4()}", "kind": kind, "question": "q?",
        "options": [{"id": "done", "label": "Mark done"}], "evidence": [],
        "urgency": "normal", "deadline_at": None,
        "recipients": [{"user_id": str(u), "role": "pm"} for u in user_ids],
        "event_id": event_id, "company_id": co["id"], "site_id": site["id"]})
    if status != "open":
        conn.execute("UPDATE decision_cards SET status = %s WHERE card_id = %s",
                     (status, card["card_id"]))
    return card


def test_inbox_rows_only_the_users_open_cards_in_listed_companies(db):
    co, site = _world(db)
    other_co, other_site = _world(db)
    me, other = _user(db, co), _user(db, co)
    ev = _event(db, co, site, {"text": "Order beams", "deadline": "2026-11-10"})
    mine = _card(db, co, site, [me["id"], other["id"]], event_id=ev)
    no_event = _card(db, co, site, [me["id"]])
    backlog = _card(db, co, site, [me["id"]], rule_id="todo.overdue_backlog", kind="fact")
    _card(db, co, site, [other["id"]])                                   # not mine
    _card(db, co, site, [me["id"]], status="superseded")
    _card(db, co, site, [me["id"]], status="answered")
    _card(db, other_co, other_site, [me["id"]])                          # company not listed

    rows = dc.inbox_rows(db, [str(co["id"])], str(me["id"]))
    by_id = {str(r["card_id"]): r for r in rows}
    assert set(by_id) == {str(mine["card_id"]), str(no_event["card_id"]),
                          str(backlog["card_id"])}
    r = by_id[str(mine["card_id"])]
    assert r["site_name"] == site["name"]
    assert r["payload"] == {"text": "Order beams", "deadline": "2026-11-10"}
    assert r["company_id"] == co["id"] and r["site_id"] == site["id"]
    assert by_id[str(no_event["card_id"])]["payload"] is None
    for col in ("answered_by", "answered_option", "answered_at", "answer_reason"):
        assert col in r


def test_inbox_rows_empty_company_list_returns_nothing(db):
    co, site = _world(db)
    me = _user(db, co)
    _card(db, co, site, [me["id"]])
    assert dc.inbox_rows(db, [], str(me["id"])) == []


def test_inbox_rows_payload_join_is_tenant_scoped(db):
    co, site = _world(db)
    other_co, other_site = _world(db)
    me = _user(db, co)
    foreign_ev = _event(db, other_co, other_site, {"text": "secret"})
    card = _card(db, co, site, [me["id"]], event_id=foreign_ev)
    rows = dc.inbox_rows(db, [str(co["id"])], str(me["id"]))
    assert [str(r["card_id"]) for r in rows] == [str(card["card_id"])]
    assert rows[0]["payload"] is None


def test_answer_first_wins_and_sets_columns(db):
    co, site = _world(db)
    me = _user(db, co)
    card = _card(db, co, site, [me["id"]])
    row = dc.answer(db, card["card_id"], co["id"], site["id"], me["id"], "done", "finished")
    assert row is not None
    assert row["status"] == "answered"
    assert row["answered_by"] == me["id"]
    assert row["answered_option"] == "done"
    assert row["answered_at"] is not None
    assert row["answer_reason"] == "finished"
    assert dc.answer(db, card["card_id"], co["id"], site["id"], me["id"], "done", None) is None


def test_answer_wrong_tenant_returns_none_and_leaves_card_open(db):
    co, site = _world(db)
    other_co, other_site = _world(db)
    me = _user(db, co)
    card = _card(db, co, site, [me["id"]])
    assert dc.answer(db, card["card_id"], other_co["id"], site["id"], me["id"], "done", None) is None
    assert dc.answer(db, card["card_id"], co["id"], other_site["id"], me["id"], "done", None) is None
    got = dc.get_for_answer(db, card["card_id"])
    assert got["status"] == "open" and got["answered_by"] is None


def test_get_for_answer_includes_answer_columns_payload_and_site(db):
    co, site = _world(db)
    me = _user(db, co)
    ev = _event(db, co, site, {"text": "T", "deadline": "2026-11-10"})
    card = _card(db, co, site, [me["id"]], event_id=ev)
    got = dc.get_for_answer(db, card["card_id"])
    assert got["payload"] == {"text": "T", "deadline": "2026-11-10"}
    assert got["recipients"] == [{"user_id": str(me["id"]), "role": "pm"}]
    assert got["options"] == [{"id": "done", "label": "Mark done"}]
    assert got["site_name"] == site["name"]
    assert got["answered_option"] is None
    dc.answer(db, card["card_id"], co["id"], site["id"], me["id"], "done", None)
    got = dc.get_for_answer(db, card["card_id"])
    assert (got["status"], got["answered_by"], got["answered_option"]) == ("answered", me["id"], "done")
    assert dc.get_for_answer(db, uuid.uuid4()) is None
    assert dc.get_for_answer(db, "not-a-uuid") is None
