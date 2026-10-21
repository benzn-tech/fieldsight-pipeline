"""Unit: the open-time re-check of stored Ask answers (spec 2026-10-22 §6, D5).

The SQL runs against a fake connection that answers by which table a statement
reads; tests/integration/test_ask_thread_check_sql.py runs it for real."""
import logging

import pytest

import ask_thread_check as atc
from repositories import scope, search_sql, topics

SITE = "11111111-1111-1111-1111-111111111111"
OTHER_SITE = "22222222-2222-2222-2222-222222222222"
ME = "33333333-3333-3333-3333-333333333333"
DOC = "44444444-4444-4444-4444-444444444444"
KEY = "extractions/ben/2026-10-20/sid" + "a" * 32
KEY2 = "extractions/ben/2026-10-20/sid" + "b" * 32
DAY_KEY = "reports/2026-10-20/ben/daily_report.json"
CALLER = {"id": ME, "company_id": "c-1", "global_role": "admin"}


class FakeConn:
    """Answers each statement by the table it reads. `live` = keys with a visible
    chunk, `tomb` = keys a recording tombstone covers, `docs` = live document ids
    in a visible site, `day_tombs` = {(folder, date): [prefix, ...]}."""

    def __init__(self, live=(), tomb=(), docs=(), day_tombs=None, fail=None):
        self.live, self.tomb, self.docs = set(live), set(tomb), set(docs)
        self.day_tombs = day_tombs or {}
        self.fail = fail
        self.calls = []

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail and self.fail in sql:
            raise RuntimeError("db down")
        if "FROM report_chunks" in sql:
            rows = [{"source_s3_key": k} for k in params["keys"] if k in self.live]
        elif "unnest" in sql:
            rows = [{"source_s3_key": k} for k in params["keys"] if k in self.tomb]
        elif "project_documents" in sql:
            rows = [{"id": d} for d in params["ids"] if d in self.docs
                    and SITE in params["site_ids"]]
        elif "FROM redactions" in sql:
            like = params[0]
            rows = [{"target_key": p} for (folder, date), ps in self.day_tombs.items()
                    if like == f"%/{folder}/{date}/%" for p in ps]
        else:
            raise AssertionError("unexpected SQL: " + sql)
        return _Rows(rows)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


@pytest.fixture(autouse=True)
def visible(monkeypatch):
    sc = {"site_ids": {SITE}, "author_ids": None}
    monkeypatch.setattr(scope, "visible_scope", lambda conn, caller: sc)
    return sc


def turn(tid, keys=(), docs=(), days=(), topics_=()):
    return {"turn_id": tid, "question": "q", "at": 1.0, "result": {"answer": "a"},
            "sources": {"record_keys": list(keys),
                        "documents": [{"document_id": d, "site_id": SITE} for d in docs],
                        "report_days": [{"folder": f, "report_date": d} for f, d in days],
                        "topic_ids": list(topics_)}}


def test_live_key_is_shown():
    conn = FakeConn(live={KEY})
    assert atc.withheld_turns(conn, CALLER, [turn("t1", [KEY])]) == set()


def test_tombstoned_key_withheld():
    # Even when a chunk row is still there, a tombstone on the key withholds.
    conn = FakeConn(live={KEY, KEY2}, tomb={KEY})
    out = atc.withheld_turns(conn, CALLER, [turn("t1", [KEY]), turn("t2", [KEY2])])
    assert out == {"t1"}


def test_key_without_visible_live_chunk_withheld():
    conn = FakeConn(live={KEY})
    out = atc.withheld_turns(conn, CALLER, [turn("t1", [KEY, KEY2]), turn("t2", [KEY])])
    assert out == {"t1"}


def test_live_key_query_uses_the_search_visibility_predicate(visible):
    visible["author_ids"] = {ME}
    conn = FakeConn(live={KEY})
    atc.withheld_turns(conn, CALLER, [turn("t1", [KEY])])
    (sql, params), = [c for c in conn.calls if "FROM report_chunks" in c[0]]
    assert search_sql._visibility_predicate("c", "TRUE") in sql
    assert params["site_ids"] == [SITE] and params["source_family"] is None
    assert params["keys"] == [KEY]


def test_author_restricted_caller_needs_own_author(visible):
    visible["author_ids"] = {ME}
    conn = FakeConn(live={KEY})
    atc.withheld_turns(conn, CALLER, [turn("t1", [KEY])])
    (_, params), = [c for c in conn.calls if "FROM report_chunks" in c[0]]
    assert params["author_ids"] == [ME]
    visible["author_ids"] = None
    conn = FakeConn(live={KEY})
    atc.withheld_turns(conn, CALLER, [turn("t1", [KEY])])
    (_, params), = [c for c in conn.calls if "FROM report_chunks" in c[0]]
    assert params["author_ids"] is None


def test_no_visible_sites_withholds_every_record_turn(visible):
    visible["site_ids"] = set()
    conn = FakeConn(live={KEY}, docs={DOC})
    out = atc.withheld_turns(conn, CALLER, [turn("t1", [KEY]), turn("t2", docs=[DOC]),
                                            turn("t3")])
    assert out == {"t1", "t2"}


def test_document_gone_or_out_of_scope_withheld(visible):
    conn = FakeConn(docs={DOC})
    gone = "55555555-5555-5555-5555-555555555555"
    out = atc.withheld_turns(conn, CALLER, [turn("t1", docs=[DOC]), turn("t2", docs=[gone]),
                                            turn("t3", docs=["not-a-uuid"])])
    assert out == {"t2", "t3"}
    visible["site_ids"] = {OTHER_SITE}
    conn = FakeConn(docs={DOC})
    assert atc.withheld_turns(conn, CALLER, [turn("t1", docs=[DOC])]) == {"t1"}


