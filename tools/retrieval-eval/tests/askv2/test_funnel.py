import ask_orchestrator as orch
from llm_tools_client import ChatTurn

from askv2 import funnel
from askv2.harness import TurnRecord

D = "2026-09-02"
ITEM = {"id": "v1-01", "question": "What did we pour?", "date": D, "expect": "answerable",
        "evidence": [{"match_any": [r"\bslab pour\b"]}]}
CONTROL = {"id": "v1-20", "question": "Who won the cup?", "date": D, "expect": "unanswerable",
           "evidence": []}
EV = "we did the slab pour at nine"
LONG = "x" * 650 + " slab pour"


def chunk_row(cid, text, date=D):
    return {"id": cid, "report_date": date, "chunk_text": text, "chunk_type": "transcript",
            "site_name": "Alpha", "metadata": {}}


def chunk_item(ref, cid, text, date=D):
    return {"ref": ref, "kind": "chunk", "date": date, "site": "Alpha", "title": "Pour",
            "text": text[:600], "chunk_id": cid, "chunk_type": "transcript", "source": "s",
            "site_slug": None, "time_start": None}


def env(items, rnd=1, tool="search_records", meta=None):
    return {"tool": tool, "id": "0", "items": items, "meta": meta or {"truncated": False},
            "args": {"query": "pour"}, "round": rnd}


def final(text):
    return ChatTurn(tool_calls=[{"id": "f", "name": "final_answer",
                                 "arguments": {"answer": text, "basis": "records"}}])


def record(*, sql=(), rows=(), envs=(), answer=None, extra_judge=0, errors=()):
    """One turn with one round-1 batch and one judge request reading it."""
    rec = TurnRecord()
    rec.log.sql_rows = list(sql)
    rec.log.errors = list(errors)
    rec.batches = [{"calls": [{"name": "search_records", "args": {"query": "pour"}}],
                    "envelopes": [dict(e) for e in envs], "ms": 100.0,
                    "judge_calls_before": 0}]
    rec.invokes = [{"batch": 0, "ms": 50.0, "calls": [],
                    "results": [{"id": "0", "rows": list(rows), "meta": {}}]}]
    msgs = orch._messages("sys", "q", [list(envs)], False)
    for _ in range(extra_judge):
        rec.judge.append({"messages": msgs, "turn": ChatTurn(tool_calls=[]), "ms": 10.0})
    rec.judge.append({"messages": msgs, "turn": final(answer) if answer else None, "ms": 900.0})
    return rec


ROUTE = {"fallback": None, "records_late": False, "ms": {"judge": [900]}}


def test_all_stages_true_when_cited():
    rec = record(sql=[chunk_row("c1", EV)], rows=[chunk_row("c1", EV)],
                 envs=[env([chunk_item("R1", "c1", EV)])], answer="We poured [R1].")
    s = funnel.score_turn(ITEM, rec, ROUTE, {"grounded": True})
    assert s["stages"] == {"sql": True, "rows": True, "item": True, "seen": True,
                           "final_view": True, "cited": True}
    assert s["lost_at"] is None and s["status"] == "scored"
    assert s["rows_round1"] is True and s["rows_judge"] is False
    assert s["evidence_lines"] and len(s["evidence_lines"][0]) <= 160


def test_never_retrieved():
    rec = record(sql=[chunk_row("c2", "nothing here")], rows=[], envs=[env([])])
    s = funnel.score_turn(ITEM, rec, ROUTE, {})
    assert not any(s["stages"].values()) and s["lost_at"] == "never_retrieved"


def test_wrong_date_is_not_evidence():
    rec = record(sql=[chunk_row("c1", EV, date="2026-09-03")])
    assert funnel.score_turn(ITEM, rec, ROUTE, {})["stages"]["sql"] is False


def test_alias_normalised_row_counts_at_sql_not_rows():
    rec = record(sql=[chunk_row("c1", EV)], rows=[chunk_row("c1", "we did the slab poor")],
                 envs=[env([chunk_item("R1", "c1", "we did the slab poor")])])
    s = funnel.score_turn(ITEM, rec, ROUTE, {})
    assert s["stages"]["sql"] and not s["stages"]["rows"]
    assert s["lost_at"] == "dispatcher_dropped"


def test_text_cap():
    rec = record(sql=[chunk_row("c1", LONG)], rows=[chunk_row("c1", LONG)],
                 envs=[env([chunk_item("R1", "c1", LONG)])])
    assert funnel.score_turn(ITEM, rec, ROUTE, {})["lost_at"] == "cut_text_cap"


def test_post_search_cut():
    rec = record(sql=[chunk_row("c1", EV)], rows=[chunk_row("c1", EV), chunk_row("c2", "x")],
                 envs=[env([chunk_item("R1", "c2", "x")])])
    assert funnel.score_turn(ITEM, rec, ROUTE, {})["lost_at"] == "cut_post_search"


