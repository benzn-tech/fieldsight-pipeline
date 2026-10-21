"""One Ask v2 turn end to end, offline: the real orchestrator, ask_tools, rag-search
handler, dispatcher and repositories, over the read-only adapter and a scripted rds-data
client. Only the judge (ScriptChat) and the embedding are fakes."""
import json

import pytest
import urllib3
from llm_tools_client import ChatTurn

import dashscope_utils
import run_ask_v2
from askv2 import adapter, harness
from fakes import FakeRdsData, col, result, s

USER = "f531eb99-d79c-4e6c-bac1-ff944b802349"
COMPANY = "11111111-1111-4111-8111-111111111111"
SITE = "4326dbc7-14a2-4251-8381-9d0067595a85"
CHUNK = "22222222-2222-4222-8222-222222222222"
D = "2026-09-02"
ITEM = {"id": "v1-01", "question": "What did we pour on level two?", "date": D,
        "expect": "answerable", "evidence": [{"match_any": [r"\bslab pour\b"]}]}

ROUTES = [
    (r"WHERE users\.cognito_sub=", result(
        [col("id", "uuid"), col("company_id", "uuid"), col("global_role", "text"),
         col("folder_name", "text")], [[s(USER), s(COMPANY), s("gm"), s("Ben_Lin")]])),
    (r"FROM sites WHERE company_id=", result([col("id", "uuid"), col("name", "text")],
                                             [[s(SITE), s("Alpha")]])),
    (r"lexical_hit", result(
        [col("id", "uuid"), col("site_id", "uuid"), col("report_date", "date"),
         col("chunk_type", "text"), col("chunk_text", "text"), col("metadata", "jsonb"),
         col("site_name", "text"), col("source_s3_key", "text"), col("distance", "float8"),
         col("lexical_hit", "bool")],
        [[s(CHUNK), s(SITE), s(D), s("transcript"), s("we did the slab pour on level two"),
          s('{"window_span": "09:00-09:05"}'), s("Alpha"), s("reports/x.json"),
          {"doubleValue": 0.2}, {"booleanValue": False}]])),
]


class ScriptChat:
    def __init__(self, *turns):
        self.turns, self.calls = list(turns), []

    def __call__(self, messages, tools, **kw):
        self.calls.append(kw)
        return self.turns.pop(0)


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts: [[0.25] * 1024 for _ in texts])
    client = FakeRdsData(ROUTES)
    ev = harness.EvalTurns()
    fake = harness.FakeLambda(ev)
    factory = lambda: adapter.DataApiConnection(client, cluster="c", secret="s",  # noqa: E731
                                                database="fieldsight_test", log=ev.rec.log)
    patches = harness.install_patches(fake, factory)
    cap = harness.install_log_capture()
    sent = []
    monkeypatch.setattr(urllib3.connectionpool.HTTPConnectionPool, "urlopen",
                        lambda self, *a, **k: sent.append(self.host))
    guard = harness.arm_url_guard(set())
    try:
        yield dict(client=client, ev=ev, fake=fake, cap=cap, guard=guard, sent=sent,
                   proxy=harness.ToolsProxy(ev, fake))
    finally:
        guard.restore()
        cap.restore()
        patches.restore()


def test_one_turn_reaches_cited_through_the_real_stack(wired):
    w = wired
    judge = ScriptChat(ChatTurn(tool_calls=[{"id": "f", "name": "final_answer", "arguments": {
        "answer": "We did the slab pour on level two [R1].", "basis": "records"}}],
        usage={"prompt_tokens": 1500, "completion_tokens": 40}, model="judge-m"))
    out = run_ask_v2.run_turn(ITEM, ev=w["ev"], chat=harness.chat_wrapper(w["ev"], judge),
                              proxy=w["proxy"], capture=w["cap"], now="2026-10-22T09:00:00+13:00",
                              caller_sub="sub-ben", scope_site=SITE)
    f = out["funnel"]
    assert f["stages"] == {"sql": True, "rows": True, "item": True, "seen": True,
                           "final_view": True, "cited": True}
    assert f["status"] == "scored" and f["lost_at"] is None
    # The route came from the ASK_ROUTE log line of this turn.
    assert out["route"]["turn"] == out["turn"] and out["route"]["fallback"] is None
    assert out["route"]["policy"] == "never"
    assert out["tool_calls"][0] == {"round": 1, "calls": [
        {"name": "search_records", "args": {"query": ITEM["question"], "site": SITE}}]}
    assert out["judge"][0]["usage"] == {"prompt": 1500, "completion": 40, "reasoning": None}
    assert out["answer"].startswith("We did the slab pour") and out["citations"] == 1
    # One Data API transaction for the one invoke: read-only first, rolled back last.
    methods = [m for m, _ in w["client"].calls]
    assert methods.count("begin_transaction") == 1 == methods.count("rollback_transaction")
    assert methods[0] == "begin_transaction" and methods[-1] == "rollback_transaction"
    assert w["client"].statements()[:2] == ["SET TRANSACTION READ ONLY",
                                            "SHOW transaction_read_only"]
    assert "commit" not in json.dumps(w["client"].calls).lower()
    assert w["sent"] == [] and w["guard"].counts == {}
    assert out["sql"]["statements"] >= 4 and out["sql"]["errors"] == []
    assert "caller_sub" not in json.dumps(out) and "sub-ben" not in json.dumps(out)


def test_a_sql_error_makes_the_turn_invalid_not_a_miss(wired):
    w = wired
    w["client"].routes.insert(0, (r"lexical_hit", lambda sql: (_ for _ in ()).throw(
        RuntimeError("ERROR: column t.superseded_at does not exist"))))
    judge = ScriptChat(ChatTurn(tool_calls=[{"id": "f", "name": "final_answer", "arguments": {
        "answer": "Nothing recorded.", "basis": "none"}}]))
    out = run_ask_v2.run_turn(ITEM, ev=w["ev"], chat=harness.chat_wrapper(w["ev"], judge),
                              proxy=w["proxy"], capture=w["cap"], now="2026-10-22T09:00:00+13:00",
                              caller_sub="sub-ben")
    assert out["funnel"]["status"] == "invalid"
    assert "superseded_at" in out["sql"]["errors"][0]
