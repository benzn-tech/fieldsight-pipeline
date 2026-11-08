"""Integration: find_items against a real Postgres.

find_items is the shared read API the status-suggestion reconcile step, cross-day
continuity and the nightly agent build on, so these pin the contract they read: site and
company scope, live rows only, first_seen across superseded passes, the filters, the
ranking and the tie order.

`created_at` defaults to now(), which is the TRANSACTION start: every row a test inserts
would share one timestamp and no ordering test would discriminate. Rows whose order
matters get an explicit created_at.

All text is synthetic. Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as _dt
import json
import uuid

import pytest

from repositories import (action_items, companies, findings, redactions, sites,
                          topic_decisions, topic_questions, topics, users)
from repositories.item_history import find_items

pytestmark = pytest.mark.integration

D1, D2, D3 = "2026-10-01", "2026-10-05", "2026-10-09"
T0 = _dt.datetime(2026, 10, 1, 9, 0, tzinfo=_dt.timezone.utc)

KEYS = {"kind", "stable_id", "item_id", "text", "responsible", "deadline", "status",
        "first_seen", "source", "recorded_by", "carried_from", "score", "matched_terms",
        "place", "origin"}

_TABLE = {"action_item": "action_items", "finding": "findings",
          "decision": "topic_decisions", "question": "topic_questions"}


def _ts(minutes):
    return T0 + _dt.timedelta(minutes=minutes)


class Seed:
    def __init__(self, db):
        self.db = db
        tag = uuid.uuid4().hex[:8]
        self.tag = tag
        self.co = companies.create_company(db, f"History-Co-{tag}")
        self.site = sites.create_site(db, self.co["id"], f"History-Site-{tag}")
        self.folder = f"Folder-{tag}"
        self.user = users.upsert_field_only_user(db, self.co["id"], self.folder,
                                                 "Fol", "Der", "worker")

    def key(self, date, base, folder=None):
        return f"extractions/{folder or self.folder}/{date}/{base}.json"

    def topic(self, date, base, *, folder=None, site=None, key=None):
        return topics.upsert_topic(
            self.db, (site or self.site)["id"], date, f"Topic {base}",
            user_id=self.user["id"], source_s3_key=key or self.key(date, base, folder),
            summary="s")

    def supersede(self, topic):
        self.db.execute("UPDATE topics SET superseded_at = now(), "
                        "superseded_by_run = 'final:test' WHERE id = %s", (topic["id"],))

    def created(self, kind, row_id, minutes):
        self.db.execute(f"UPDATE {_TABLE[kind]} SET created_at = %s WHERE id = %s",
                        (_ts(minutes), row_id))

    def action(self, topic, text, minutes, *, status="open", responsible=None,
               site=None):
        row = action_items.insert_item(self.db, topic["id"], (site or self.site)["id"],
                                       text, responsible=responsible)
        if status != "open":
            self.db.execute("UPDATE action_items SET status = %s WHERE id = %s",
                            (status, row["id"]))
        self.created("action_item", row["id"], minutes)
        return row

    def finding(self, topic, text, minutes, *, entity=None):
        row = findings.insert_findings(self.db, topic["id"], self.site["id"], [
            {"observation": text, "domain": "quality", "severity": "minor",
             "entity": {"name": entity} if entity else None}])[0]
        row["stable_id"] = findings.get_stable_id(self.db, row["id"])
        self.created("finding", row["id"], minutes)
        return row

    def decision(self, topic, text, minutes, *, decided_by=None):
        row = topic_decisions.insert_decisions(self.db, topic["id"], self.site["id"], [
            {"decision": text, "decided_by": decided_by}])[0]
        self.created("decision", row["id"], minutes)
        return row

    def question(self, topic, text, minutes):
        row = topic_questions.insert_questions(self.db, topic["id"], self.site["id"],
                                               [text])[0]
        self.created("question", row["id"], minutes)
        return row

    def carry(self, kind, new_row, old_row):
        mod = {"action_item": action_items, "finding": findings,
               "decision": topic_decisions, "question": topic_questions}[kind]
        mod.carry_identity(self.db, new_row["id"],
                           {"id": old_row["id"], "stable_id": old_row["stable_id"],
                            "human_touched": False})

    def link(self, kind, row, stem, start, score=0.9):
        col = {"action_item": "src_action_item_id", "finding": "src_finding_id",
               "decision": "src_decision_id", "question": "src_question_id"}[kind]
        turn = {"source_stem": stem, "start_sec": start, "end_sec": start + 4.0,
                "speaker_label": "spk_0", "quote_status": "verified"}
        self.db.execute(
            f"INSERT INTO event_links (site_id, link_type, src_kind, src_stable_id, {col}, "
            f"dst_kind, dst_turn, method, score, rule_version) "
            f"VALUES (%s,'supported_by',%s,%s,%s,'turn',%s::jsonb,'lexical',%s,'lexical_v1')",
            (self.site["id"], kind, row["stable_id"], row["id"], json.dumps(turn), score))

    def find(self, **kw):
        args = {"company_id": self.co["id"], "site_id": self.site["id"],
                "kinds": ("action_item", "finding", "decision", "question")}
        args.update(kw)
        return find_items(self.db, **args)


def _texts(rows):
    return [r["text"] for r in rows]


def test_returns_only_this_sites_live_items(db):
    s = Seed(db)
    other_site = sites.create_site(db, s.co["id"], f"History-Other-{s.tag}")
    t = s.topic(D1, "sid" + "a" * 32)
    s.action(t, "Alpha pour the slab", 1, responsible="Crew Lead")
    s.finding(t, "Beta crack on wall", 2, entity="Beta Trade")
    s.decision(t, "Gamma use the north gate", 3, decided_by="Site Boss")
    s.question(t, "Delta when does steel arrive", 4)
    t_other = s.topic(D1, "sid" + "b" * 32, site=other_site)
    s.action(t_other, "Elsewhere item", 5, site=other_site)

    rows = s.find()

    assert sorted(_texts(rows)) == ["Alpha pour the slab", "Beta crack on wall",
                                    "Delta when does steel arrive",
                                    "Gamma use the north gate"]
    for r in rows:
        assert set(r) == KEYS
        assert r["place"] is None
        assert r["score"] == 0.0 and r["matched_terms"] == []
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["action_item"]["responsible"] == "Crew Lead"
    assert by_kind["action_item"]["status"] == "open"
    assert by_kind["finding"]["responsible"] == "Beta Trade"
    assert by_kind["decision"]["responsible"] == "Site Boss"
    assert by_kind["decision"]["status"] is None
    assert by_kind["question"]["responsible"] is None
    assert by_kind["question"]["status"] == "open"
    src = by_kind["action_item"]["source"]
    assert src == {"user_folder": s.folder, "date": D1, "session_base": "sid" + "a" * 32,
                   "source_s3_key": s.key(D1, "sid" + "a" * 32)}
    assert by_kind["action_item"]["recorded_by"] == s.folder
    # Only the kinds asked for.
    assert {r["kind"] for r in s.find(kinds=("question",))} == {"question"}


def test_moved_site_is_scoped_by_its_current_company(db):
    s = Seed(db)
    t = s.topic(D1, "sid" + "a" * 32)
    s.action(t, "Moved site item", 1)
    new_co = companies.create_company(db, f"History-NewCo-{s.tag}")

    assert _texts(s.find()) == ["Moved site item"]
    db.execute("UPDATE sites SET company_id = %s WHERE id = %s", (new_co["id"], s.site["id"]))

    assert s.find() == [], "the old company must lose the site the moment it moves"
    assert _texts(s.find(company_id=new_co["id"])) == ["Moved site item"]


def test_superseded_pass_hidden_but_counts_for_first_seen(db):
    s = Seed(db)
    base = "sid" + "a" * 32
    t1 = s.topic(D1, base)
    old = s.action(t1, "Order rebar pass one", 1)
    s.supersede(t1)
    t2 = s.topic(D1, base)
    new = s.action(t2, "Order rebar pass two", 30)
    s.carry("action_item", new, old)

    rows = s.find(kinds=("action_item",))

    assert len(rows) == 1
    r = rows[0]
    assert r["text"] == "Order rebar pass two", "the live pass's text, never the stale one"
    assert r["stable_id"] == old["stable_id"]
    assert r["first_seen"] == _ts(1)
    assert r["source"]["source_s3_key"] == t1["source_s3_key"]
    assert r["carried_from"] == old["id"]


def test_item_dropped_by_the_live_pass_is_not_returned(db):
    """The re-extraction did not carry this one: it exists only on the superseded pass, so
    no deduplication by stable_id can hide it -- only the live arm can."""
    s = Seed(db)
    base = "sid" + "a" * 32
    t1 = s.topic(D1, base)
    s.action(t1, "Only in the stale pass", 1)
    s.question(t1, "Stale question", 2)
    s.supersede(t1)
    t2 = s.topic(D1, base)
    s.action(t2, "Only in the live pass", 30)

    assert _texts(s.find()) == ["Only in the live pass"]


def test_first_seen_comes_from_an_earlier_session(db):
    """A carry across sessions: the first_seen row is an older day's, under another key."""
    s = Seed(db)
    t1 = s.topic(D1, "sid" + "a" * 32)
    old = s.decision(t1, "Use the north gate", 1)
    s.supersede(t1)
    t2 = s.topic(D2, "sid" + "b" * 32)
    new = s.decision(t2, "Use the north gate only", 50)
    s.carry("decision", new, old)

    r = s.find(kinds=("decision",))[0]
    assert r["source"]["date"] == D1
    assert r["source"]["session_base"] == "sid" + "a" * 32


