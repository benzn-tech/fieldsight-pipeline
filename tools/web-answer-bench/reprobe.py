"""Stage 0 re-probe: one Claude config, a lower `max_uses`, on Stage 0's stored drafts.

Spec: docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md §4.0, §9
(`max_uses` is the post-Stage-0 lever). No gemini call: the drafts come from
--out/stage0_drafts.jsonl, so the verify inputs are the ones Stage 0 measured.

    C:/Users/camil/wt/benchenv/Scripts/python.exe tools/web-answer-bench/reprobe.py \
        --out <stage0 out dir> --config haiku-20250305 --max-uses 2 \
        --search-price-per-1000 10 --dry-run
    # state the printed numbers, then: ... --confirm-calls 24

Exit codes: 0 done, 2 refused, 3 hard stop (shared ledger at its cap).
Nothing printed or summarised carries question, draft or answer text.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import claude_verify                                     # noqa: E402
import stage0                                            # noqa: E402
import ledger as ledger_mod                               # noqa: E402
from ledger import CAP_USD, Ledger, call_cost             # noqa: E402

CONFIGS = {name: (model, tool) for name, model, tool in stage0.CONFIGS}


UPPER_CALLS = stage0.REPS * stage0.CALLS_PER_VERIFY_UPPER   # one pause_turn each


def plan(max_uses):
    return {"verifies": stage0.REPS, "upper": UPPER_CALLS,
            "searches_upper": UPPER_CALLS * max_uses}


def validate(argv, env):
    ap = argparse.ArgumentParser(prog="reprobe")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", required=True, choices=sorted(CONFIGS))
    ap.add_argument("--max-uses", type=int, required=True)
    ap.add_argument("--search-price-per-1000", type=float, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm-calls", type=int)
    ap.add_argument("--set", default=os.path.join(HERE, "set.v1.jsonl"))
    args = ap.parse_args(argv)
    ledger_mod.refuse_if_locked(args.out, stage0._refuse)
    if not 1 <= args.max_uses <= claude_verify.MAX_USES:
        stage0._refuse(f"--max-uses must be 1..{claude_verify.MAX_USES}")
    if not os.path.exists(os.path.join(args.out, "stage0_drafts.jsonl")):
        stage0._refuse("no stage0_drafts.jsonl in --out (run Stage 0 first)")
    if not args.dry_run:
        missing = [k for k in ("ANTHROPIC_API_KEY",) if not env.get(k)]
        if missing:
            stage0._refuse("missing environment variable(s): " + ", ".join(missing))
        if args.confirm_calls != UPPER_CALLS:
            stage0._refuse(f"--confirm-calls must equal the upper bound "
                           f"{UPPER_CALLS} (run --dry-run first)")
    return args


def _cost_upper(model, max_uses, price):
    return UPPER_CALLS * call_cost(model, stage0.UPPER_INPUT_TOKENS,
                                        stage0.UPPER_OUTPUT_TOKENS, max_uses, price)


def run(args, *, client, clock=time.monotonic, stdout=sys.stdout):
    import web_answer                                     # lazy: needs src/ on the path
    model, tool = CONFIGS[args.config]
    upper = _cost_upper(model, args.max_uses, args.search_price_per_1000)
    print(f"reprobe {args.config} max_uses={args.max_uses}: {stage0.REPS} verifies, "
          f"upper bound {UPPER_CALLS} calls, "
          f"<= {UPPER_CALLS * args.max_uses} searches, "
          f"Anthropic <= ${upper:.2f} (hard stop ${CAP_USD:g}, cumulative)", file=stdout)
    if args.dry_run:
        return 0, {"dry_run": True}

    with open(os.path.join(args.out, "stage0_drafts.jsonl"), encoding="utf-8") as f:
        drafts = {d["item"]: d["draft"] for d in map(json.loads, f) if d.get("draft")}
    tune = stage0.load_tune(args.set)
    questions = {it["id"]: it["question"] for it in tune}
    unsure = stage0.unsure_ids(tune)
    ledger = Ledger(os.path.join(args.out, "anthropic_ledger.json"))
    stem = f"reprobe_{args.config}_mu{args.max_uses}"
    records, hard_stop = [], False
    with open(os.path.join(args.out, stem + "_calls.jsonl"), "w", encoding="utf-8") as f:
        for rep, item in enumerate(stage0.order(sorted(questions))):
            if ledger.exhausted():
                hard_stop = True
                break
            prompt = web_answer.VERIFY_PROMPT.format(question=questions[item],
                                                     draft=drafts[item])
            r = claude_verify.verify(client, model=model, tool_version=tool, prompt=prompt,
                                     budget_s=stage0.VERIFY_BUDGET_S, clock=clock,
                                     max_uses=args.max_uses)
            r = stage0.reclassify(dict(r, item=item), unsure)
            cost = call_cost(model, r["input_tokens"], r["output_tokens"],
                             r["search_requests"], args.search_price_per_1000)
            ledger.add(cost)
            rec = dict(r, config=args.config, item=item, rep=rep, model=model,
                       tool_version=tool, max_uses=args.max_uses,
                       prompt_sha256=stage0._sha(prompt), cost_usd=round(cost, 6))
            records.append(rec)
            f.write(json.dumps(rec) + "\n")
            print(f"{args.config} {item} rep{rep} {r['outcome']} {r['latency_ms']} ms "
                  f"searched={r['searched']} results={r['result_count']}", file=stdout)
            if r["outcome"] == "rejected":
                break

    config = dict(stage0.summarize(records)["configs"][args.config], max_uses=args.max_uses)
    passed = stage0.stop_rule({"configs": {args.config: config}})["proceed"]
    summary = {"config": config, "passes_gate": passed, "hard_stop": hard_stop,
               "anthropic_spent_usd": round(ledger.spent, 4)}
    with open(os.path.join(args.out, stem + "_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"{args.config} max_uses={args.max_uses}: n={config['n']} p50={config['p50_ms']} "
          f"max={config['max_ms']} searched={config['searched_rate']} "
          f"no_search={config['no_search']} failures={config['failures']} "
          f"passes_gate={passed} ${config['cost_usd']:.4f}", file=stdout)
    return (3 if hard_stop else 0), summary


def main(argv=None):
    args = validate(sys.argv[1:] if argv is None else argv, os.environ)
    client = None
    if not args.dry_run:
        import anthropic                                  # lazy: bench venv only
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],
                                     max_retries=0)
    code, _ = run(args, client=client)
    return code


if __name__ == "__main__":
    sys.exit(main())
