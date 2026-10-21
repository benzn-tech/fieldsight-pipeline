#!/usr/bin/env python3
"""Retrieval-eval ruler, Ask v2 mode: what the model-driven Ask loop puts in front of
the judge, and what it cites.

Spec docs/2026-10-22-ask-v2-mode-spec.md as amended by -review.md; plan -plan.md.
The real `ask_orchestrator.answer` runs in this process with the real `ask_tools` and the
real rag-search handler/dispatcher/repositories. Only transport is substituted: the
rag-search Lambda is a function call, and its Aurora connection is the read-only Data API
adapter (askv2/adapter.py). Nothing here is imported by a Lambda.

Per question it scores the funnel sql -> rows -> item -> seen -> final_view -> cited
(askv2/funnel.py) and records the tool calls the judge chose.
"""
import secrets

from askv2 import adapter, funnel

TZ = "Pacific/Auckland"


def _calls_by_round(rec):
    """The tool calls of each batch, round 1 (no judge call before it) first."""
    out, rnd = [], 1
    for b in rec.batches:
        if b["judge_calls_before"] > 0:
            rnd = b["judge_calls_before"] + 1
        out.append({"round": rnd, "calls": [{"name": c.get("name"), "args": c.get("args")}
                                            for c in b["calls"]]})
    return out


def _judge_summary(rec):
    out = []
    for j in rec.judge:
        turn = j.get("turn")
        out.append({"tool_choice": j.get("tool_choice") if isinstance(j.get("tool_choice"), str)
                    else "final_answer",
                    "tool_calls": [{"name": tc.get("name"), "args": tc.get("arguments")}
                                   for tc in (turn.tool_calls if turn else []) or []
                                   if tc.get("name") != "final_answer"],
                    "final": bool(turn and any(tc.get("name") == "final_answer"
                                               for tc in turn.tool_calls or [])),
                    "usage": j.get("usage"), "model": j.get("model"), "error": j.get("error"),
                    "ms": j.get("ms")})
    return out


def run_turn(item, *, ev, chat, proxy, capture, now, caller_sub, scope_site=None,
             keep_renders=None):
    """One question through `ask_orchestrator.answer`, as `_work_screen` builds the body
    (spec §4): question, tz, pinned now, caller_sub; `site_id` under --scope-site."""
    import ask_orchestrator
    rec = ev.next()
    turn_id = secrets.token_hex(8)
    body = {"question": item["question"], "tz": TZ, "now": now, "caller_sub": caller_sub}
    if scope_site:
        body["site_id"] = scope_site
    try:
        result = ask_orchestrator.answer(body, channel="screen", on_state=rec.states.append,
                                         tools=proxy, chat=chat, turn_id=turn_id)
    finally:
        adapter.rollback_open()          # review B3.4: nothing stays open past a turn
    route = capture.routes.get(turn_id) or {}
    score = funnel.score_turn(item, rec, route, result)
    final = next((j["turn"] for j in reversed(rec.judge) if j.get("turn") is not None), None)
    fin = ask_orchestrator._final_of(final) if final is not None else None
    if keep_renders is not None:
        keep_renders.append({"id": item["id"], "turn": turn_id,
                             "messages": [j["messages"] for j in rec.judge]})
    stmt_ms = sorted(st["ms"] for st in rec.log.statements)
    return {
        "id": item["id"], "expect": item.get("expect"), "turn": turn_id, "funnel": score,
        "route": route, "states": list(rec.states),
        "tool_calls": _calls_by_round(rec), "judge": _judge_summary(rec),
        "answer": result.get("answer"), "final_raw": fin.get("answer") if isinstance(fin, dict) else None,
        "citations": len(result.get("citations") or []), "grounded": result.get("grounded"),
        "error": result.get("error"),
        "sql": {"statements": len(stmt_ms), "ms_total": round(sum(stmt_ms), 1),
                "ms_max": stmt_ms[-1] if stmt_ms else None,
                "transactions": rec.log.transactions, "errors": list(rec.log.errors),
                "warnings": sorted(set(rec.log.warnings)),
                "largest_response": rec.log.largest_response,
                "templates": sorted({(st["sha256"], st["head"]) for st in rec.log.statements})},
    }