def test_since_until_filter_on_first_seen_report_date(db):
    s = Seed(db)
    t1 = s.topic(D1, "sid" + "a" * 32)
    early = s.action(t1, "Early item", 1)
    s.supersede(t1)
    t2 = s.topic(D3, "sid" + "c" * 32)
    carried = s.action(t2, "Early item restated", 40)
    s.carry("action_item", carried, early)
    t3 = s.topic(D2, "sid" + "b" * 32)
    s.action(t3, "Middle item", 20)

    # The carried item's live row is on D3 but it was first seen on D1.
    assert _texts(s.find(kinds=("action_item",), since=D2)) == ["Middle item"]
    assert _texts(s.find(kinds=("action_item",), until=D1)) == ["Early item restated"]
    assert _texts(s.find(kinds=("action_item",), since=D2, until=D2)) == ["Middle item"]
    assert _texts(s.find(kinds=("action_item",), since=_dt.date(2026, 10, 5),
                         until=_dt.date(2026, 10, 5))) == ["Middle item"]


def test_statuses_dict_filters_per_kind(db):
    s = Seed(db)
    t = s.topic(D1, "sid" + "a" * 32)
    for i, st in enumerate(("open", "in_progress", "blocked", "done", "closed")):
        s.action(t, f"Action {st}", i, status=st)
    s.decision(t, "A decision", 10)

    rows = s.find(kinds=("action_item", "decision"),
                  statuses={"action_item": ["open", "in_progress", "blocked"]})

    assert sorted(_texts(rows)) == ["A decision", "Action blocked", "Action in_progress",
                                    "Action open"]


