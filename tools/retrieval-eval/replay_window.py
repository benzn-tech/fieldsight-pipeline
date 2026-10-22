#!/usr/bin/env python3
"""Offline replay of the Ask tools item window over a `run_ask_v2.py --keep-rows` dump.

Spec docs/superpowers/specs/2026-10-22-ask-tools-item-window.md §6.1, as amended by its
review (BLOCKER 2, SHOULD 1/2, NIT 1); plan Tasks 7 and 10. No model, no Data API, no
network: every envelope is rebuilt from the rows it was built from in the live run
(`post_search`: after the rerank and the k cut), with the imported `ask_tools._envelope`
at the given window and result budget, and the whole turn is rendered once through
`ask_orchestrator._budgeted`, as the turn's last judge call sees it. The tool calls are
held fixed: a turn that would have ended earlier is the live ruler's to see.

One process per src ref (two `ask_tools` versions never share one process: their lazy
imports resolve from sys.path), so an arm is one run of this script:

    python tools/retrieval-eval/replay_window.py --rows <scratch>/rows-before-session.json \\
        --gold tools/retrieval-eval/gold/v1.1.jsonl --family session \\
        --src-ref 5608895a --window 600 --result-chars 10000 \\
        --results tools/retrieval-eval/results/<before-session>.json \\
        --out <scratch>/replay-5608895a-600.json
    python tools/retrieval-eval/replay_window.py ... --src-ref fix/ask-tools-item-window \\
        --window 1000 --result-chars 12500 --out <scratch>/replay-branch-1000.json
    python tools/retrieval-eval/replay_window.py --compare A.json B.json

`--window` sets `ask_tools.WINDOW_CHARS` when the src has an item window, else
`MAX_TEXT` (the head rule at that size; it also caps topic rows). The output holds no
record text, only ids, flags and counts; it still belongs in scratch, not results/.

`--dump-envelopes PATH` also writes one JSONL line per replayed turn: the envelopes
`ask_orchestrator._messages` flattens, grouped by round, with the items as built, plus
for a multi-call turn (null otherwise) the item lines byte-identical to an earlier one
by (earlier tool -> later tool), `len(render(flat))` with the budget lifted, and the
items `_budgeted` drops -- all from the imported renderer. It holds record text: scratch
only, never results/.
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_RERANKED = ("search_records", "latest_activity")
_SCORED_KINDS = ("chunk", "topic")
_OLD_LABELS = {"cut_600_cap": "cut_text_cap"}


def _p90(values):
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]


def _p95(values):
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def _configure(ask_tools, window, result_chars):
    """Set the arm's constants; returns what to restore."""
    saved = {k: getattr(ask_tools, k) for k in ("WINDOW_CHARS", "MAX_TEXT", "MAX_RESULT_CHARS")
             if hasattr(ask_tools, k)}
    if window:
        setattr(ask_tools, "WINDOW_CHARS" if "WINDOW_CHARS" in saved else "MAX_TEXT", window)
    if result_chars:
        ask_tools.MAX_RESULT_CHARS = result_chars
    return saved


def _envelope_rows(name, ids, result):
    """The rows `_envelope` got in the live run. Reranked calls: the recorded ids mapped
    into the dispatcher's pool. Anything else got the dispatcher's rows (documents:
    rows[:k], which is the recorded count). Returns (rows, ids not found)."""
    pool = [r for r in (result or {}).get("rows") or [] if isinstance(r, dict)]
    if name not in _RERANKED:
        return pool[:len(ids)], 0
    by_id = {}
    for r in pool:
        by_id.setdefault(r.get("id"), r)
    rows = [by_id[i] for i in ids if i in by_id]
    return rows, len(ids) - len(rows)


