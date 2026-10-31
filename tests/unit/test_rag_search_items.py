"""rag-search `mode: "items"` request handling and the scope builder it shares with Ask
(action packs Task 8). The SQL itself runs against real Postgres in
tests/integration/test_rag_search_items_sql.py."""
import pytest

rag = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")
from repositories import topics  # noqa: E402

# The Ask tools' source clause as it was before Task 8 -- byte for byte.
_ASK_SOURCE = ("(t.source_s3_key LIKE 'extractions/%%'"
               " OR (t.source_s3_key LIKE 'reports/%%' AND NOT "
               + topics._ask_tools_extraction_presence() + "))")


def test_ask_callers_get_the_unchanged_where():
    where, params = topics._ask_tools_where(["s1"], author_ids={"u1"}, site_id="s1",
                                            user_id="u1")
    assert where == " AND ".join([
        "t.site_id = ANY(%s::uuid[])", "t.work_class IS DISTINCT FROM 'non_work'",
        _ASK_SOURCE, "t.user_id = ANY(%s::uuid[])", "t.site_id = %s", "t.user_id = %s"])
    assert params == [["s1"], ["s1"], ["u1"], "s1", "u1"]
    assert "agent" not in where


def test_include_agent_widens_only_the_source_clause():
    plain, p1 = topics._ask_tools_where(["s1"], author_ids={"u1"}, site_id=None,
                                        user_id=None)
    agent, p2 = topics._ask_tools_where(["s1"], author_ids={"u1"}, site_id=None,
                                        user_id=None, include_agent=True)
    assert p1 == p2
    assert agent == plain.replace(_ASK_SOURCE,
                                  _ASK_SOURCE[:-1] + " OR t.source = 'agent')")


def test_bad_args_answer_before_any_connection(monkeypatch):
    def boom():
        raise AssertionError("no connection for a bad request")
    monkeypatch.setattr(rag, "get_cached_connection", boom)
    monkeypatch.setattr(rag, "close_cached_connection", lambda: None)
    res = rag.lambda_handler({"mode": "items", "sub": "s", "args": {"task_no": -1}}, None)
    assert res == {"rows": [], "meta": {"error": "bad_args", "field": "task_no"}}


def test_no_sub_is_not_provisioned(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda: object())
    monkeypatch.setattr(rag, "close_cached_connection", lambda: None)
    res = rag.lambda_handler({"mode": "items", "args": {}}, None)
    assert res == {"rows": [], "meta": {"error": "not_provisioned"}}


def test_empty_author_set_short_circuits_without_sql(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda: object())
    monkeypatch.setattr(rag, "close_cached_connection", lambda: None)
    monkeypatch.setattr(rag.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u", "company_id": "c",
                                           "global_role": "worker", "archived_at": None})
    monkeypatch.setattr(rag.tenancy_status, "caller_blocked", lambda *a, **k: None)
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s1"}, "author_ids": set()})
    monkeypatch.setattr(rag.topics, "scoped_items_rows",
                        lambda *a, **k: pytest.fail("no SQL for an empty author set"))
    res = rag.lambda_handler({"mode": "items", "sub": "x", "args": {}}, None)
    assert res["rows"] == [] and res["meta"]["empty_reason"] == "nothing_visible"


@pytest.mark.parametrize("deadline", [0, -1, "nan", "x"])
def test_no_time_left_answers_timeout_before_any_connection(monkeypatch, deadline):
    def boom():
        raise AssertionError("no connection without time")
    monkeypatch.setattr(rag, "get_cached_connection", boom)
    monkeypatch.setattr(rag, "close_cached_connection", lambda: None)
    res = rag.lambda_handler({"mode": "items", "sub": "s", "deadline_s": deadline,
                              "args": {}}, None)
    assert res == {"rows": [], "meta": {"error": "timeout"}}


def test_deadline_becomes_the_reads_statement_timeout(monkeypatch):
    seen = {}
    monkeypatch.setattr(rag, "get_cached_connection", lambda: object())
    monkeypatch.setattr(rag, "close_cached_connection", lambda: None)
    monkeypatch.setattr(rag.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u", "company_id": "c",
                                           "global_role": "gm", "archived_at": None})
    monkeypatch.setattr(rag.tenancy_status, "caller_blocked", lambda *a, **k: None)
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s1"}, "author_ids": None})
    monkeypatch.setattr(rag.topics, "scoped_items_rows",
                        lambda *a, **k: seen.update(k) or [])
    for raw, ms in ((None, 20000), (2.5, 2500), (99, 20000)):
        ev = {"mode": "items", "sub": "x", "args": {}}
        if raw is not None:
            ev["deadline_s"] = raw
        rag.lambda_handler(ev, None)
        assert seen["timeout_ms"] == ms


def test_items_sql_carries_get_topic_visible_hide_rule():
    """One hide rule, two readers: get_topic_visible and the items tool."""
    hide = topics._topic_not_hidden("t")
    assert hide in topics._TOPIC_VISIBLE_SQL
    assert "reverted_at IS NULL" in hide and "non_work" in hide
