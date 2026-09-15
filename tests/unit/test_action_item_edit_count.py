"""Unit: a to-do's version is 1 + its content_edits rows (todo-card spec §3.4 / §8.1).

The repository counts; the serializer (render_report_shape) turns the count into
`version`. These tests pin the repository half: one batched query, over the
SURVIVORS of the collapse only, and no query at all when there is nothing to count.
FakeConn records SQL and never parses it -- the real-SQL half lives in
tests/integration/test_action_item_edit_counts.py.
"""
import pytest

from tests.unit.test_topics_repo import FakeConn

topics = pytest.importorskip("repositories.topics", reason="requires psycopg (installed in CI)")

PREFIX = "extractions/Ada_L/2026-09-01/"


def _topic(tid):
    return {"id": tid, "site_id": "s-1", "user_id": "u-1",
            "source_s3_key": PREFIX + "sidAAA.json", "report_date": "2026-09-01",
            "occurred_at": None, "category": "progress", "title": tid, "summary": "s",
            "time_range": None, "participants": [], "source": "ai", "created_at": "c0",
            "site_name": "Alpha", "user_name": "Ada L"}


def _item(aid, tid, text="Order timber", created_at=1):
    return {"id": aid, "topic_id": tid, "text": text, "responsible": None,
            "deadline": None, "deadline_text": None, "priority": None,
            "status": "open", "created_at": created_at}


def _edit_calls(conn):
    return [c for c in conn.calls if "content_edits" in c["sql"]]


def test_an_unedited_item_counts_zero(monkeypatch):
    monkeypatch.delenv("ENABLE_TODO_COLLAPSE", raising=False)
    conn = FakeConn(results=[[_topic("t-1")], [_item("a-1", "t-1")], [], [], []])
    rows = topics.list_topics_for_date(conn, ["s-1"], "2026-09-01")
    assert rows[0]["action_items"][0]["edit_count"] == 0


def test_three_edits_count_three_in_one_batched_query(monkeypatch):
    monkeypatch.delenv("ENABLE_TODO_COLLAPSE", raising=False)
    items = [_item("a-1", "t-1"), _item("a-2", "t-1", text="Book pump"),
             _item("a-3", "t-1", text="Call engineer")]
    conn = FakeConn(results=[
        [_topic("t-1")],
        items,
        [{"row_id": "a-2", "n": 3}],   # content_edits counts
        [], [], [],                    # safety, findings, photos
    ])
    rows = topics.list_topics_for_source_prefix(conn, PREFIX)
    by_id = {a["id"]: a for a in rows[0]["action_items"]}
    assert by_id["a-2"]["edit_count"] == 3
    assert by_id["a-1"]["edit_count"] == 0
    calls = _edit_calls(conn)
    assert len(calls) == 1, "the count must be ONE query for all items, never per item"
    sql = calls[0]["sql"]
    assert "table_name = 'action_items'" in sql
    assert "row_id = ANY(%s::uuid[])" in sql
    assert "GROUP BY row_id" in sql
    assert "company_id" not in sql     # deliberate: see spec §8.1
    assert calls[0]["params"] == (["a-1", "a-2", "a-3"],)


def test_a_collapsed_pair_counts_the_survivor_only(monkeypatch):
    """Two recordings, two topics, one commitment. History is per row_id, so the
    chip must show the survivor's count -- the collapsed row's edits are not
    reachable from the card that is shown."""
    monkeypatch.setenv("ENABLE_TODO_COLLAPSE", "true")
    conn = FakeConn(results=[
        [_topic("t-1"), _topic("t-2")],
        [_item("a-1", "t-1", created_at=1), _item("a-2", "t-2", created_at=2)],
        [{"row_id": "a-1", "n": 2}],
        [], [], [],
    ])
    rows = topics.list_topics_for_source_prefix(conn, PREFIX)
    survivor = rows[0]["action_items"][0]
    assert survivor["id"] == "a-1" and survivor["collapsed_ids"] == ["a-2"]
    assert survivor["edit_count"] == 2
    assert _edit_calls(conn)[0]["params"] == (["a-1"],), "a collapsed id was counted"


def test_no_surviving_items_issues_no_count_query(monkeypatch):
    monkeypatch.delenv("ENABLE_TODO_COLLAPSE", raising=False)
    conn = FakeConn(results=[[_topic("t-1")], [], [], []])
    topics.list_topics_for_date(conn, ["s-1"], "2026-09-01")
    assert _edit_calls(conn) == []
    assert len(conn.calls) == 4      # main + action_items + safety + findings


def test_get_topic_full_does_not_count(monkeypatch):
    """Reindex's read. It embeds text; a version has no meaning there."""
    monkeypatch.delenv("ENABLE_TODO_COLLAPSE", raising=False)
    conn = FakeConn(results=[[_topic("t-1")], [_item("a-1", "t-1")], [], [], []])
    full = topics.get_topic_full(conn, "t-1")
    assert _edit_calls(conn) == []
    assert "edit_count" not in full["action_items"][0]