def _rebuild_turn(rec, ask_tools):
    """Every envelope of one turn, in batch and call order, tagged with `args` and
    `round` as the orchestrator tags them. Returns (envelopes, rebuilt, unreplayed)."""
    ctx = ask_tools.ToolContext(caller_sub="replay", today=None, deadline=0.0,
                                lambda_client=None, function_name="replay",
                                refs=ask_tools.RefAssigner())
    posts = {(p.get("batch"), str(p.get("call_id"))): p for p in rec.get("post_search") or []}
    results = {}
    for inv in rec.get("invokes") or []:
        for r in inv.get("results") or []:
            if isinstance(r, dict):
                results[(inv.get("batch"), str(r.get("id")))] = r
    envs, rebuilt, unreplayed, rnd = [], [], 0, 1
    for bi, b in enumerate(rec.get("batches") or []):
        if b.get("judge_calls_before", 0) > 0:
            rnd = b["judge_calls_before"] + 1
        calls = b.get("calls") or []
        for e in b.get("envelopes") or []:
            cid = str(e.get("id"))
            post = posts.get((bi, cid))
            if post is None:
                if e.get("items"):
                    unreplayed += 1                     # items with no recorded rows
                env = {"tool": e.get("tool"), "id": cid, "items": [],
                       "meta": dict(e.get("meta") or {})}
            else:
                result = results.get((bi, cid))
                rows, missing = _envelope_rows(post["name"], post.get("ids") or [], result)
                unreplayed += missing
                meta = {k: v for k, v in ((result or {}).get("meta") or {}).items()
                        if k not in ("truncated", "windowed", "head")}
                for k in ("clamped", "range"):
                    if k in (e.get("meta") or {}):
                        meta[k] = e["meta"][k]
                env = ask_tools._envelope(post["name"], cid, post.get("args") or {}, rows,
                                          meta, ctx)
                rebuilt.append(env)
            i = int(cid) if cid.isdigit() else -1
            if 0 <= i < len(calls) and isinstance(calls[i], dict):
                env["args"] = dict(calls[i].get("args") or {})
            env["round"] = rnd
            envs.append(env)
    return envs, rebuilt, unreplayed


def _score_turn(rec, gold, envs, rebuilt, ask_tools, ask_orchestrator):
    from askv2 import funnel
    pats = funnel._patterns(gold) if gold else []
    day = (gold or {}).get("date") or rec.get("date")
    items = [it for e in envs for it in e.get("items") or []]
    blocks = ask_orchestrator._budgeted(envs)
    dropped, ev_dropped, shown_lines = 0, False, []
    for env, block in zip(envs, blocks):
        lines = [ask_orchestrator._item_line(it) for it in env.get("items") or []]
        shown = block[1:]
        shown_lines.extend(shown)
        dropped += len(lines) - len(shown)
        gone = lines[len(shown):]
        if any(funnel.hit(pats, day, text, d)
               for d, text, _ in funnel._tool_lines([{"role": "tool",
                                                       "content": "\n".join(gone)}])):
            ev_dropped = True
    in_render = any(funnel.hit(pats, day, text, d)
                    for d, text, _ in funnel._tool_lines([{"role": "tool",
                                                            "content": "\n".join(shown_lines)}]))
    in_item = any(funnel.hit(pats, day, funnel._item_text(it), it.get("date"))
                  for it in items if it.get("kind") in _SCORED_KINDS)
    overhead = [ask_tools._size(it) - len(it.get("text") or "")
                for e in rebuilt for it in e.get("items") or []
                if it.get("kind") in ("chunk", "document")]
    return {
        "id": rec.get("id"), "lang": (gold or {}).get("lang"), "expect": rec.get("expect"),
        "evidence_in_item": in_item, "evidence_in_render": in_render,
        "evidence_dropped": ev_dropped, "dropped_lines": dropped, "items": len(items),
        "rendered_chars": sum(len(ln) + 1 for block in blocks for ln in block),
        "truncated_envelopes": sum(bool((e.get("meta") or {}).get("truncated"))
                                   for e in rebuilt),
    }, overhead


def _envelope_metrics(envs, ask_orchestrator):
    """--dump-envelopes metrics of one multi-call turn, from the production renderer:
    item lines byte-identical to an earlier item's line in the turn, by (earlier tool ->
    later tool); the render length with the budget lifted; the items `_budgeted` drops.
    None for a single-call turn."""
    if len(envs) < 2:
        return None
    first, dups = {}, {}
    for env in envs:
        tool = env.get("tool") or "unknown"
        for it in env.get("items") or []:
            line = ask_orchestrator._item_line(it)
            if line in first:
                pair = "%s->%s" % (first[line], tool)
                dups[pair] = dups.get(pair, 0) + 1
            else:
                first[line] = tool
    budget = ask_orchestrator.RENDER_MAX_CHARS
    ask_orchestrator.RENDER_MAX_CHARS = sys.maxsize
    try:
        render_len = len(ask_orchestrator.render(envs))
    finally:
        ask_orchestrator.RENDER_MAX_CHARS = budget
    blocks = ask_orchestrator._budgeted(envs)
    dropped = sum(len(env.get("items") or []) - (len(block) - 1)
                  for env, block in zip(envs, blocks))
    return {"dup_item_lines_by_tool_pair": dups, "render_len": render_len,
            "budget_dropped_items": dropped}


