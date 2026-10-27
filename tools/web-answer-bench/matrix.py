"""Prompt x model matrix, round 0: every candidate prompt on every admitted model.

Spec: docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md §4.4, §6, §7
Plan: docs/superpowers/plans/2026-10-28-web-answer-prompt-matrix.md (Task 5)

Owner-run, from the bench venv (the SDK is not in the CI environment):

    C:/Users/camil/wt/benchenv/Scripts/python.exe tools/web-answer-bench/matrix.py \
        --out <dir outside the repo> --round 0 --search-price-per-1000 <price> --dry-run
    # state the printed numbers, then: ... --confirm-calls <upper>

Exit codes: 0 done, 2 refused (arguments / env), 3 hard stop (matrix pool at its
cap, or the scored-run reserve would be touched).

Order: gemini drafts first (every verify cell verifies them, paired by item and
rep), then the other cells cheapest first: OpenRouter, Haiku, Sonnet. Nothing
printed or summarised carries question, draft or answer text; that text lives
only in --out/matrix_r0_calls.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(REPO, "src")
for _p in (HERE, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import claude_verify                                     # noqa: E402
import openrouter_bench                                  # noqa: E402
import prompts                                           # noqa: E402
import stage0                                            # noqa: E402
from ledger import (CAP_USD, MATRIX_POOL_CAP, SCORED_RESERVE, Ledger,  # noqa: E402
                    call_cost)

MODEL_KEYS = ("gemini", "deepseek", "mimo", "sonnet", "haiku")
# Stage 0 froze these (spec §4.0): one tool version, max_uses per model.
CLAUDE = {"sonnet": ("claude-sonnet-5-5", 3), "haiku": ("claude-haiku-5-5", 2)}
TOOL_VERSION = "web_search_20250305"
SEARCH_MODELS = ("gemini", "deepseek", "sonnet", "haiku")   # mimo: draft only
REPS = 3
POOL = "matrix"
CHAIN_BUDGET_S = stage0.CHAIN_BUDGET_S                   # 22.1
STEP_BUDGET_S = stage0.VERIFY_BUDGET_S                   # 14: verify / single call
OR_MAX_TOKENS = {"draft": 1024, "verify": 2048, "single": 2048}   # as web_answer
GEMINI_DRAFT = ("draft", "draft_v1", "gemini")
_VENDOR_RANK = {"sonnet": 2, "haiku": 1}
_STEP_RANK = {"draft": 0, "verify": 1, "single": 2}


def _cell_id(cell):
    return "/".join(cell)


def _run_order(cell):
    step, pid, model = cell
    return (cell != GEMINI_DRAFT, _VENDOR_RANK.get(model, 0), _STEP_RANK[step], pid,
            MODEL_KEYS.index(model))


def round0_cells():
    """The 21 round-0 cells, in run order."""
    cells = [("draft", "draft_v1", m) for m in MODEL_KEYS]
    for step in ("verify", "single"):
        for pid in sorted(p for p, s in prompts.CANDIDATES.items() if s == step):
            cells += [(step, pid, m) for m in SEARCH_MODELS]
    return sorted(cells, key=_run_order)


def calls_upper(cell):
    step, _, model = cell
    if model in CLAUDE:
        return 1 if step == "draft" else stage0.CALLS_PER_VERIFY_UPPER   # pause_turn
    return 2 if step == "verify" else 1                                  # one retry


def plan(cells, reps=REPS, *, items=5, price_per_1000=0.0):
    p = {"cells": len(cells), "runs": 0, "anthropic_upper": 0, "openrouter_upper": 0,
         "searches_upper": 0, "anthropic_usd_upper": 0.0}
    for cell in cells:
        runs = reps * items
        calls = runs * calls_upper(cell)
        p["runs"] += runs
        step, _, model = cell
        if model in CLAUDE:
            name, max_uses = CLAUDE[model]
            searches = max_uses if step != "draft" else 0
            p["anthropic_upper"] += calls
            p["searches_upper"] += calls * searches
            p["anthropic_usd_upper"] += calls * call_cost(
                name, stage0.UPPER_INPUT_TOKENS, stage0.UPPER_OUTPUT_TOKENS, searches,
                price_per_1000)
        else:
            p["openrouter_upper"] += calls
    p["upper"] = p["anthropic_upper"] + p["openrouter_upper"]
    p["anthropic_usd_upper"] = round(p["anthropic_usd_upper"], 6)
    return p


def unconfirmed_tune(path):
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return [r["id"] for r in rows if r["split"] == "tune" and r.get("confirmed") is not True]


def validate(argv, env, repo_root=REPO):
    ap = argparse.ArgumentParser(prog="matrix")
    ap.add_argument("--out", required=True)
    ap.add_argument("--round", type=int, required=True, choices=[0])
    ap.add_argument("--search-price-per-1000", type=float, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm-calls", type=int)
    ap.add_argument("--set", default=os.path.join(HERE, "set.v1.jsonl"))
    args = ap.parse_args(argv)
    out = Path(args.out).resolve()
    if out == Path(repo_root).resolve() or Path(repo_root).resolve() in out.parents:
        stage0._refuse("--out must be outside the repository")
    if args.search_price_per_1000 < 0:
        stage0._refuse("--search-price-per-1000 must be >= 0")
    if not args.dry_run:
        missing = [k for k in stage0.ENV_KEYS if not env.get(k)]
        if missing:
            stage0._refuse("missing environment variable(s): " + ", ".join(missing))
        # Spec §4.4 gate (2): grades are only as good as the gold they are read against.
        unconfirmed = unconfirmed_tune(args.set)
        if unconfirmed:
            stage0._refuse("tune gold not confirmed: " + ", ".join(unconfirmed))
        upper = plan(round0_cells())["upper"]
        if args.confirm_calls != upper:
            stage0._refuse(f"--confirm-calls must equal the upper bound {upper} "
                           "(run --dry-run first)")
    args.out = str(out)
    return args


def _domains(r):
    import web_answer                                     # lazy: needs src/ on the path
    titles = r.get("result_titles") or [None] * len(r.get("result_urls") or [])
    out = []
    for url, title in zip(r.get("result_urls") or [], titles):
        d = web_answer._domain(url, title)
        if d and d not in out:
            out.append(d)
    return out


def _answer(step, r, draft):
    """What the reader gets, and from where. A verify that did not verify hands
    the reader the draft, unverified (web_answer.general_answer)."""
    text = (r.get("text") or "").strip()
    if step == "verify":
        if r["outcome"] == "ok" and text:
            return text, "verify"
        return draft + "\n\n[unverified]", "draft"
    if text and (r["outcome"] == "ok" or (step == "single" and r["outcome"] == "no_search")):
        return text, "model"
    return None, None


def _one(cell, prompt, *, client, or_call, clock, draft_ms=None):
    step, _, model = cell
    if model in CLAUDE:
        name, max_uses = CLAUDE[model]
        if step == "draft":
            tool, budget = None, _draft_timeout()
        else:
            tool = TOOL_VERSION
            budget = STEP_BUDGET_S if step == "single" else \
                min(STEP_BUDGET_S, CHAIN_BUDGET_S - draft_ms / 1000.0)
        return claude_verify.verify(client, model=name, tool_version=tool, prompt=prompt,
                                    budget_s=budget, clock=clock, max_uses=max_uses)
    if step == "draft":
        return or_call(model, prompt, web=False, timeout=_draft_timeout(),
                       max_tokens=OR_MAX_TOKENS[step], clock=clock)
    if step == "single":
        return or_call(model, prompt, web=True, timeout=STEP_BUDGET_S,
                       max_tokens=OR_MAX_TOKENS[step], clock=clock)
    left = CHAIN_BUDGET_S - draft_ms / 1000.0
    return or_call(model, prompt, web=True, timeout=min(STEP_BUDGET_S, left),
                   max_tokens=OR_MAX_TOKENS[step], retry_left_s=left, clock=clock)


def _draft_timeout():
    import web_answer                                     # lazy: needs src/ on the path
    return web_answer._draft_timeout(CHAIN_BUDGET_S)


def _p50(values):
    return int(statistics.median(values)) if values else None


def summarize(records):
    cells = {}
    for r in records:
        cells.setdefault(r["cell"], []).append(r)
    out = {}
    for cid, rs in cells.items():
        live = [r for r in rs if r["outcome"] != "no_draft"]
        ms = [r["latency_ms"] for r in live if r["calls"]]
        orc = [r["openrouter_cost_usd"] for r in live
               if isinstance(r.get("openrouter_cost_usd"), (int, float))]
        out[cid] = {
            "n": len(live), "no_draft": len(rs) - len(live),
            "outcomes": dict(Counter(r["outcome"] for r in live)),
            "p50_ms": _p50(ms), "max_ms": max(ms) if ms else None,
            "searched_rate": (sum(bool(r["searched"]) for r in live) / len(live))
            if live else None,
            "retries": sum(bool(r.get("retried")) for r in live),
            "continuations": sum(bool(r.get("continuation")) for r in live),
            "anthropic_usd": round(sum(r["cost_usd"] or 0 for r in live), 6),
            "openrouter_usd": round(sum(orc), 6) if orc else None,
        }
    return out


def _write_summary(out, summary):
    with open(os.path.join(out, "matrix_r0_summary.json"), "w", encoding="utf-8",
              newline="") as f:
        json.dump(summary, f, indent=2)


def _blocked(ledger):
    """No Anthropic call once the matrix pool is spent or the scored reserve
    would be the next thing spent."""
    return ledger.pool_exhausted(POOL, MATRIX_POOL_CAP) or ledger.room() <= SCORED_RESERVE


def run(args, *, items, client, or_call=openrouter_bench.call, clock=time.monotonic,
        stdout=sys.stdout):
    """Returns (exit code, summary). `client` and `or_call` are injected."""
    cells = round0_cells()
    p = plan(cells, price_per_1000=args.search_price_per_1000)
    ledger = Ledger(os.path.join(args.out, "anthropic_ledger.json"))
    print(f"matrix r0 plan: {p['cells']} cells, {p['runs']} runs, upper bound "
          f"{p['upper']} calls (Anthropic {p['anthropic_upper']}, OpenRouter "
          f"{p['openrouter_upper']}), <= {p['searches_upper']} Anthropic searches, "
          f"Anthropic <= ${p['anthropic_usd_upper']:.2f} at "
          f"${args.search_price_per_1000:g}/1000 searches", file=stdout)
    print(f"ledger: matrix pool ${ledger.pool_spent(POOL):.2f} of ${MATRIX_POOL_CAP:g}, "
          f"total ${ledger.spent:.2f} of ${CAP_USD:g}, room ${ledger.room():.2f}, "
          f"scored reserve ${SCORED_RESERVE:g} (Claude cells stop at the pool cap)",
          file=stdout)
    if args.dry_run:
        return 0, {"dry_run": True, "plan": p}

    os.makedirs(args.out, exist_ok=True)
    unsure = stage0.unsure_ids(items)
    summary = {"round": 0, "plan": p, "hard_stop": False}
    drafts, records, seq = {}, [], 0
    with open(os.path.join(args.out, "matrix_r0_calls.jsonl"), "w", encoding="utf-8",
              newline="") as f:
        for cell in cells:
            step, pid, model = cell
            template = prompts.load(pid)
            for rep in range(REPS):
                for it in items:
                    item = it["id"]
                    if model in CLAUDE and _blocked(ledger):
                        summary["hard_stop"] = True
                        break
                    seq += 1
                    rec = {"run_id": f"r0-{seq:04d}", "round": 0, "cell": _cell_id(cell),
                           "step": step, "prompt_id": pid, "model_key": model,
                           "model": CLAUDE[model][0] if model in CLAUDE
                           else openrouter_bench.PROFILES[model]["model"],
                           "item": item, "rep": rep, "unsure": item in unsure,
                           "prompt_sha256": prompts.sha(pid)}
                    draft = drafts.get((item, rep))
                    if step == "verify" and draft is None:
                        rec.update(outcome="no_draft", calls=0, latency_ms=0,
                                   searched=False, cost_usd=0.0, answer=None,
                                   answer_source=None, text="")
                    else:
                        kw = {"question": it["question"]}
                        if step == "verify":
                            kw["draft"] = draft[0]
                        r = _one(cell, template.format(**kw), client=client,
                                 or_call=or_call, clock=clock,
                                 draft_ms=draft[1] if draft else None)
                        if step != "draft":
                            r = stage0.reclassify(dict(r, item=item), unsure)
                        cost = 0.0
                        if model in CLAUDE:
                            cost = call_cost(CLAUDE[model][0], r["input_tokens"],
                                             r["output_tokens"], r.get("search_requests", 0),
                                             args.search_price_per_1000)
                            ledger.add(cost, pool=POOL)
                        answer, source = _answer(step, r, draft[0] if draft else "")
                        rec.update(r)
                        rec.update(item=item, cost_usd=round(cost, 6),
                                   result_domains=_domains(r), answer=answer,
                                   answer_source=source)
                        if step == "verify":
                            rec["draft_ms"] = draft[1]
                        if cell == GEMINI_DRAFT and r["outcome"] == "ok" and r["text"]:
                            drafts[(item, rep)] = (r["text"], r["latency_ms"])
                    records.append(rec)
                    f.write(json.dumps(rec) + "\n")
                    print(f"{rec['run_id']} {rec['cell']} {item} rep{rep} {rec['outcome']} "
                          f"{rec['latency_ms']} ms searched={rec['searched']} "
                          f"results={rec.get('result_count', 0)}", file=stdout)
                if summary["hard_stop"]:
                    break
            if summary["hard_stop"]:
                print(f"matrix: hard stop before {_cell_id(cell)} rep{rep} (matrix pool "
                      f"${ledger.pool_spent(POOL):.2f}, room ${ledger.room():.2f})",
                      file=stdout)
                break

    summary["cells"] = summarize(records)
    summary["anthropic_spent_usd"] = round(ledger.spent, 4)
    summary["matrix_pool_spent_usd"] = round(ledger.pool_spent(POOL), 4)
    _write_summary(args.out, summary)
    for cid, c in summary["cells"].items():
        print(f"{cid}: n={c['n']} outcomes={json.dumps(c['outcomes'], sort_keys=True)} "
              f"p50={c['p50_ms']} max={c['max_ms']} searched={c['searched_rate']} "
              f"${c['anthropic_usd']:.4f}", file=stdout)
    return (3 if summary["hard_stop"] else 0), summary


def main(argv=None):
    args = validate(sys.argv[1:] if argv is None else argv, os.environ)
    items = stage0.load_tune(args.set)
    client = None
    if not args.dry_run:
        import anthropic                                  # lazy: bench venv only
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],
                                     max_retries=0)
    code, _ = run(args, items=items, client=client)
    return code


if __name__ == "__main__":
    sys.exit(main())