def test_exclude_source_keys_drops_this_sessions_items(db):
    s = Seed(db)
    t_now = s.topic(D2, "sid" + "b" * 32)
    s.action(t_now, "This session item", 5)
    t_before = s.topic(D1, "sid" + "a" * 32)
    s.action(t_before, "Earlier session item", 1)

    rows = s.find(exclude_source_keys=[t_now["source_s3_key"]])
    assert _texts(rows) == ["Earlier session item"]
    assert len(s.find(exclude_source_keys=[])) == 2


def test_exclude_source_keys_uses_the_live_rows_key(db):
    """An item first seen in an earlier session but restated in this one is excluded: the
    calling session already has it."""
    s = Seed(db)
    t1 = s.topic(D1, "sid" + "a" * 32)
    old = s.question(t1, "When does steel arrive", 1)
    s.supersede(t1)
    t2 = s.topic(D2, "sid" + "b" * 32)
    new = s.question(t2, "When does the steel arrive", 30)
    s.carry("question", new, old)

    assert s.find(exclude_source_keys=[t2["source_s3_key"]]) == []
    assert len(s.find(exclude_source_keys=[t1["source_s3_key"]])) == 1


def test_recorded_by_filters_on_first_seen_recorder(db):
    s = Seed(db)
    other = f"Other-{s.tag}"
    t1 = s.topic(D1, "sid" + "a" * 32, folder=other)
    old = s.action(t1, "Recorded by other first", 1)
    s.supersede(t1)
    t2 = s.topic(D2, "sid" + "b" * 32)  # restated in this folder's session
    new = s.action(t2, "Recorded by other first, restated", 30)
    s.carry("action_item", new, old)
    t3 = s.topic(D2, "sid" + "c" * 32)
    s.action(t3, "Recorded by me", 20)
    t4 = s.topic(D2, "x", key=f"manual/{s.tag}/odd-key")
    s.action(t4, "Unparseable key", 25)

    mine = s.find(recorded_by=s.folder)
    theirs = s.find(recorded_by=other)
    assert _texts(mine) == ["Recorded by me"]
    assert _texts(theirs) == ["Recorded by other first, restated"]
    assert theirs[0]["recorded_by"] == other
    odd = [r for r in s.find() if r["text"] == "Unparseable key"][0]
    assert odd["recorded_by"] is None
    assert odd["source"]["user_folder"] is None
    assert odd["source"]["source_s3_key"] == f"manual/{s.tag}/odd-key"


def test_responsible_filter(db):
    s = Seed(db)
    t = s.topic(D1, "sid" + "a" * 32)
    s.action(t, "Assigned to crew", 1, responsible="Crew Lead")
    s.action(t, "Assigned elsewhere", 2, responsible="Someone Else")
    s.question(t, "A question", 3)

    assert _texts(s.find(responsible="crew lead ")) == ["Assigned to crew"]


