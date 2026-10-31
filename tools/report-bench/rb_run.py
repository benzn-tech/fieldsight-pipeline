"""Runner: pilot and round 0 (later rounds12 / heldout, Task 12), dry-run first.

    python rb_run.py --phase pilot|round0 --out DIR --dry-run
    python rb_run.py --phase pilot|round0 --out DIR --confirm-calls N
        [--task X T R] [--pilot-session SID]

Reads `<out>/prompts/index.json` (rb_prerender) and `<out>/inputs/manifest.json`.
Order: extraction -> template -> report, then model, candidate, session, rep
(spec §7: a cap hit loses the least-changed task).

* `--dry-run` prints the number of calls still to make and the $ upper, split
  Anthropic / OpenRouter, and exits 0. A live run whose `--confirm-calls` is
  missing or differs from that number exits 2 before any call.
* OpenRouter (spec 2026-10-31 Part A): `--openrouter-key-file` = the owner's
  bench-only key (without it QWEN_API_KEY, the prod key, and a printed warning);
  dollars go to the shared ledger's `openrouter` pool, capped by
  `--openrouter-cap` (default $10, both benches together). The dry run (and a
  live start) exits 2 when the OpenRouter upper is over the room left; a call
  is refused once the pool is spent (cut, exit 3, like the Claude cap).
* `<out>/bench.lock` is held for the whole run: the report and web benches
  share `anthropic_ledger.json` and must never run at the same time.
* Before each Claude call: `ReportLedger.can_spend("report/<phase>", upper)`.
  When it says no, every remaining Claude call of the run is cut (OpenRouter
  calls continue), the summary names the cut cells and the exit code is 3.
* Every finished run is appended to `<out>/runs/<phase>.jsonl` at once; a
  rerun skips finished keys (resume). The ledger is charged with the actual $
  before the record is written (a crash in between over-counts, never under).
* Answer text goes to `<out>/answers/<hash>.txt`; stdout and the summary carry
  ids, counts, outcomes and dollars only. The SDK is imported on the live
  Claude path only (`anthropic.Anthropic(max_retries=0)`).
* A `schema_rejected` Claude cell is rerun at once without `format`, and every
  later run of that cell goes without it (`format_dropped`, spec §9).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402
import rb_ledger  # noqa: E402
import rb_requests  # noqa: E402

TASK_ORDER = ("X", "T", "R")
REPS = {"pilot": 1, "round0": 3, "rounds12": 3, "heldout": 3}
POOLS = {"pilot": "report/pilot", "round0": "report/round0", "rounds12": "report/rounds12",
         "heldout": "report/heldout"}
PILOT_CANDIDATE = "X-cur"


def usd_upper(task, model_key, tokens, budget):
    """`tokens` = the prompt's upper token estimate (rb_common.est_tokens)."""
    m = rb_requests.MODELS[model_key]
    if m["vendor"] != "anthropic":
        # Pinned 2026-10-31 prices; every body carries the reasoning headroom.
        W = rb_ledger.web_ledger()
        up = W.or_upper(m["model"], tokens, budget + rb_requests.HEADROOM)
        if task == "T":
            up += W.or_upper(m["model"], tokens,
                             rb_requests.T_RETRY_BUDGET + rb_requests.HEADROOM)
        return up
    up = rb_ledger.call_cost(m["model"], tokens, budget + rb_requests.HEADROOM)
    if task == "T":
        up += rb_ledger.call_cost(m["model"], tokens,
                                  rb_requests.T_RETRY_BUDGET + rb_requests.HEADROOM)
    return up


def _keys_for(index, cid, roles, manifest):
    keys = sorted(index.get(cid, {}))
    if manifest is None:
        return keys
    out = []
    for k in keys:
        role = (manifest.get(k) or {}).get("role")
        if role is None and k in manifest.get("R", {}):
            role = manifest["R"][k].get("role", "set")
        if (role or "set") in roles:
            out.append(k)
    return out


def _entry(phase, task, cid, model, key, rep, e, pool):
    budget = rb_requests.budget(task, e.get("n", 0))
    return {"key": f"{phase}|{task}|{cid}|{model}|{key}|{rep}", "phase": phase, "task": task,
            "candidate": cid, "model": model, "session": key, "rep": rep, "budget": budget,
            "prompt_sha256": e["sha256"], "prompt_path": os.path.join("prompts", cid, key + ".txt"),
            "usd_upper": usd_upper(task, model, e.get("est_tokens",
                                                      rb_common.est_tokens(e.get("chars", 0))),
                                   budget), "pool": pool}