def test_report_day_with_any_tombstone_withheld():
    conn = FakeConn(live={DAY_KEY},
                    day_tombs={("ben", "2026-10-20"): ["extractions/ben/2026-10-20/sid" + "c" * 32]})
    out = atc.withheld_turns(conn, CALLER, [
        turn("t1", [DAY_KEY], days=[("ben", "2026-10-20")]),
        turn("t2", [DAY_KEY], days=[("ben", "2026-10-21")]),
    ])
    assert out == {"t1"}


def test_report_days_are_checked_once_per_distinct_day():
    conn = FakeConn(live={DAY_KEY})
    atc.withheld_turns(conn, CALLER, [turn("t%d" % i, [DAY_KEY], days=[("ben", "2026-10-20")])
                                      for i in range(5)])
    assert len([c for c in conn.calls if c[0].startswith("SELECT DISTINCT target_key")]) == 1
    assert len([c for c in conn.calls if "FROM report_chunks" in c[0]]) == 1


def test_null_key_skipped():
    conn = FakeConn(live={KEY})
    t = turn("t1", [KEY])
    t["sources"]["record_keys"] += [None, ""]
    assert atc.withheld_turns(conn, CALLER, [t]) == set()
    (_, params), = [c for c in conn.calls if "FROM report_chunks" in c[0]]
    assert params["keys"] == [KEY]


def test_web_only_turn_never_withheld():
    conn = FakeConn(fail="SELECT")
    assert atc.withheld_turns(conn, CALLER, [turn("t1")]) == set()
    assert conn.calls == []


def test_turn_without_a_manifest_is_withheld():
    t = turn("t1")
    del t["sources"]
    assert atc.withheld_turns(FakeConn(), CALLER, [t]) == {"t1"}


@pytest.mark.parametrize("fail", ["FROM report_chunks", "unnest", "project_documents",
                                  "FROM redactions"])
def test_db_error_fails_closed(fail, caplog):
    conn = FakeConn(live={KEY, DAY_KEY}, docs={DOC}, fail=fail)
    caplog.set_level(logging.INFO)
    out = atc.withheld_turns(conn, CALLER, [turn("t1", [KEY]), turn("t2", docs=[DOC]),
                                            turn("t3", [DAY_KEY], days=[("ben", "2026-10-20")]),
                                            turn("t4")])
    assert out == {"t1", "t2", "t3"}
    assert "RuntimeError" in caplog.text


def test_scope_error_fails_closed(monkeypatch):
    def boom(conn, caller):
        raise RuntimeError("x")
    monkeypatch.setattr(scope, "visible_scope", boom)
    assert atc.withheld_turns(FakeConn(), CALLER, [turn("t1", [KEY]), turn("t2")]) == {"t1"}


def test_check_logs_no_keys_or_ids(caplog):
    caplog.set_level(logging.DEBUG)
    atc.withheld_turns(FakeConn(), CALLER, [turn("t1", [KEY], docs=[DOC],
                                                 days=[("ben", "2026-10-20")])])
    assert KEY not in caplog.text and DOC not in caplog.text and "ben" not in caplog.text



# ------------------------------------------------------------ topic rule

TOPIC = "77777777-7777-7777-7777-777777777777"
GONE_TOPIC = "88888888-8888-8888-8888-888888888888"


@pytest.fixture
def topic_reads(monkeypatch):
    seen = []

    def visible(conn, topic_id, site_ids, author_ids):
        seen.append((topic_id, list(site_ids), author_ids))
        return {"id": topic_id} if topic_id == TOPIC else None

    monkeypatch.setattr(topics, "get_topic_visible", visible)
    return seen


def test_topic_gone_or_invisible_withheld(topic_reads):
    out = atc.withheld_turns(FakeConn(), CALLER, [turn("t1", topics_=[TOPIC]),
                                                  turn("t2", topics_=[GONE_TOPIC]),
                                                  turn("t3", topics_=[TOPIC, GONE_TOPIC])])
    assert out == {"t2", "t3"}


def test_topic_rule_uses_the_pinned_path_read_with_the_callers_scope(topic_reads, visible):
    visible["author_ids"] = {ME}
    atc.withheld_turns(FakeConn(), CALLER, [turn("t%d" % i, topics_=[TOPIC]) for i in range(3)])
    # Once per distinct topic, with the caller's resolved reach.
    assert topic_reads == [(TOPIC, [SITE], [ME])]


def test_topic_with_no_visible_sites_withheld(topic_reads, visible):
    visible["site_ids"] = set()
    assert atc.withheld_turns(FakeConn(), CALLER, [turn("t1", topics_=[TOPIC])]) == {"t1"}


def test_topic_read_error_fails_closed(monkeypatch):
    def boom(conn, topic_id, site_ids, author_ids):
        raise RuntimeError("db down")
    monkeypatch.setattr(topics, "get_topic_visible", boom)
    assert atc.withheld_turns(FakeConn(), CALLER, [turn("t1", topics_=[TOPIC]),
                                                   turn("t2")]) == {"t1"}


def test_manifest_without_topic_ids_still_reads(topic_reads):
    t = turn("t1", [KEY])
    del t["sources"]["topic_ids"]
    assert atc.withheld_turns(FakeConn(live={KEY}), CALLER, [t]) == set()
