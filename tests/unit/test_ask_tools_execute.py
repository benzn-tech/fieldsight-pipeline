"""ask_tools.execute: embed, one batched rag-search invoke, post-search, refs,
output budget (spec 2026-10-15-ask-retrieval-tools §3.2-§3.4)."""
import io
import json
from datetime import date

import pytest

import ask_tools


class FakeLambda:
    def __init__(self, results, function_error=None):
        self.results, self.events, self.function_error = results, [], function_error

    def invoke(self, FunctionName, InvocationType, Payload):
        ev = json.loads(Payload)
        self.events.append(ev)
        out = {"Payload": io.BytesIO(json.dumps({"results": self.results(ev)}).encode())}
        if self.function_error:
            out["FunctionError"] = self.function_error
        return out


def _echo(rows=(), meta=None):
    return lambda ev: [{"id": c["id"], "name": c["name"], "rows": list(rows),
                        "meta": dict(meta or {})} for c in ev["calls"]]


def _size(items):
    """The budget's unit: the tool result as the orchestrator renders it (unicode, not
    escaped -- a CJK character is one character, not six)."""
    return len(json.dumps(items, ensure_ascii=False))


def _ctx(fake, today=date(2026, 10, 15), deadline=1030.0, refs=None):
    kw = {} if refs is None else {"refs": refs}
    return ask_tools.ToolContext(caller_sub="sub-1", today=today, deadline=deadline,
                                 lambda_client=fake, function_name="rag",
                                 clock=lambda: 1000.0, **kw)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(ask_tools, "_embed", lambda texts: [[0.0] * 4 for _ in texts])
    monkeypatch.setattr(ask_tools, "_post_search", lambda q, rows, k: rows[:k])


# --- refs ------------------------------------------------------------------

def test_ref_assigner_shares_counters_per_prefix():
    r = ask_tools.RefAssigner()
    assert r.assign("topic", "t1") == "R1"
    assert r.assign("web", "https://a", prefix="W") == "W1"
    assert r.assign("chunk", "c1") == "R2"
    assert r.assign("topic", "t1") == "R1"


def test_ref_assigner_w_and_r_are_independent():
    r = ask_tools.RefAssigner()
    assert [r.assign("web", f"https://{i}", prefix="W") for i in range(3)] == ["W1", "W2", "W3"]
    assert r.assign("topic", "t1") == "R1"           # W traffic did not advance R
    assert r.assign("web", "https://0", prefix="W") == "W1"
    assert r.assign("web", "https://new", prefix="W") == "W4"


def test_one_ref_assigner_spans_execute_calls_and_the_web_tool():
    rows = [{"id": "t1", "title": "Pour"}]
    refs = ask_tools.RefAssigner()
    fake = FakeLambda(_echo(rows))
    refs.assign("web", "https://x", prefix="W")
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}],
                            _ctx(fake, refs=refs))
    assert env[0]["items"][0]["ref"] == "R1"


# --- identity / args -------------------------------------------------------

def test_identity_injected_and_args_cannot_override():
    fake = FakeLambda(_echo())
    env = ask_tools.execute([{"name": "day_overview",
                              "args": {"date_from": "yesterday", "sub": "evil"}}], _ctx(fake))
    assert env[0]["meta"] == {"error": "bad_args", "field": "sub"}
    assert env[0]["items"] == []
    assert fake.events == []                          # nothing valid: no invoke at all
    ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}], _ctx(fake))
    assert fake.events[-1]["mode"] == "tools"
    assert fake.events[-1]["sub"] == "sub-1"
    assert fake.events[-1]["calls"][0]["args"]["date_from"] == "2026-10-14"