def _envelope_record(rec, repeat, envs, arm, ask_orchestrator):
    """One --dump-envelopes line (family and arm.src_ref are main's to add): the
    envelopes `_messages` flattens, grouped by round in call order."""
    rounds = []
    for env in envs:
        if not rounds or rounds[-1]["round"] != env.get("round"):
            rounds.append({"round": env.get("round"), "envelopes": []})
        rounds[-1]["envelopes"].append({"name": env.get("tool"), "call_id": env.get("id"),
                                        "items": list(env.get("items") or [])})
    return {"qid": rec.get("id"), "repeat": repeat, "turn": rec.get("turn"),
            "arm": dict(arm), "rounds": rounds,
            "metrics": _envelope_metrics(envs, ask_orchestrator)}


def _baseline_lost_at(results):
    """turn id -> the live run's lost_at, old labels read as new."""
    out = {}
    for run in (results or {}).get("runs") or []:
        for t in run.get("turns") or []:
            lost = (t.get("funnel") or {}).get("lost_at")
            out[t.get("turn")] = _OLD_LABELS.get(lost, lost)
    return out


def replay(rows, gold, ask_tools, ask_orchestrator, *, window=None, result_chars=None,
           results=None, envelope_dump=None):
    """Score every turn of a --keep-rows dump at one arm. `gold`: {id: gold item}.
    `envelope_dump`: a list that gets one --dump-envelopes record per turn.
    The module constants are restored before returning."""
    saved = _configure(ask_tools, window, result_chars)
    try:
        lost_at = _baseline_lost_at(results)
        turns, overhead, seen = {}, [], {}
        dump_arm = {"window": getattr(ask_tools, "WINDOW_CHARS", ask_tools.MAX_TEXT),
                    "result_chars": ask_tools.MAX_RESULT_CHARS}
        for rec in rows:
            qid = rec.get("id")
            seen[qid] = seen.get(qid, 0) + 1
            envs, rebuilt, unreplayed = _rebuild_turn(rec, ask_tools)
            if envelope_dump is not None:
                envelope_dump.append(_envelope_record(rec, seen[qid], envs, dump_arm,
                                                      ask_orchestrator))
            t, oh = _score_turn(rec, gold.get(qid), envs, rebuilt, ask_tools, ask_orchestrator)
            t["unreplayed"] = unreplayed
            if results is not None:
                t["lost_at"] = lost_at.get(rec.get("turn"))
            turns["%s#%d" % (qid, seen[qid])] = t
            overhead.extend(oh)
        mode = "centred" if hasattr(ask_tools, "WINDOW_CHARS") else "head"
        arm = {"window": getattr(ask_tools, "WINDOW_CHARS", ask_tools.MAX_TEXT),
               "result_chars": ask_tools.MAX_RESULT_CHARS}
    finally:
        for k, v in saved.items():
            setattr(ask_tools, k, v)
    answerable = {k: t for k, t in turns.items() if t["expect"] == "answerable"}
    capped = {k: t for k, t in answerable.items() if t.get("lost_at") == "cut_text_cap"}
    chars = [t["rendered_chars"] for t in turns.values()]
    summary = {
        "turns": len(turns), "answerable_turns": len(answerable),
        "evidence_in_item": sum(t["evidence_in_item"] for t in answerable.values()),
        "evidence_in_render": sum(t["evidence_in_render"] for t in answerable.values()),
        "cut_text_cap": {"turns": len(capped),
                         "evidence_in_item": sum(t["evidence_in_item"] for t in capped.values())},
        "dropped_lines": sum(t["dropped_lines"] for t in turns.values()),
        "turns_with_drop": sum(t["dropped_lines"] > 0 for t in turns.values()),
        "evidence_bearing_drops": sum(t["evidence_dropped"] for t in answerable.values()),
        "rendered_chars": {"median": int(statistics.median(chars)) if chars else 0,
                           "p90": _p90(chars)},
        "truncated_envelopes": sum(t["truncated_envelopes"] for t in turns.values()),
        "unreplayed": sum(t["unreplayed"] for t in turns.values()),
        "overhead": {"n": len(overhead), "max": max(overhead, default=0),
                     "p95": _p95(overhead)},
        "zh_mixed": {k: {"evidence_in_item": t["evidence_in_item"],
                         "evidence_in_render": t["evidence_in_render"]}
                     for k, t in answerable.items() if t["lang"] in ("zh", "mixed")},
    }
    return dict(arm, window_mode=mode, turns=turns, summary=summary)