def plan(phase, tasks, index, manifest=None, pilot_session=None, cells=None):
    """The ordered call list. `cells` (rounds12 / heldout): [(task, cid, model)]."""
    pool = POOLS[phase]
    reps = range(1, REPS[phase] + 1)
    out = []
    for task in TASK_ORDER:
        if task not in tasks:
            continue
        cids = sorted(c for c in index if index[c] and
                      next(iter(index[c].values()))["task"] == task)
        for model in rb_requests.MODEL_ORDER:
            for cid in cids:
                if phase == "pilot" and cid != PILOT_CANDIDATE:
                    continue
                if cells is not None and (task, cid, model) not in cells:
                    continue
                roles = ("heldout",) if phase == "heldout" else ("set",)
                keys = _keys_for(index, cid, roles, manifest)
                if phase == "pilot":
                    keys = [pilot_session] if pilot_session else keys[:1]
                for key in keys:
                    for rep in reps:
                        out.append(_entry(phase, task, cid, model, key, rep,
                                          index[cid][key], pool))
    return out


def is_done(rec) -> bool:
    """A finished run, for resume. Not done: no key, 429, 5xx, connection
    errors (status None) -- the vendor or our setup, not the model -- and a
    schema_rejected cell whose format-free rerun has not happened."""
    if rec.get("outcome") == "schema_rejected":
        return False
    a = (rec.get("attempts") or [{}])[-1]
    if a.get("error_code") == "no_key":
        return False
    if rec.get("outcome") == "http_error":
        st = a.get("http_status")
        return not (st is None or st == 429 or st >= 500)
    return True


def latest(records) -> dict:
    """The last record per key (a retried run appends a new one)."""
    out = {}
    for r in records:
        out[r["key"]] = r
    return out


def done_keys(runs_path):
    return {k for k, r in latest(rb_common.read_jsonl(runs_path)).items() if is_done(r)}


def preflight(todo, client_factory) -> list:
    """What would make the run fail on its first call, checked before the lock."""
    import rb_callers
    vendors = {rb_requests.MODELS[c["model"]]["vendor"] for c in todo}
    problems = []
    if "openrouter" in vendors and not rb_callers.openrouter_key():
        problems.append("QWEN_API_KEY (the OpenRouter key) is not set")
    if "anthropic" in vendors:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            problems.append("ANTHROPIC_API_KEY is not set")
        if client_factory is _default_client and importlib.util.find_spec("anthropic") is None:
            problems.append("the anthropic SDK is not importable (use benchenv)")
    return problems


def _cell(c):
    return f"{c['task']}|{c['candidate']}|{c['model']}"


def split_upper(calls):
    a = sum(c["usd_upper"] for c in calls
            if rb_requests.MODELS[c["model"]]["vendor"] == "anthropic")
    o = sum(c["usd_upper"] for c in calls
            if rb_requests.MODELS[c["model"]]["vendor"] != "anthropic")
    return a, o


def _answer_path(out, key):
    return os.path.join(out, "answers", hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
                        + ".txt")


def _default_client():
    import anthropic                                  # live path only
    return anthropic.Anthropic(max_retries=0)


def execute(phase, calls, out, *, caller=None, client_factory=_default_client, http=None,
            echo=print, openrouter_cap=None):
    import rb_callers
    caller = caller or rb_callers.call
    runs_path = os.path.join(out, "runs", phase + ".jsonl")
    done = done_keys(runs_path)
    dropped = {_cell(r) for r in rb_common.read_jsonl(runs_path)
               if r.get("format_dropped") or r.get("outcome") == "schema_rejected"}
    ledger = rb_ledger.ReportLedger(os.path.join(out, rb_ledger.LEDGER_NAME))
    or_cap = (rb_ledger.web_ledger().OPENROUTER_CAP_USD if openrouter_cap is None
              else openrouter_cap)
    client, cap_hit, or_cap_hit, cut = None, False, False, []
    for c in calls:
        if c["key"] in done:
            continue
        claude = rb_requests.MODELS[c["model"]]["vendor"] == "anthropic"
        if not claude and (or_cap_hit or ledger.openrouter_room(or_cap) <= 1e-9):
            or_cap_hit = True
            cut.append(c["key"])
            continue
        if claude:
            if cap_hit or not ledger.can_spend(c["pool"], c["usd_upper"]):
                cap_hit = True
                cut.append(c["key"])
                continue
            if client is None:
                client = client_factory()
        with open(os.path.join(out, c["prompt_path"]), encoding="utf-8", newline="") as f:
            prompt = f.read()
        with_format = _cell(c) not in dropped
        r = caller(c["model"], c["task"], prompt, c["budget"], http=http, client=client,
                   with_format=with_format)
        format_dropped = not with_format
        if claude and r["outcome"] == "schema_rejected":
            dropped.add(_cell(c))
        if claude and r["outcome"] == "schema_rejected" and (
                cap_hit or not ledger.can_spend(c["pool"], c["usd_upper"])):
            cap_hit = True                      # recorded as schema_rejected: not done
            cut.append(c["key"])
        elif claude and r["outcome"] == "schema_rejected":
            first = r["attempts"]
            r = caller(c["model"], c["task"], prompt, c["budget"], http=http, client=client,
                       with_format=False)
            r["attempts"] = first + r["attempts"]
            r["usd"] = round(sum(a.get("usd") or 0.0 for a in r["attempts"]), 6)
            format_dropped = True
        if claude and r.get("usd"):
            ledger.add(c["pool"], r["usd"])
        if not claude:
            spent = sum(a.get("openrouter_cost_usd") or 0.0 for a in r["attempts"])
            if spent:
                ledger.add_openrouter(spent)
        text = r.pop("text", "") or ""
        tp = _answer_path(out, c["key"])
        rb_common.write_text(tp, text)
        rec = {k: v for k, v in c.items() if k != "usd_upper"}
        rec.update(r)
        rec.update({"planned_usd_upper": c["usd_upper"], "text_path": tp,
                    "format_dropped": format_dropped})
        rb_common.append_jsonl(runs_path, rec)
        echo(f"{c['key']} {r['outcome']} {r['attempts'][-1].get('latency_ms')}ms")
    return {"cut": cut, "cap_hit": cap_hit or or_cap_hit, "openrouter_cap_hit": or_cap_hit}