def test_latest_activity_on_or_before_defaults_to_callers_today():
    fake = FakeLambda(_echo())
    ask_tools.execute([{"name": "latest_activity", "args": {}}], _ctx(fake))
    assert fake.events[-1]["calls"][0]["args"]["on_or_before"] == "2026-10-15"
    ask_tools.execute([{"name": "latest_activity", "args": {"on_or_before": "2026-10-01"}}],
                      _ctx(fake))
    assert fake.events[-1]["calls"][0]["args"]["on_or_before"] == "2026-10-01"
    ask_tools.execute([{"name": "latest_activity", "args": {}}], _ctx(fake, today=None))
    assert "on_or_before" not in fake.events[-1]["calls"][0]["args"]


def test_every_search_shaped_call_carries_an_embedding_and_others_do_not():
    seen = []
    fake = FakeLambda(_echo())
    ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}},
                       {"name": "latest_activity", "args": {"query": "crane"}},
                       {"name": "latest_activity", "args": {}},
                       {"name": "day_overview", "args": {"date_from": "today"}}], _ctx(fake))
    for c in fake.events[-1]["calls"]:
        seen.append((c["name"], "query_embedding" in c))
    assert seen == [("search_records", True), ("latest_activity", True),
                    ("latest_activity", False), ("day_overview", False)]


def test_clamped_moves_from_args_to_meta_and_range_is_reported():
    fake = FakeLambda(_echo())
    env = ask_tools.execute([{"name": "day_overview",
                              "args": {"date_from": "2026-10-01", "date_to": "2026-10-15"}}],
                            _ctx(fake))
    sent = fake.events[-1]["calls"][0]["args"]
    assert "clamped" not in sent
    assert env[0]["meta"]["clamped"]["max_days"] == 7
    assert env[0]["meta"]["range"] == {"from": "2026-10-09", "to": "2026-10-15"}


def test_more_than_four_calls_rejects_the_extras_only():
    fake = FakeLambda(_echo([], {"empty_reason": "nothing_in_range"}))
    calls = [{"name": "day_overview", "args": {"date_from": "today"}}] * 5
    env = ask_tools.execute(calls, _ctx(fake))
    assert len(env) == 5
    assert len(fake.events[-1]["calls"]) == 4
    assert env[4]["meta"] == {"error": "bad_args", "field": "calls"}
    assert all("error" not in e["meta"] for e in env[:4])


# --- items / refs ----------------------------------------------------------

def test_refs_unique_across_calls_and_deduped_by_kind_and_id():
    rows = [{"id": "t1", "title": "Pour", "summary": "B2", "report_date": "2026-10-14",
             "site_name": "S1", "user_name": "Ben", "time_range": "09:00 - 09:40"}]
    fake = FakeLambda(_echo(rows))
    ctx = _ctx(fake)
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}},
                             {"name": "latest_activity", "args": {}}], ctx)
    assert env[0]["items"][0]["ref"] == "R1"
    assert env[1]["items"][0]["ref"] == "R1"            # same topic, same ref
    item = env[0]["items"][0]
    assert (item["kind"], item["who"], item["time"], item["topic_id"]) == \
        ("topic", "Ben", "09:00 - 09:40", "t1")
    people = FakeLambda(_echo([{"person_ref": "p1", "name": "Ben Lin"}]))
    env2 = ask_tools.execute([{"name": "find_people", "args": {"name": "Ben"}}],
                             _ctx(people, refs=ctx.refs))
    assert env2[0]["items"][0]["ref"] == "R2"
    assert env2[0]["items"][0]["kind"] == "person"
    assert env2[0]["items"][0]["person_ref"] == "p1"


def test_topic_and_chunk_with_same_id_are_different_items():
    def results(ev):
        out = []
        for c in ev["calls"]:
            if c["name"] == "day_overview":
                out.append({"id": c["id"], "name": c["name"], "rows": [{"id": "x", "title": "T"}],
                            "meta": {}})
            else:
                out.append({"id": c["id"], "name": c["name"],
                            "rows": [{"id": "x", "chunk_text": "c", "topic_id": "x"}], "meta": {}})
        return out
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}},
                             {"name": "search_records", "args": {"query": "pour"}}],
                            _ctx(FakeLambda(results)))
    assert env[0]["items"][0]["ref"] != env[1]["items"][0]["ref"]
    assert env[1]["items"][0]["kind"] == "chunk"