def compare(a, b):
    """Per-turn flips from arm A to arm B, both ways."""
    out = {}
    for stage in ("evidence_in_item", "evidence_in_render"):
        keys = sorted(set(a["turns"]) & set(b["turns"]))
        out[stage] = {
            "gained": [k for k in keys if not a["turns"][k][stage] and b["turns"][k][stage]],
            "lost": [k for k in keys if a["turns"][k][stage] and not b["turns"][k][stage]]}
    out["summary"] = {"a": dict(a.get("summary") or {}, src_ref=a.get("src_ref")),
                      "b": dict(b.get("summary") or {}, src_ref=b.get("src_ref"))}
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--rows")
    ap.add_argument("--gold")
    ap.add_argument("--src-ref")
    ap.add_argument("--family", choices=["report", "session"])
    ap.add_argument("--window", type=int, default=None)
    ap.add_argument("--result-chars", type=int, default=None)
    ap.add_argument("--results", default=None,
                    help="the live run's results file: attaches each turn's lost_at")
    ap.add_argument("--out", default=None)
    ap.add_argument("--dump-envelopes", default=None, metavar="PATH",
                    help="JSONL, one line per turn: the envelopes and items each round "
                         "rendered, with duplicate-line, render-length and budget-drop "
                         "metrics (record text: scratch only)")
    args = ap.parse_args(argv)
    if args.compare:
        return args
    if not (args.rows and args.gold and args.src_ref and args.family and args.out):
        ap.error("--rows, --gold, --src-ref, --family and --out are required")
    for flag, path in (("--out", args.out), ("--dump-envelopes", args.dump_envelopes)):
        if path and Path(path).resolve().is_relative_to((HERE / "results").resolve()):
            raise SystemExit("%s must not point into results/ (scratch only)" % flag)
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.compare:
        a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in args.compare)
        print(json.dumps(compare(a, b), indent=1, ensure_ascii=False))
        return
    from askv2 import harness
    src, sha = harness.prepare_imports(args.src_ref, args.family)
    import ask_orchestrator
    import ask_tools
    rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    gold = {}
    for line in Path(args.gold).read_text(encoding="utf-8").splitlines():
        if line.strip():
            g = json.loads(line)
            gold[g["id"]] = g
    results = (json.loads(Path(args.results).read_text(encoding="utf-8"))
               if args.results else None)
    dump = [] if args.dump_envelopes else None
    out = replay(rows, gold, ask_tools, ask_orchestrator, window=args.window,
                 result_chars=args.result_chars, results=results, envelope_dump=dump)
    if dump is not None:
        Path(args.dump_envelopes).parent.mkdir(parents=True, exist_ok=True)
        with open(args.dump_envelopes, "w", encoding="utf-8", newline="\n") as fh:
            for d in dump:
                d = dict(family=args.family, **d)
                d["arm"] = dict(src_ref=args.src_ref, **d["arm"])
                fh.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
        print("wrote %s (%d turns)" % (args.dump_envelopes, len(dump)))
    out.update(src_ref=args.src_ref, src_sha=sha, family=args.family,
               rows_file=Path(args.rows).name)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False),
                              encoding="utf-8")
    print(json.dumps(dict(out["summary"], window=out["window"], window_mode=out["window_mode"],
                          result_chars=out["result_chars"], src_ref=args.src_ref),
                     ensure_ascii=False))
    print("wrote %s" % args.out)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
