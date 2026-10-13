"""Unit tests for src/index_request.py -- the request the session search index is built from."""
import json

import pytest

ir = pytest.importorskip("index_request", reason="requires psycopg (installed in CI)")

from deleted_predicates import visible_topics_predicate
from session_index import build_chunks


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        return self

    def fetchall(self):
        return [{"id": i} for i in self.conn.ids]


class FakeConn:
    def __init__(self, ids=()):
        self.ids = list(ids)
        self.sql = []

    def cursor(self, row_factory=None):
        return _Cur(self)


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        if kw["Key"].startswith("lake_manifest/"):
            return
        self.puts.append(kw)


FULL = {
    "id": "t1", "title": "Door install", "summary": "Doors are going in.",
    "time_range": "10:00 – 10:05", "category": "progress", "participants": ["Ben"],
    "work_class": "work",
    "decisions": [{"decision": "Phase it", "rationale": "access", "decided_by": "Sam"}, "Plain"],
    "open_questions": ["Is 3604 a 150?"],
    "action_items": [
        {"text": "Order doors", "responsible": "Bob", "deadline": None, "deadline_text": "Friday"},
        {"text": "Book crane", "responsible": None, "deadline": "2026-07-10", "deadline_text": None},
    ],
    "findings": [{"observation": "Frame out of plumb", "severity": "minor", "domain": "quality"}],
}

KW = dict(site_id="s1", user_id="u1", owner_company_id="c1", report_date="2026-07-06",
          user_folder="Ben_X", session_base="Benl1_2026-07-06_10-00-00", tier="final",
          extracted_at="2026-07-06T10:05:00Z")
KEY = "extractions/Ben_X/2026-07-06/Benl1_2026-07-06_10-00-00.json"


def test_key_shapes():
    assert ir.request_key("2026-07-06", "Ben_X", "b") == "index_requests/2026-07-06/Ben_X/b.json"
    assert ir.vectors_key("2026-07-06", "Ben_X", "b") == "index_vectors/2026-07-06/Ben_X/b.json"


def test_live_topic_ids_filters_on_visibility():
    conn = FakeConn(["t1", "t2"])
    assert ir.live_topic_ids(conn, KEY) == ["t1", "t2"]
    sql, params = conn.sql[0]
    assert visible_topics_predicate("t") in sql
    assert params == (KEY,)


def test_build_maps_topic_to_chunk_builder_shape(monkeypatch):
    monkeypatch.setattr(ir.topics_repo, "get_topic_full", lambda conn, tid: dict(FULL, id=tid))
    req = ir.build(FakeConn(["t1"]), KEY, **KW)

    assert req["topics"] == [{
        "id": "t1", "title": "Door install", "summary": "Doors are going in.",
        "time_range": "10:00 – 10:05", "category": "progress", "participants": ["Ben"],
        "work_class": "work",
        "decisions": [{"decision": "Phase it", "rationale": "access"},
                      {"decision": "Plain", "rationale": ""}],
        "questions": [{"question": "Is 3604 a 150?"}],
        "action_items": [
            {"action": "Order doors", "responsible": "Bob", "deadline": "Friday"},
            {"action": "Book crane", "responsible": None, "deadline": "2026-07-10"}],
        "findings": [{"observation": "Frame out of plumb", "severity": "minor"}],
    }]
    assert req["members"] == [{"user_folder": "Ben_X", "date": "2026-07-06",
                               "session_base": "Benl1_2026-07-06_10-00-00"}]
    assert req["replaces"] == [] and req["member_bases"] == []
    assert req["owner_company_id"] == "c1" and req["tier"] == "final"
    json.dumps(req)  # serialisable as is

    # The consumer reads exactly these keys: every mapped field shows up in a chunk.
    text = "\n".join(c["chunk_text"] for c in build_chunks(req, []) if c["chunk_type"] == "topic")
    for needle in ("Door install", "Phase it — access", "Is 3604 a 150?", "Order doors",
                   "responsible: Bob", "due: Friday", "Frame out of plumb"):
        assert needle in text


def test_build_group_members_come_from_replaced_keys(monkeypatch):
    monkeypatch.setattr(ir.topics_repo, "get_topic_full", lambda conn, tid: dict(FULL, id=tid))
    replaces = ["extractions/Ben_X/2026-07-06/A_2026-07-06_10-00-00.json",
                "extractions/Sam_Y/2026-07-07/B_2026-07-07_00-10-00.json"]
    req = ir.build(FakeConn(["t1"]), "extractions/Ben_X/2026-07-06/grpG.json",
                   **{**KW, "tier": "group", "session_base": "grpG"},
                   member_bases=["A_2026-07-06_10-00-00", "B_2026-07-07_00-10-00"],
                   replaces=replaces)
    assert req["replaces"] == replaces
    assert req["members"] == [
        {"user_folder": "Ben_X", "date": "2026-07-06", "session_base": "A_2026-07-06_10-00-00"},
        {"user_folder": "Sam_Y", "date": "2026-07-07", "session_base": "B_2026-07-07_00-10-00"}]


def test_build_with_no_live_topics_is_still_a_request(monkeypatch):
    monkeypatch.setattr(ir.topics_repo, "get_topic_full", lambda *a: pytest.fail("no topics"))
    assert ir.build(FakeConn([]), KEY, **KW)["topics"] == []


def test_emit_writes_request_key_with_owner_hint():
    s3 = FakeS3()
    req = {"report_date": "2026-07-06", "user_folder": "Ben_X",
           "session_base": "Benl1_2026-07-06_10-00-00", "owner_company_id": "c1", "topics": []}
    key = ir.emit(s3, "bkt", req)
    assert key == "index_requests/2026-07-06/Ben_X/Benl1_2026-07-06_10-00-00.json"
    put = s3.puts[0]
    assert put["Key"] == key and put["Bucket"] == "bkt"
    assert json.loads(put["Body"]) == req