def test_two_metrics_in_one_turn_get_two_refs():
    def results(ev):
        return [{"id": c["id"], "name": c["name"],
                 "rows": [{"metric": c["args"]["metric"], "value": 1}], "meta": {}}
                for c in ev["calls"]]
    env = ask_tools.execute([{"name": "count_metric",
                              "args": {"metric": "duration", "date_from": "today"}},
                             {"name": "count_metric",
                              "args": {"metric": "count_photos", "date_from": "today"}}],
                            _ctx(FakeLambda(results)))
    assert [e["items"][0]["kind"] for e in env] == ["metric", "metric"]
    assert env[0]["items"][0]["ref"] != env[1]["items"][0]["ref"]
    assert "count_photos" in env[1]["items"][0]["text"]


def test_chunk_item_carries_every_field_build_citations_reads():
    """The orchestrator maps an item back to a citation card without re-fetching:
    every key `_build_citations` reads off a chunk must be on the item (spec §3.2)."""
    import lambda_ask_agent as laa
    chunk = {"id": "c1", "chunk_text": "z" * 900, "chunk_type": "transcript_window",
             "topic_id": "t1", "source_s3_key": "extractions/Ben/2026-10-14/s.json",
             "metadata": {"window_span": "12:42:59–12:45:10"}, "report_date": "2026-10-14",
             "site_name": "Riverside", "site_slug": "riverside", "topic_title": "Pour",
             "distance": 0.2, "lexical_hit": False}
    env = ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}}],
                            _ctx(FakeLambda(_echo([chunk]))))
    item = env[0]["items"][0]
    want = laa._build_citations([chunk])[0]
    mapping = {"source_s3_key": "source", "report_date": "date", "site_name": "site",
               "site_slug": "site_slug", "topic_title": "title", "chunk_type": "chunk_type",
               "time_start": "time_start"}
    assert set(want) == set(mapping) | {"snippet"}     # _build_citations grew a field: map it
    for src, dst in mapping.items():
        assert item[dst] == want[src], dst
    assert item["text"][:200] == want["snippet"]
    assert item["time_start"] == "12:42:59"
    assert item["time"] == "12:42:59–12:45:10"
    assert (item["chunk_id"], item["topic_id"]) == ("c1", "t1")


def test_sparse_chunk_still_has_the_citation_keys():
    env = ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}}],
                            _ctx(FakeLambda(_echo([{"id": "c1", "chunk_text": "t"}]))))
    item = env[0]["items"][0]
    for key in ("source", "date", "site", "site_slug", "title", "chunk_type", "text",
                "time_start"):
        assert key in item, key


def test_text_capped_at_600():
    fake = FakeLambda(_echo([{"id": "t", "title": "T", "summary": "y" * 900}]))
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}],
                            _ctx(fake))
    assert len(env[0]["items"][0]["text"]) == 600


# --- post-search -----------------------------------------------------------

