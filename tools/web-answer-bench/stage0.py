"""Stage 0: can any Claude model + web search verify a gemini draft in budget?

Spec: docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md §4.0
Plan: docs/superpowers/plans/2026-10-27-claude-web-answer-bench-stage0.md

Owner-run. Run it from the bench venv (the SDK is not in the CI environment):

    C:/Users/camil/wt/benchenv/Scripts/python.exe tools/web-answer-bench/stage0.py \
        --out <dir outside the repo> --search-price-per-1000 <from the pricing page> --dry-run
    # state the printed numbers, then:
    ... --confirm-calls 77

Exit codes: 0 done, 2 refused (arguments / env), 3 hard stop (ledger at $45),
4 a gemini draft failed (nothing was sent to Anthropic).

Nothing printed or summarised carries question, draft or answer text; that text
lives only in --out/stage0_calls.jsonl and --out/stage0_drafts.jsonl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(REPO, "src")
for _p in (HERE, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import claude_verify                                     # noqa: E402
import ledger as ledger_mod                               # noqa: E402
from ledger import CAP_USD, PRICES, Ledger, call_cost     # noqa: E402

CONFIGS = (
    ("sonnet-20260209", "claude-sonnet-5-5", "web_search_20260209"),
    ("sonnet-20250305", "claude-sonnet-5-5", "web_search_20250305"),   # unconfirmed
    ("haiku-20250305", "claude-haiku-5-5", "web_search_20250305"),
)
REPS = 12
CHAIN_BUDGET_S = 22.1          # 25 - t_pre 2.9 (spec §4.1)
VERIFY_BUDGET_S = 14.0
P50_GATE_MS = 9000             # VERIFY_FLOOR
MAX_GATE_MS = 14000            # VERIFY_BUDGET
OVER_BUDGET_MS = MAX_GATE_MS + 1
# no_search: answered without searching. Allowed only on an `unsure` item (where
# declining to search is the right call) and counted at its own wall time; on any
# other item it is a failed verify.
GATE_OK = ("ok", "max_tokens", "no_search")
SEARCHED_RATE_GATE = 0.9       # on items that need the web (not `unsure`)
# Upper-bound assumptions per Anthropic request (spec §7).
UPPER_INPUT_TOKENS = 46_000
UPPER_OUTPUT_TOKENS = claude_verify.MAX_TOKENS
CALLS_PER_VERIFY_UPPER = 2     # one pause_turn continuation
ENV_KEYS = ("ANTHROPIC_API_KEY", "CORROBORATION_API_KEY")


def load_tune(path):
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return [{"id": r["id"], "question": r["question"], "kind": r.get("kind")}
            for r in rows if r["split"] == "tune"]


def unsure_ids(items):
    return frozenset(it["id"] for it in items if it.get("kind") == "unsure")


def order(ids, reps=REPS):
    return [ids[i % len(ids)] for i in range(reps)]


def plan():
    verifies = len(CONFIGS) * REPS
    upper_requests = verifies * CALLS_PER_VERIFY_UPPER
    return {"drafts": 5, "planned": 5 + verifies, "upper": 5 + upper_requests,
            "searches_upper": upper_requests * claude_verify.MAX_USES}


def cost_upper(price_per_1000):
    total = 0.0
    for _, model, _ in CONFIGS:
        requests = REPS * CALLS_PER_VERIFY_UPPER
        total += requests * call_cost(model, UPPER_INPUT_TOKENS, UPPER_OUTPUT_TOKENS,
                                      claude_verify.MAX_USES, price_per_1000)
    return total


def _refuse(msg):
    print("stage0: " + msg, file=sys.stderr)
    raise SystemExit(2)


def validate(argv, env, repo_root=REPO):
    ap = argparse.ArgumentParser(prog="stage0")
    ap.add_argument("--out", required=True)
    ap.add_argument("--search-price-per-1000", type=float, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm-calls", type=int)
    ap.add_argument("--set", default=os.path.join(HERE, "set.v1.jsonl"))
    args = ap.parse_args(argv)
    out = Path(args.out).resolve()
    if out == Path(repo_root).resolve() or Path(repo_root).resolve() in out.parents:
        _refuse("--out must be outside the repository")
    ledger_mod.refuse_if_locked(out, _refuse)
    if args.search_price_per_1000 < 0:
        _refuse("--search-price-per-1000 must be >= 0")
    if not args.dry_run:
        missing = [k for k in ENV_KEYS if not env.get(k)]
        if missing:
            _refuse("missing environment variable(s): " + ", ".join(missing))
        if args.confirm_calls != plan()["upper"]:
            _refuse(f"--confirm-calls must equal the upper bound {plan()['upper']} "
                    "(run --dry-run first)")
    args.out = str(out)
    return args


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reclassify(r, unsure=frozenset()):
    """Records written before `no_search` existed called it `failed`; `unsure`
    marks the items where declining to search is allowed."""
    r = dict(r, unsure=r.get("unsure", r.get("item") in unsure))
    if (r["outcome"] == "failed" and r.get("http_status") == 200
            and r.get("stop_reason") == "end_turn" and not r.get("searched")
            and not r.get("error_code") and not r.get("search_requests")):
        r["outcome"] = "no_search"
    if r["outcome"] == "no_search" and not r["unsure"]:
        r["outcome"] = "failed"
    return r


def _rate(flags):
    return (sum(flags) / len(flags)) if flags else None


def _p50(values):
    return int(statistics.median(values)) if values else None


def summarize(records):
    configs = {}
    for name, model, tool in CONFIGS:
        rs = [r for r in records if r["config"] == name]
        rejected = any(r["outcome"] == "rejected" for r in rs)
        live = [r for r in rs if r["outcome"] != "rejected"]
        # Anything but an answer (or a max_tokens stop, reported on its own) is
        # over budget: a verify that fails fast is not a fast verify.
        failed = [r for r in live if r["outcome"] not in GATE_OK]
        walls = [OVER_BUDGET_MS if r["outcome"] not in GATE_OK else r["latency_ms"]
                 for r in live]
        configs[name] = {
            "model": model, "tool": tool, "rejected": rejected, "n": len(live),
            "p50_ms": int(statistics.median(walls)) if walls else None,
            "max_ms": max(walls) if walls else None,
            "searched_rate": (sum(r["searched"] for r in live) / len(live)) if live else None,
            "searched_rate_web": _rate([r["searched"] for r in live if not r.get("unsure")]),
            "p50_searched_ms": _p50([r["latency_ms"] for r in live
                                     if r["searched"] and r["outcome"] in GATE_OK]),
            "failures": len(failed),
            "timeouts": sum(r["outcome"] == "timeout" for r in live),
            "no_search": sum(r["outcome"] == "no_search" for r in live),
            "refusals": sum(r["outcome"] == "refusal" for r in live),
            "max_tokens_stops": sum(r["outcome"] == "max_tokens" for r in live),
            "continuations": sum(bool(r["continuation"]) for r in live),
            "error_codes": sorted({r["error_code"] for r in live if r["error_code"]}),
            "cost_usd": round(sum(r["cost_usd"] for r in rs), 6),
        }
    return {"configs": configs}


def stop_rule(summary):
    passing = {}
    models = []
    for name, c in summary["configs"].items():
        if c["model"] not in models:
            models.append(c["model"])
        # Speed of a verify that searched, and searching where the web is needed:
        # a config that answers fast without searching is not a fast verify.
        ok = (not c["rejected"] and c["n"] == REPS and c["p50_ms"] is not None
              and c["p50_ms"] <= P50_GATE_MS and c["max_ms"] <= MAX_GATE_MS
              and c.get("p50_searched_ms") is not None
              and c["p50_searched_ms"] <= P50_GATE_MS
              and (c.get("searched_rate_web") or 0) >= SEARCHED_RATE_GATE)
        if ok and (c["model"] not in passing or c["p50_ms"] < passing[c["model"]][1]):
            passing[c["model"]] = (c["tool"], c["p50_ms"])
    return {"proceed": bool(passing),
            "frozen": {m: t for m, (t, _) in passing.items()},
            "dropped": [m for m in models if m not in passing]}


def _write_summary(out, summary):
    with open(os.path.join(out, "stage0_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)


def run(args, *, items, client, draft_fn, clock=time.monotonic, stdout=sys.stdout):
    """Returns (exit code, summary). `client` and `draft_fn` are injected."""
    import web_answer                                     # lazy: needs src/ on the path
    p = plan()
    upper = cost_upper(args.search_price_per_1000)
    print(f"stage0 plan: drafts {p['drafts']}, planned calls {p['planned']}, "
          f"upper bound {p['upper']} calls, <= {p['searches_upper']} searches, "
          f"Anthropic <= ${upper:.2f} at ${args.search_price_per_1000:g}/1000 searches "
          f"(hard stop ${CAP_USD:g}, cumulative)", file=stdout)
    if args.dry_run:
        return 0, {"dry_run": True, "plan": p, "cost_upper_usd": round(upper, 4)}

    os.makedirs(args.out, exist_ok=True)
    ledger = Ledger(os.path.join(args.out, "anthropic_ledger.json"))
    summary = {"configs": {}, "hard_stop": False, "plan": p}
    if ledger.exhausted():
        summary["hard_stop"] = True
        _write_summary(args.out, summary)
        print(f"stage0: hard stop, ledger already at ${ledger.spent:.2f}", file=stdout)
        return 3, summary

    drafts = {}
    with open(os.path.join(args.out, "stage0_drafts.jsonl"), "w", encoding="utf-8") as f:
        for it in items:
            t = clock()
            text = (draft_fn(it["question"]) or "").strip()
            ms = int(round((clock() - t) * 1000))
            if not text:
                print(f"stage0: draft failed for {it['id']}; nothing sent to Anthropic",
                      file=stdout)
                return 4, summary
            drafts[it["id"]] = text
            f.write(json.dumps({"item": it["id"], "draft_ms": ms, "draft": text}) + "\n")
            print(f"draft {it['id']} {ms} ms", file=stdout)

    questions = {it["id"]: it["question"] for it in items}
    unsure = unsure_ids(items)
    ids = [it["id"] for it in items]
    records = []
    with open(os.path.join(args.out, "stage0_calls.jsonl"), "w", encoding="utf-8") as f:
        for name, model, tool in CONFIGS:
            for rep, item in enumerate(order(ids)):
                if ledger.exhausted():
                    summary["hard_stop"] = True
                    break
                prompt = web_answer.VERIFY_PROMPT.format(question=questions[item],
                                                         draft=drafts[item])
                r = claude_verify.verify(client, model=model, tool_version=tool,
                                         prompt=prompt, budget_s=VERIFY_BUDGET_S,
                                         clock=clock)
                r = reclassify(dict(r, item=item), unsure)
                cost = call_cost(model, r["input_tokens"], r["output_tokens"],
                                 r["search_requests"], args.search_price_per_1000)
                ledger.add(cost)
                rec = dict(r, config=name, item=item, rep=rep, model=model,
                           tool_version=tool, prompt_sha256=_sha(prompt),
                           cost_usd=round(cost, 6))
                records.append(rec)
                f.write(json.dumps(rec) + "\n")
                print(f"{name} {item} rep{rep} {r['outcome']} {r['latency_ms']} ms "
                      f"searched={r['searched']} results={r['result_count']}", file=stdout)
                if r["outcome"] == "rejected":
                    print(f"{name}: rejected (HTTP 400), no further calls", file=stdout)
                    break
            if summary["hard_stop"]:
                break

    summary.update(summarize(records))
    summary["stop_rule"] = stop_rule(summary)
    summary["anthropic_spent_usd"] = round(ledger.spent, 4)
    _write_summary(args.out, summary)
    for name, c in summary["configs"].items():
        print(f"{name}: n={c['n']} p50={c['p50_ms']} max={c['max_ms']} "
              f"searched={c['searched_rate']} rejected={c['rejected']} "
              f"refusals={c['refusals']} max_tokens={c['max_tokens_stops']} "
              f"${c['cost_usd']:.4f}", file=stdout)
    print("stop rule: " + json.dumps(summary["stop_rule"]), file=stdout)
    return (3 if summary["hard_stop"] else 0), summary


def _gemini_draft(question):
    import corroboration_client
    import web_answer
    reply = corroboration_client.call(
        web_answer.DRAFT_PROMPT.format(question=question),
        timeout=web_answer._draft_timeout(CHAIN_BUDGET_S), max_tokens=1024,
        effort="low", caller="bench_draft")
    return reply.text if reply.ok else ""


def main(argv=None):
    args = validate(sys.argv[1:] if argv is None else argv, os.environ)
    items = load_tune(args.set)
    client = None
    if not args.dry_run:
        import anthropic                                  # lazy: bench venv only
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],
                                     max_retries=0)
    code, _ = run(args, items=items, client=client, draft_fn=_gemini_draft)
    return code


if __name__ == "__main__":
    sys.exit(main())
