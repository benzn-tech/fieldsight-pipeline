"""Pure scoring over what one Ask v2 turn recorded (spec §1, review S1/S2/S10)."""
import re


def classify_question(question, today):
    """The round-1 shape the real orchestrator will pick for `question` (no model,
    no SQL), computed with the production modules themselves (review S6). The
    metric fast path fires only for a dated question (`ask_orchestrator._answer`)."""
    import ask_orchestrator
    import lexical_terms
    import metric_slots
    import query_slots
    q_from, _ = query_slots.time_range(question, today)
    latest = bool(ask_orchestrator._LATEST.search(question))
    requirements = bool(ask_orchestrator._REQUIREMENTS.search(question))
    if q_from:
        shape = "day+search"
    elif latest:
        shape = "latest"
    else:
        shape = "search"
    return {"dated": bool(q_from), "latest": latest, "requirements": requirements,
            "metric_fast_path": bool(q_from) and bool(metric_slots.detect(question)),
            "subject_terms": list(lexical_terms.subject_terms(question)),
            "round1": shape,
            "round1_with_documents": shape + "+documents" if requirements else shape}


# --- the funnel --------------------------------------------------------------

STAGES = ("sql", "rows", "item", "seen", "final_view", "cited")
EXCERPT = 160
IN_VPC_STMT_MS = 5            # review N6: a statement's in-VPC cost
PROD_SCREEN_MS = 40_000       # ask_orchestrator.DEADLINE_S["screen"] in prod


def _patterns(item):
    """run.py's rule: any `match_any` regex, case-insensitive (not imported: run.py
    exports and imports production modules at import time)."""
    return [re.compile(p, re.I) for ev in item.get("evidence", []) for p in ev["match_any"]]


def _row_text(row):
    """The record text a row carries: a chunk's text, or a topic's title + summary."""
    if "chunk_text" in row:
        return row.get("chunk_text") or ""
    return " ".join(str(row.get(k) or "") for k in ("title", "summary"))


def _item_text(it):
    if it.get("kind") == "topic":
        return " ".join(str(it.get(k) or "") for k in ("title", "text"))
    return it.get("text") or ""


def _item_id(it):
    return str(it.get("chunk_id") if it.get("kind") == "chunk" else it.get("topic_id"))


def _tool_lines(messages):
    """(date field, text after it) for every item line of every `tool` message."""
    out = []
    for m in messages or []:
        if m.get("role") != "tool":
            continue
        for line in (m.get("content") or "").splitlines():
            parts = line.split(" | ")
            if len(parts) >= 3 and re.match(r"[RD]\d+$", parts[0]):
                out.append((parts[1], " | ".join(parts[2:]), line))
    return out


def score_turn(item, rec, route, result):
    """One turn's funnel (spec §1, review S1/S2/S10/N6). `rec` is a harness.TurnRecord,
    `route` the turn's ASK_ROUTE dict, `result` what `answer()` returned."""
    import ask_orchestrator
    route = route or {}
    result = result or {}
    pats = _patterns(item)
    day = item.get("date")

    def hit(text, date):
        return str(date or "")[:10] == day and any(p.search(text or "") for p in pats)

    envelopes = [e for b in rec.batches for e in (b.get("envelopes") or [])]
    items = [it for e in envelopes for it in e.get("items") or []
             if it.get("kind") in ("chunk", "topic")]
    round1 = {i for i, b in enumerate(rec.batches) if b.get("judge_calls_before") == 0}
    rows = [(inv.get("batch") in round1, r) for inv in rec.invokes
            for res in inv.get("results") or [] for r in res.get("rows") or []
            if isinstance(r, dict) and "report_date" in r]
    final_turn = next((j["turn"] for j in reversed(rec.judge) if j.get("turn") is not None), None)
    fin = ask_orchestrator._final_of(final_turn) if final_turn is not None else None
    cited_refs = (ask_orchestrator._cited_card_refs(fin["answer"], envelopes)
                  if isinstance(fin, dict) else set())

    out = {"stages": None, "lost_at": None, "rows_round1": None, "rows_judge": None,
           "evidence_lines": [], "control": None}
    if item.get("expect") == "answerable":
        hit_rows = [(r1, r) for r1, r in rows if hit(_row_text(r), r.get("report_date"))]
        hit_items = [it for it in items if hit(_item_text(it), it.get("date"))]
        seen_lines = [ln for j in rec.judge for d, text, ln in _tool_lines(j.get("messages"))
                      if d.startswith(day) and any(p.search(text) for p in pats)]
        last = rec.judge[-1]["messages"] if rec.judge else []
        stages = {
            "sql": any(hit(_row_text(r), r.get("report_date")) for r in rec.log.sql_rows),
            "rows": bool(hit_rows),
            "item": bool(hit_items),
            "seen": bool(seen_lines),
            "final_view": any(d.startswith(day) and any(p.search(t) for p in pats)
                              for d, t, _ in _tool_lines(last)),
            "cited": any(it.get("ref") in cited_refs for it in hit_items),
        }
        out.update(stages=stages, rows_round1=any(r1 for r1, _ in hit_rows),
                   rows_judge=any(not r1 for r1, _ in hit_rows),
                   evidence_lines=sorted({ln[:EXCERPT] for ln in seen_lines}))
        first_false = next((s for s in STAGES if not stages[s]), None)
        if first_false == "sql":
            out["lost_at"] = "never_retrieved"
        elif first_false == "rows":
            out["lost_at"] = "dispatcher_dropped"
        elif first_false == "item":
            # The row reached ask_tools; its own item exists but no longer carries the
            # words: they were past MAX_TEXT. No item at all: the post-search k cut or
            # the 10k result budget.
            ids = {_item_id(it) for it in items}
            capped = any(str(r.get("id")) in ids for _, r in hit_rows)
            out["lost_at"] = "cut_600_cap" if capped else "cut_post_search"
        elif first_false == "seen":
            out["lost_at"] = "cut_render_budget"
        elif first_false == "final_view":
            out["lost_at"] = "out_of_final_view"
        elif first_false == "cited":
            out["lost_at"] = "seen_not_cited"
    else:
        citations = len(result.get("citations") or [])
        out["control"] = {"grounded": bool(result.get("grounded")), "basis": route.get("basis"),
                          "citations": citations, "answered_with_citations": citations > 0}

    # Status (review S2): any fallback means the funnel did not finish; an adapter
    # error makes the turn invalid, never a miss.
    judge_ms = sum(j.get("ms") or 0 for j in rec.judge)
    tools_ms = sum(b.get("ms") or 0 for b in rec.batches)
    sql_ms = sum(s.get("ms") or 0 for s in rec.log.statements)
    n_stmt = len(rec.log.statements)
    # Review N6: take the laptop's SQL time out and put an in-VPC allowance back.
    est = int(round(judge_ms + tools_ms - sql_ms + IN_VPC_STMT_MS * n_stmt))
    flags = []
    if route.get("records_late"):
        flags.append("records_late")
    if any((e.get("meta") or {}).get("error") == "timeout" for e in envelopes):
        flags.append("envelope_timeout")
    if est > PROD_SCREEN_MS:
        flags.append("would_exceed_40s")
    if rec.log.errors:
        status = "invalid"
    elif route.get("fallback") is not None:
        status = "fallback"
    elif flags:
        status = "timing_distorted"
    else:
        status = "scored"
    out.update(status=status, fallback=route.get("fallback"), flags=flags,
               timing={"judge_ms": round(judge_ms), "tools_ms": round(tools_ms),
                       "sql_ms": round(sql_ms), "statements": n_stmt,
                       "estimated_prod_ms": est})
    return out