def test_chinese_query_ranks_chinese_item_first(db):
    s = Seed(db)
    t = s.topic(D1, "sid" + "a" * 32)
    s.action(t, "混凝土浇筑 二楼楼板", 1)
    s.action(t, "Order rebar for level two", 50)  # newer: wins on recency without a query
    s.finding(t, "脚手架 缺少护栏", 40)

    assert _texts(s.find())[0] == "Order rebar for level two"
    rows = s.find(query="二楼楼板浇筑")
    assert rows[0]["text"] == "混凝土浇筑 二楼楼板"
    assert rows[0]["score"] > 0
    assert "楼板" in rows[0]["matched_terms"]
    assert len(rows) == 3, "a query ranks, it never filters"
    assert all(r["score"] == 0.0 for r in rows[1:])


def test_tie_order_is_first_seen_desc_then_stable_id(db):
    s = Seed(db)
    t = s.topic(D1, "sid" + "a" * 32)
    a = s.action(t, "Tie one", 5)
    b = s.action(t, "Tie two", 5)
    c = s.action(t, "Newest", 9)
    d = s.action(t, "Oldest", 1)

    rows = s.find()
    ties = sorted([a, b], key=lambda r: str(r["stable_id"]))
    assert [r["stable_id"] for r in rows] == [c["stable_id"], ties[0]["stable_id"],
                                              ties[1]["stable_id"], d["stable_id"]]
    assert [r["stable_id"] for r in s.find(limit=2)] == [c["stable_id"],
                                                         ties[0]["stable_id"]]


def test_origin_from_live_rows_supported_by_link(db):
    s = Seed(db)
    base = "sid" + "a" * 32
    t1 = s.topic(D1, base)
    old = s.finding(t1, "Crack near column", 1)
    # Scored above the live link: a lookup by stable_id instead of by row would pick it.
    s.link("finding", old, "stale_stem", 10.0, score=0.99)
    s.supersede(t1)
    t2 = s.topic(D1, base)
    new = s.finding(t2, "Crack near the column", 30)
    s.carry("finding", new, old)
    new["stable_id"] = old["stable_id"]
    s.link("finding", new, "live_stem", 42.0)
    t3 = s.topic(D1, "sid" + "b" * 32)
    s.finding(t3, "No link here", 20)

    rows = {r["text"]: r for r in s.find(kinds=("finding",))}
    assert rows["Crack near the column"]["origin"] == {
        "source_stem": "live_stem", "start_sec": 42.0, "end_sec": 46.0,
        "speaker_label": "spk_0", "quote_status": "verified"}
    assert rows["No link here"]["origin"] is None


def test_deleted_topic_items_hidden(db):
    s = Seed(db)
    t_gone = s.topic(D1, "sid" + "a" * 32)
    s.action(t_gone, "Deleted item", 1)
    s.question(t_gone, "Deleted question", 2)
    t_kept = s.topic(D1, "sid" + "b" * 32)
    s.action(t_kept, "Kept item", 3)
    redactions.create_redaction(db, s.co["id"], t_gone["id"], "test delete", None, "admin",
                                scope="deleted")

    assert _texts(s.find()) == ["Kept item"]


def test_deleted_recording_hidden_by_source_prefix(db):
    s = Seed(db)
    t_gone = s.topic(D1, "sid" + "a" * 32)
    s.action(t_gone, "Deleted recording item", 1)
    t_kept = s.topic(D2, "sid" + "b" * 32)
    s.action(t_kept, "Kept item", 3)
    redactions.create_recording_tombstone(
        db, s.co["id"], f"extractions/{s.folder}/{D1}/sid{'a' * 32}", "test delete",
        None, "admin")

    assert _texts(s.find()) == ["Kept item"]


def test_deleted_first_pass_does_not_supply_first_seen(db):
    """A topic-level delete of the stale pass only: first_seen falls to the next
    undeleted row instead of naming deleted content's source."""
    s = Seed(db)
    t1 = s.topic(D1, "sid" + "a" * 32)
    old = s.action(t1, "Order rebar", 1)
    s.supersede(t1)
    t2 = s.topic(D2, "sid" + "b" * 32)
    new = s.action(t2, "Order the rebar", 30)
    s.carry("action_item", new, old)
    redactions.create_redaction(db, s.co["id"], t1["id"], "test delete", None, "admin",
                                scope="deleted")

    r = s.find()[0]
    assert r["first_seen"] == _ts(30)
    assert r["source"]["source_s3_key"] == t2["source_s3_key"]