def test_post_search_runs_for_search_and_latest_with_query_only(monkeypatch):
    seen = []
    monkeypatch.setattr(ask_tools, "_post_search",
                        lambda q, rows, k: seen.append((q, k)) or rows[:k])
    rows = [{"id": f"c{i}", "chunk_text": "x"} for i in range(20)]
    env = ask_tools.execute([{"name": "search_records", "args": {"query": "pour", "k": 3}},
                             {"name": "latest_activity", "args": {"query": "crane"}},
                             {"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(FakeLambda(_echo(rows))))
    assert seen == [("pour", 3), ("crane", 8)]
    assert len(env[0]["items"]) == 3


def test_post_search_failure_falls_back_to_first_k(monkeypatch):
    def boom(q, rows, k):
        raise ValueError("rerank down")
    monkeypatch.setattr(ask_tools, "_post_search", boom)
    rows = [{"id": f"c{i}", "chunk_text": "x"} for i in range(20)]
    env = ask_tools.execute([{"name": "search_records", "args": {"query": "pour", "k": 5}}],
                            _ctx(FakeLambda(_echo(rows))))
    assert [i["chunk_id"] for i in env[0]["items"]] == [f"c{i}" for i in range(5)]


def test_real_post_search_keeps_a_keyword_only_hit(monkeypatch):
    """The unpatched path: lambda_ask_agent's admit/rerank chain, rerank off."""
    monkeypatch.undo()
    monkeypatch.setattr(ask_tools, "_embed", lambda texts: [[0.0] * 4 for _ in texts])
    import lambda_ask_agent as laa
    monkeypatch.setattr(laa, "RERANK_ENABLED", False)
    rows = [{"id": f"v{i}", "chunk_text": "x", "lexical_hit": False} for i in range(10)]
    rows.append({"id": "lex", "chunk_text": "x", "lexical_hit": True})
    env = ask_tools.execute([{"name": "search_records", "args": {"query": "B2-14", "k": 3}}],
                            _ctx(FakeLambda(_echo(rows))))
    assert [i["chunk_id"] for i in env[0]["items"]] == ["v0", "v1", "lex"]


# --- budget ----------------------------------------------------------------

def test_output_budget_truncates_and_flags():
    big = [{"id": f"t{i}", "title": "T", "summary": "x" * 600} for i in range(40)]
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}],
                            _ctx(FakeLambda(_echo(big))))
    assert env[0]["meta"]["truncated"] is True
    assert 0 < len(env[0]["items"]) < 40
    assert _size(env[0]["items"]) <= ask_tools.MAX_RESULT_CHARS


def test_budget_holds_on_the_serialised_list_at_every_item_size():
    """The budget is on json.dumps(items), separators included: many small items
    are where per-item sums undercount."""
    for n in range(0, 400, 13):
        rows = [{"id": f"t{i}", "summary": "s" * n} for i in range(400)]
        env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                                _ctx(FakeLambda(_echo(rows))))
        assert _size(env[0]["items"]) <= ask_tools.MAX_RESULT_CHARS, n
        assert env[0]["meta"]["truncated"] is True


def test_dispatcher_truncation_survives_when_under_budget():
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}],
                            _ctx(FakeLambda(_echo([{"id": "t1", "title": "T"}],
                                                  {"truncated": True, "ms": 3}))))
    assert env[0]["meta"]["truncated"] is True
    assert env[0]["meta"]["ms"] == 3


def test_empty_result_keeps_empty_reason_and_flags_not_truncated():
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}],
                            _ctx(FakeLambda(_echo([], {"empty_reason": "nothing_in_range",
                                                       "latest_before": {"date": "2026-10-12"}}))))
    assert env[0]["items"] == []
    assert env[0]["meta"]["empty_reason"] == "nothing_in_range"
    assert env[0]["meta"]["truncated"] is False


# --- failures --------------------------------------------------------------

def test_invoke_failure_is_upstream_for_every_call_never_raises():
    class Broken:
        def invoke(self, **k): raise RuntimeError("down")
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}},
                             {"name": "find_people", "args": {"name": "Ben"}}],
                            _ctx(Broken()))
    assert [e["meta"]["error"] for e in env] == ["upstream", "upstream"]


def test_function_error_is_upstream_for_every_call():
    fake = FakeLambda(_echo([{"id": "t1", "title": "T"}]), function_error="Unhandled")
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}},
                             {"name": "find_people", "args": {"name": "Ben"}}], _ctx(fake))
    assert [e["meta"].get("error") for e in env] == ["upstream", "upstream"]
    assert all(e["items"] == [] for e in env)


def test_malformed_payload_is_upstream():
    class Garbage:
        def invoke(self, **k):
            return {"Payload": io.BytesIO(b"not json")}
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(Garbage()))
    assert env[0]["meta"]["error"] == "upstream"