def summary(phase, out, cut):
    recs = list(latest(rb_common.read_jsonl(os.path.join(out, "runs",
                                                          phase + ".jsonl"))).values())
    counts = Counter(f"{_cell(r)}|{r['outcome']}" for r in recs)
    cut_cells = sorted({k.split("|", 1)[1].rsplit("|", 2)[0] for k in cut})
    s = {"phase": phase, "runs": len(recs), "counts": dict(sorted(counts.items())),
         "anthropic_usd": round(sum(r.get("usd") or 0.0 for r in recs), 6),
         "openrouter_cost_usd": round(sum((a.get("openrouter_cost_usd") or 0.0)
                                          for r in recs for a in r.get("attempts", [])), 6),
         "cut_calls": len(cut), "cut_cells": cut_cells}
    rb_common.write_json(os.path.join(out, f"summary_{phase}.json"), s)
    return s


def main(argv=None, *, caller=None, client_factory=_default_client, http=None, cells=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=list(REPS))
    ap.add_argument("--task", nargs="+", default=list(TASK_ORDER))
    ap.add_argument("--out", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--confirm-calls", type=int)
    ap.add_argument("--pilot-session")
    ap.add_argument("--r-template", help="recorded only; R prompts were pre-rendered with it")
    ap.add_argument("--openrouter-key-file")
    ap.add_argument("--openrouter-cap", type=float,
                    default=rb_ledger.web_ledger().OPENROUTER_CAP_USD)
    a = ap.parse_args(argv)
    out = rb_common.check_out_dir(a.out)
    import rb_callers
    W = rb_ledger.web_ledger()
    try:
        key, _ = W.load_or_key(a.openrouter_key_file, "QWEN_API_KEY")
    except ValueError as exc:
        print("refused: " + str(exc))
        return 2
    rb_callers.set_openrouter_key(key if a.openrouter_key_file else None)
    index = rb_common.read_json(os.path.join(out, "prompts", "index.json"))
    mpath = os.path.join(out, "inputs", "manifest.json")
    manifest = rb_common.read_json(mpath) if os.path.exists(mpath) else None
    if cells is None and a.phase in ("rounds12", "heldout"):
        import rb_select
        cells = rb_select.cells_for_phase(a.phase, out)
    calls = plan(a.phase, a.task, index, manifest, a.pilot_session, cells)
    done = done_keys(os.path.join(out, "runs", a.phase + ".jsonl"))
    todo = [c for c in calls if c["key"] not in done]
    an, orr = split_upper(todo)
    print(f"phase {a.phase}: planned {len(calls)} calls, done {len(calls) - len(todo)}, "
          f"to make {len(todo)}; $ upper anthropic {an:.2f} openrouter {orr:.2f}")
    for t in TASK_ORDER:
        n = sum(1 for c in todo if c["task"] == t)
        if n:
            print(f"  {t}: {n}")
    led = rb_ledger.ReportLedger(os.path.join(out, rb_ledger.LEDGER_NAME))
    room = led.openrouter_room(a.openrouter_cap)
    print(f"openrouter pool ${led.openrouter_spent():.2f} of ${a.openrouter_cap:g}, "
          f"room ${room:.2f}")
    if orr > room + 1e-9:
        print(f"refused: openrouter upper {orr:.2f} > room {room:.2f} "
              "(raise --openrouter-cap or split the run)")
        return 2
    if a.dry_run:
        return 0
    if a.confirm_calls != len(todo):
        print(f"--confirm-calls {a.confirm_calls} != {len(todo)} calls to make; nothing sent")
        return 2
    problems = preflight(todo, client_factory)
    if problems:
        for p in problems:
            print("refused: " + p)
        return 2
    with rb_common.bench_lock(out, "report"):
        res = execute(a.phase, todo, out, caller=caller, client_factory=client_factory,
                      http=http, openrouter_cap=a.openrouter_cap)
    s = summary(a.phase, out, res["cut"])
    print(f"runs {s['runs']} anthropic ${s['anthropic_usd']:.2f} cut {s['cut_calls']}")
    for k, v in s["counts"].items():
        print(f"  {k} {v}")
    for c in s["cut_cells"]:
        print(f"  CUT {c}")
    return 3 if res["cap_hit"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