def test_render_budget_drop_uses_the_real_render(monkeypatch):
    monkeypatch.setattr(orch, "RENDER_MAX_CHARS", 200)
    filler = [chunk_item("R%d" % i, "f%d" % i, "filler text %d" % i) for i in range(1, 5)]
    envs = [env(filler + [chunk_item("R9", "c1", EV)])]
    rec = record(sql=[chunk_row("c1", EV)], rows=[chunk_row("c1", EV)], envs=envs)
    s = funnel.score_turn(ITEM, rec, ROUTE, {})
    assert s["stages"]["item"] and not s["stages"]["seen"]
    assert s["lost_at"] == "cut_render_budget"


def test_seen_then_aged_out_of_the_final_view():
    rec = record(sql=[chunk_row("c1", EV)], rows=[chunk_row("c1", EV)],
                 envs=[env([chunk_item("R1", "c1", EV)])], answer="Nothing.", extra_judge=1)
    rec.judge[-1]["messages"] = orch._messages("sys", "q", [[env([])]], False)
    s = funnel.score_turn(ITEM, rec, ROUTE, {})
    assert s["stages"]["seen"] and not s["stages"]["final_view"]
    assert s["lost_at"] == "out_of_final_view"


def test_seen_not_cited():
    rec = record(sql=[chunk_row("c1", EV)], rows=[chunk_row("c1", EV)],
                 envs=[env([chunk_item("R1", "c1", EV), chunk_item("R2", "c2", "x")])],
                 answer="Something else [R2].")
    assert funnel.score_turn(ITEM, rec, ROUTE, {})["lost_at"] == "seen_not_cited"


def test_text_only_final_and_a_range_cite():
    rec = record(sql=[chunk_row("c1", EV)], rows=[chunk_row("c1", EV)],
                 envs=[env([chunk_item("R1", "c2", "x"), chunk_item("R2", "c3", "y"),
                            chunk_item("R3", "c1", EV)])])
    rec.judge[-1]["turn"] = ChatTurn(text="It was poured [R1-R3].")
    assert funnel.score_turn(ITEM, rec, ROUTE, {})["stages"]["cited"] is True


def test_a_topic_item_matches_on_title_and_summary():
    topic_row = {"id": "t1", "report_date": D, "title": "Slab pour", "summary": "done"}
    item = {"ref": "R1", "kind": "topic", "date": D, "title": "Slab pour", "text": "done",
            "topic_id": "t1", "site": "Alpha", "source": None, "site_slug": None,
            "chunk_type": None, "time_start": None}
    rec = record(sql=[topic_row], rows=[topic_row], envs=[env([item], tool="day_overview")],
                 answer="Done [R1].")
    assert funnel.score_turn(ITEM, rec, ROUTE, {})["stages"]["cited"] is True


def test_no_synthesis_fallback_is_not_a_miss():
    rec = record(sql=[chunk_row("c1", EV)], rows=[chunk_row("c1", EV)],
                 envs=[env([chunk_item("R1", "c1", EV)])])
    rec.judge[-1]["turn"] = ChatTurn(error="http 500")
    s = funnel.score_turn(ITEM, rec, dict(ROUTE, fallback="judge_error+no_synthesis"),
                          {"answer": "", "error": "No answer"})
    assert s["status"] == "fallback" and s["fallback"] == "judge_error+no_synthesis"


def test_adapter_error_makes_the_turn_invalid():
    rec = record(errors=["BadRequestException: boom"])
    assert funnel.score_turn(ITEM, rec, ROUTE, {})["status"] == "invalid"


def test_timing_flags():
    rec = record(sql=[chunk_row("c1", EV)], envs=[env([], meta={"error": "timeout"})])
    s = funnel.score_turn(ITEM, rec, ROUTE, {})
    assert s["status"] == "timing_distorted" and "envelope_timeout" in s["flags"]
    rec = record()
    rec.judge[-1]["ms"] = 39_900.0
    rec.log.statements = [{"ms": 400.0}] * 4
    s = funnel.score_turn(ITEM, rec, dict(ROUTE, ms={"judge": [39_900]}), {})
    # 39,900 judge + 100 tools - 1,600 laptop SQL + 4 x 5 ms in-VPC = 38,420: under 40 s.
    assert s["timing"]["estimated_prod_ms"] == 38_420 and s["status"] == "scored"
    rec.log.statements = [{"ms": 1.0}] * 4
    s = funnel.score_turn(ITEM, rec, dict(ROUTE, ms={"judge": [39_900]}), {})
    assert "would_exceed_40s" in s["flags"]
    s = funnel.score_turn(ITEM, record(), dict(ROUTE, records_late=True), {})
    assert "records_late" in s["flags"]


def test_control_answered_with_citations():
    rec = record(envs=[env([chunk_item("R1", "c1", "x")])], answer="Someone [R1].")
    s = funnel.score_turn(CONTROL, rec, dict(ROUTE, basis="records"),
                          {"grounded": True, "citations": [{"snippet": "x"}]})
    assert s["control"] == {"grounded": True, "basis": "records", "citations": 1,
                            "answered_with_citations": True}
    assert s["stages"] is None and s["lost_at"] is None