def test_embed_failure_only_fails_the_calls_that_needed_it(monkeypatch):
    def boom(texts):
        raise RuntimeError("dashscope down")
    monkeypatch.setattr(ask_tools, "_embed", boom)
    fake = FakeLambda(_echo([{"id": "t1", "title": "T"}]))
    env = ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}},
                             {"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(fake))
    assert env[0]["meta"]["error"] == "upstream"
    assert "error" not in env[1]["meta"]
    assert [c["name"] for c in fake.events[-1]["calls"]] == ["day_overview"]


def test_deadline_passed_down_and_capped_at_20():
    fake = FakeLambda(_echo())
    ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}], _ctx(fake))
    assert fake.events[-1]["deadline_s"] == 20
    ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}],
                      _ctx(fake, deadline=1007.5))
    assert fake.events[-1]["deadline_s"] == 7.5



def test_never_raises_on_garbage_calls():
    env = ask_tools.execute([None, "x", {"name": 3}, {"name": "day_overview", "args": []}],
                            _ctx(FakeLambda(_echo())))
    assert [e["meta"]["error"] for e in env] == ["bad_args"] * 4


def test_missing_result_for_a_call_is_upstream():
    fake = FakeLambda(lambda ev: [])
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(fake))
    assert env[0]["meta"]["error"] == "upstream"


# --- fix round 1 -----------------------------------------------------------

def test_expired_deadline_times_out_every_call_without_embed_or_invoke(monkeypatch):
    embedded = []
    monkeypatch.setattr(ask_tools, "_embed", lambda texts: embedded.append(texts) or [[0.0]])

    class Untouchable:
        def __init__(self):
            self.calls = 0

        def invoke(self, **k):
            self.calls += 1
            raise AssertionError("invoked on an expired deadline")
    for deadline in (1000.0, 990.0):
        client = Untouchable()
        env = ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}},
                                 {"name": "day_overview", "args": {"date_from": "today"}},
                                 {"name": "day_overview", "args": {"sub": "x"}}],
                                _ctx(client, deadline=deadline))
        assert client.calls == 0
        assert [e["meta"].get("error") for e in env] == ["timeout", "timeout", "bad_args"]
        assert all(e["items"] == [] for e in env)
    assert embedded == []


def test_a_sliver_of_deadline_left_still_invokes():
    fake = FakeLambda(_echo())
    ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                      _ctx(fake, deadline=1000.25))
    assert fake.events[-1]["deadline_s"] == 0.25


ZH = ("今天上午在二号楼三层完成了楼板混凝土浇筑，泵车九点到场，浇筑持续约两个小时。"
      "现场工程师检查了钢筋保护层厚度和预埋件位置，发现东侧两处保护层不足，已要求整改后再浇。"
      "下午安排养护覆盖，明天上午复查表面裂缝情况并记录。")


def test_chinese_text_is_budgeted_by_characters_not_escapes():
    rows = [{"id": f"t{i}", "title": f"二号楼三层楼板浇筑 {i}", "summary": ZH * 2,
             "site_name": "滨江二期", "user_name": "林工", "report_date": "2026-10-14",
             "time_range": "09:00 - 11:00"} for i in range(20)]
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(FakeLambda(_echo(rows))))
    items = env[0]["items"]
    assert len(items) >= 10, len(items)        # an escaped (ascii) budget fits only 2-3
    assert _size(items) <= ask_tools.MAX_RESULT_CHARS
    assert items[0]["text"].startswith("今天上午")


def test_a_malformed_row_is_coerced_or_skipped_never_the_whole_turn():
    def results(ev):
        out = []
        for c in ev["calls"]:
            if c["name"] == "day_overview":
                rows = [{"id": "t1", "title": 7, "summary": 12345},
                        "not a row", None,
                        {"id": "t2", "title": "ok", "summary": {"weird": 1}},
                        {"id": "t3", "title": "fine", "summary": "fine"}]
            elif c["name"] == "search_records":
                rows = [{"id": "c1", "chunk_text": 99, "metadata": 5, "report_date": 20261014}]
            else:
                rows = [{"person_ref": "p1", "name": ["Ben"]}]
            out.append({"id": c["id"], "name": c["name"], "rows": rows, "meta": {}})
        return out
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}},
                             {"name": "search_records", "args": {"query": "pour"}},
                             {"name": "find_people", "args": {"name": "Ben"}}],
                            _ctx(FakeLambda(results)))
    assert all("error" not in e["meta"] for e in env)
    assert env[0]["items"][0]["text"] == "12345"
    assert [i["topic_id"] for i in env[0]["items"]] == ["t1", "t2", "t3"]
    assert env[1]["items"][0]["text"] == "99"
    assert len(env[2]["items"]) == 1


def test_a_row_that_breaks_item_building_is_skipped(monkeypatch):
    real = ask_tools._item

    def flaky(name, args, row, call_id):
        if row.get("id") == "bad":
            raise TypeError("boom")
        return real(name, args, row, call_id)
    monkeypatch.setattr(ask_tools, "_item", flaky)
    rows = [{"id": "bad", "title": "x"}, {"id": "good", "title": "y"}]
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(FakeLambda(_echo(rows))))
    assert [i["topic_id"] for i in env[0]["items"]] == ["good"]
    assert "error" not in env[0]["meta"]


def test_rows_without_an_id_get_their_own_refs():
    rows = [{"title": "a"}, {"title": "b"}, {"id": None, "title": "c"}]
    refs = ask_tools.RefAssigner()
    fake = FakeLambda(_echo(rows))
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(fake, refs=refs))
    assert [i["ref"] for i in env[0]["items"]] == ["R1", "R2", "R3"]
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(fake, refs=refs))
    assert [i["ref"] for i in env[0]["items"]] == ["R4", "R5", "R6"]   # never deduped
    people = FakeLambda(_echo([{"name": "A"}, {"name": "B"}]))
    env = ask_tools.execute([{"name": "find_people", "args": {"name": "x"}}],
                            _ctx(people, refs=refs))
    assert [i["ref"] for i in env[0]["items"]] == ["R7", "R8"]


def test_a_duplicate_row_inside_one_envelope_is_emitted_once():
    rows = [{"id": "t1", "title": "a"}, {"id": "t2", "title": "b"}, {"id": "t1", "title": "a"}]
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}},
                             {"name": "day_overview", "args": {"date_from": "yesterday"}}],
                            _ctx(FakeLambda(_echo(rows))))
    assert [i["ref"] for i in env[0]["items"]] == ["R1", "R2"]
    assert [i["ref"] for i in env[1]["items"]] == ["R1", "R2"]   # across calls: first ref kept


@pytest.mark.parametrize("result", [
    {"id": "0"}, {"id": "0", "rows": None, "meta": None}, {"id": "0", "rows": [], "meta": {}},
    {"id": "0", "rows": "x", "meta": "y"}, {"id": "0", "rows": [], "meta": {"ms": 4}}])
def test_a_malformed_result_with_no_rows_and_no_meta_is_upstream(result):
    fake = FakeLambda(lambda ev: [result])
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(fake))
    assert env[0]["meta"]["error"] == "upstream"


def test_an_honest_empty_result_is_not_upstream():
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "today"}}],
                            _ctx(FakeLambda(_echo([], {"empty_reason": "nothing_in_range"}))))
    assert "error" not in env[0]["meta"]


def test_time_start_import_failure_is_logged_once(monkeypatch, caplog):
    import sys
    monkeypatch.setattr(ask_tools, "_TIME_START_WARNED", False)
    monkeypatch.setitem(sys.modules, "lambda_ask_agent", None)     # import -> ImportError
    rows = [{"id": f"c{i}", "chunk_text": "x", "chunk_type": "transcript_window",
             "metadata": {"window_span": "12:00:00–12:01:00"}} for i in range(3)]
    with caplog.at_level("WARNING"):
        env = ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}},
                                 {"name": "search_records", "args": {"query": "slab"}}],
                                _ctx(FakeLambda(_echo(rows))))
    assert env[0]["items"][0]["time_start"] is None
    lines = [r for r in caplog.records if "time_start" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelname == "WARNING"
    assert lines[0].getMessage().endswith(("ImportError", "ModuleNotFoundError"))


def test_logs_carry_arg_shape_and_caller_hash_never_values(caplog):
    rows = [{"id": "t1", "title": "Secret Title", "summary": "secret summary"}]
    with caplog.at_level("INFO"):
        ask_tools.execute([{"name": "search_records",
                            "args": {"query": "Mike Tane slab", "date_from": "2026-10-10",
                                     "date_to": "2026-10-14", "person": "Mike"}}],
                          _ctx(FakeLambda(_echo(rows))))
    text = caplog.text
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("ask tool search_records"))
    assert "fields=date_from,date_to,person,query" in line
    assert "days=5" in line
    assert f"caller={ask_tools.sub_hash('sub-1')}" in line
    for secret in ("Mike", "Tane", "slab", "Secret", "secret", "sub-1"):
        assert secret not in text, secret


def test_dispatcher_and_executor_share_one_log_helper():
    d = pytest.importorskip("rag_tools_dispatch", reason="requires psycopg (installed in CI)")
    assert d._sub_hash is ask_tools.sub_hash
    assert d._arg_shape is ask_tools.arg_shape


@pytest.mark.parametrize("row,want", [
    ({"metric": "duration", "value": 5400, "unit": "seconds"}, "duration=5400 seconds"),
    ({"metric": "count_photos", "value": 0, "notes": {"zero_kind": "no_rows_for_that_day"}},
     "count_photos=0 (no_rows_for_that_day)"),
    ({"value": 3, "unit": "sessions", "notes": {"zero_kind": None}, "extra": "x" * 900},
     "count_sessions=3 sessions"),
    ({"value": None}, "count_sessions=unknown"),
])
def test_metric_text_is_a_compact_readable_string(row, want):
    metric = row.get("metric", "count_sessions")
    env = ask_tools.execute([{"name": "count_metric",
                              "args": {"metric": metric, "date_from": "today"}}],
                            _ctx(FakeLambda(_echo([row]))))
    item = env[0]["items"][0]
    assert item["text"] == want
    assert item["metric"] == metric


def test_candidate_k_rides_on_search_shaped_calls_when_rerank_is_on(monkeypatch):
    monkeypatch.setenv("ENABLE_RERANK", "true")
    monkeypatch.setenv("RERANK_CANDIDATE_K", "40")
    fake = FakeLambda(_echo())
    ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}},
                       {"name": "latest_activity", "args": {"query": "crane"}},
                       {"name": "latest_activity", "args": {}},
                       {"name": "day_overview", "args": {"date_from": "today"}}], _ctx(fake))
    got = [c.get("candidate_k") for c in fake.events[-1]["calls"]]
    assert got == [40, 40, None, None]


def test_candidate_k_default_and_rerank_off_match_rag_answer(monkeypatch):
    import lambda_ask_agent as laa
    monkeypatch.setenv("ENABLE_RERANK", "true")
    monkeypatch.delenv("RERANK_CANDIDATE_K", raising=False)
    fake = FakeLambda(_echo())
    ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}}], _ctx(fake))
    # the same env var and default as lambda_ask_agent.RERANK_CANDIDATES
    assert fake.events[-1]["calls"][0]["candidate_k"] == 32
    assert 'os.environ.get("RERANK_CANDIDATE_K", "32")' in open(laa.__file__, encoding="utf-8").read()
    monkeypatch.setenv("ENABLE_RERANK", "false")
    ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}}], _ctx(fake))
    assert "candidate_k" not in fake.events[-1]["calls"][0]
    monkeypatch.setenv("ENABLE_RERANK", "true")
    monkeypatch.setenv("RERANK_CANDIDATE_K", "junk")
    ask_tools.execute([{"name": "search_records", "args": {"query": "pour"}}], _ctx(fake))
    assert "candidate_k" not in fake.events[-1]["calls"][0]
