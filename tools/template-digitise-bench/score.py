"""Score template-digitise reads against the fixture truth; gate and pick a winner.

Spec: docs/superpowers/specs/2026-10-31-model-scope-and-config-update-design.md Part B1.

    python tools/template-digitise-bench/score.py --out <run out dir>

Matching is the 2026-10-16 spike's (docs/superpowers/specs/2026-10-16-template-digitise-spike/
score.py), copied here, not imported: a truth item is matched by the best unused predicted
item at difflib ratio >= 0.8 (1.0 = exact, else a wording change). On top of it:

  invented   an unmatched predicted item that paraphrases no missed truth item. An unmatched
             pred paired (greedy, best first) with a missed truth item at ratio >= 0.5 is a
             wording change, not an invention.
  empty      a read that returned no items, or failed.
  cut_off    a read stopped at max_tokens (template_vision.MSG_TOO_LONG, PARTS_CUT_OFF).

Gate (per arm): recall >= 0.98 and zero invented on every input in every rep (3/3), no empty
or cut-off read, p50 latency <= 60 s. Winner among the candidate image arms: passes, then
recall, then fewer wording changes, then p50. None passes -> stay on gemini.

Writes <out>/summary.json. Stdout carries arm ids, counts and numbers only, never item text.
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import glob
import html
import json
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TRUTH_DIR = os.path.join(REPO, "tests", "fixtures", "template_digitise")

MATCH_AT = 0.8
PARAPHRASE_AT = 0.5
GATE_RECALL = 0.98
GATE_P50_S = 60.0
GATE_REPS = 3
# Candidate image arms (gemini is the shipped baseline, never a candidate).
CANDIDATES = ("muse_low", "muse_medium", "deepseek_off", "deepseek_low")
BASELINE = "gemini_shipped"
PDF_ARM = "muse_pdf_low"

# ---------------------------------------------------------------- spike matching
LABEL = re.compile(r'^\s*(\d+[.)]|[a-z]{1,3}[.)])\s+', re.I)


def norm(s):
    return ' '.join(LABEL.sub('', html.unescape(s)).replace('’', "'").lower().split())


def _lis(a):
    t = []
    for x in a:
        i = bisect.bisect_left(t, x)
        if i == len(t):
            t.append(x)
        else:
            t[i] = x
    return len(t)


def _ratio(a, b):
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def _items(sections):
    out = []
    for s in sections or []:
        if not isinstance(s, dict):
            continue
        title = s.get("title") if isinstance(s.get("title"), str) else ""
        for i in s.get("items") or []:
            if isinstance(i, str):
                out.append((norm(i), norm(title)))
    return out


def score(truth, pred_sections):
    """Counts only (no item text): the spike's match, plus invented / paraphrase."""
    T = _items(truth["sections"])
    P = _items(pred_sections)
    used, match, missed = set(), [], []
    exact = wording = sec_ok = 0
    for t, ts in T:
        best, br = None, 0.0
        for pi, (p, _ps) in enumerate(P):
            if pi in used:
                continue
            r = _ratio(t, p)
            if r > br:
                br, best = r, pi
            if r == 1.0:
                break
        if best is not None and br >= MATCH_AT:
            used.add(best)
            match.append(best)
            if br == 1.0:
                exact += 1
            else:
                wording += 1
            if P[best][1] == ts:
                sec_ok += 1
        else:
            missed.append(t)
    extra = [P[pi][0] for pi in range(len(P)) if pi not in used]
    # Greedy pairing, best ratio first: an extra that paraphrases a missed truth item
    # is a wording change; what is left over is invented.
    pairs = sorted(((_ratio(t, p), ti, pi) for ti, t in enumerate(missed)
                    for pi, p in enumerate(extra)), reverse=True)
    t_used, p_used = set(), set()
    for r, ti, pi in pairs:
        if r < PARAPHRASE_AT:
            break
        if ti in t_used or pi in p_used:
            continue
        t_used.add(ti)
        p_used.add(pi)
    tt = [norm(s["title"]) for s in truth["sections"]]
    pt = [norm(s.get("title") or "") for s in pred_sections or [] if isinstance(s, dict)]
    n = max(1, len(T))
    return {"truth_items": len(T), "pred_items": len(P), "recall": len(match) / n,
            "exact": exact / n, "wording": wording + len(p_used),
            "paraphrased": len(p_used), "missed": len(missed) - len(t_used),
            "invented": len(extra) - len(p_used),
            "order": _lis(match) / max(1, len(match)),
            "sec_truth": len(tt), "sec_pred": len(pt),
            "sec_titles_ok": sum(1 for x in tt if x in pt),
            "item_in_right_section": sec_ok / n}


def matches_any(truth, pred_sections):
    """Step-0 probe: did the answer read the page at all (any item matched)?"""
    T = [t for t, _ in _items(truth["sections"])]
    return any(_ratio(t, p) >= MATCH_AT for p, _ in _items(pred_sections) for t in T)


# ---------------------------------------------------------------- arms
def load_truths(truth_dir=TRUTH_DIR):
    out = {}
    for doc in ("bicl", "penina"):
        with open(os.path.join(truth_dir, f"truth_{doc}.json"), encoding="utf-8") as f:
            out[doc] = json.load(f)
    return out


def read_metrics(result, truths):
    """One read's row: its counts, latency, and whether it was empty / cut off."""
    m = {"arm": result["arm"], "input": result["input"], "rep": result["rep"],
         "latency_s": result.get("latency_s"), "cut_off": bool(result.get("cut_off")),
         "ok": bool(result.get("ok")), "cost_usd": result.get("cost_usd")}
    if result.get("ok"):
        m.update(score(truths[result["doc"]], result.get("sections") or []))
    else:
        m.update(truth_items=len(_items(truths[result["doc"]]["sections"])), pred_items=0,
                 recall=0.0, wording=0, invented=0, sec_titles_ok=0, sec_pred=0)
    m["empty"] = not m["ok"] or m["pred_items"] == 0
    return m


def arm_summary(rows, inputs, reps):
    """Per arm: the gate's numbers over its reads. `inputs` / `reps` = what was planned."""
    ms = [r["latency_s"] for r in rows if isinstance(r.get("latency_s"), (int, float))]
    p50 = statistics.median(ms) if ms else None
    by_input = {}
    for r in rows:
        by_input.setdefault(r["input"], []).append(r)
    complete = all(len(by_input.get(i, [])) >= reps for i in inputs)
    s = {"reads": len(rows), "planned": len(inputs) * reps, "complete": complete,
         "recall_mean": (statistics.mean(r["recall"] for r in rows) if rows else 0.0),
         "recall_min": min((r["recall"] for r in rows), default=0.0),
         "invented": sum(r["invented"] for r in rows),
         "wording": sum(r["wording"] for r in rows),
         "sections_ok": sum(r.get("sec_titles_ok", 0) for r in rows),
         "sections_truth": sum(r.get("sec_truth", 0) for r in rows),
         "empty": sum(1 for r in rows if r["empty"]),
         "cut_off": sum(1 for r in rows if r["cut_off"]),
         "p50_s": p50, "max_s": max(ms) if ms else None,
         "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in rows), 6)}
    s["passes"] = bool(
        rows and complete and reps >= GATE_REPS
        and all(r["recall"] >= GATE_RECALL and r["invented"] == 0 for r in rows)
        and s["empty"] == 0 and s["cut_off"] == 0
        and p50 is not None and p50 <= GATE_P50_S)
    return s


def pick(arms):
    """The decision from arm summaries {arm_id: summary}. Image winner among the
    candidates, then where PDF uploads go (spec B1 PDF coupling)."""
    cands = [(a, arms[a]) for a in CANDIDATES if a in arms]
    ranked = sorted(cands, key=lambda x: (not x[1]["passes"], -x[1]["recall_mean"],
                                          x[1]["wording"],
                                          x[1]["p50_s"] if x[1]["p50_s"] is not None
                                          else float("inf")))
    order = [a for a, _ in ranked]
    winner = ranked[0][0] if ranked and ranked[0][1]["passes"] else None
    pdf_pass = bool(arms.get(PDF_ARM, {}).get("passes"))
    if winner is None:
        image, pdf = "gemini (stay; no candidate passed)", "gemini (stay)"
    else:
        image = winner
        pdf = (PDF_ARM if pdf_pass else
               "owner's call: gemini for PDF only, or PDF_ACCEPTED=False")
    return {"winner": winner, "ranking": order, "image": image, "pdf": pdf,
            "pdf_arm_passes": pdf_pass}


def summarize(out, plan_arms, truths=None):
    """Read <out>/arms/*/*.json, score, gate, pick; write <out>/summary.json."""
    truths = truths or load_truths()
    rows = {}
    for path in sorted(glob.glob(os.path.join(out, "arms", "*", "*.json"))):
        with open(path, encoding="utf-8") as f:
            res = json.load(f)
        rows.setdefault(res["arm"], []).append(read_metrics(res, truths))
    arms = {}
    for arm in plan_arms:
        if arm["id"] in rows:
            arms[arm["id"]] = arm_summary(rows[arm["id"]], arm["inputs"], arm["reps"])
    summary = {"arms": arms, "decision": pick(arms),
               "reads": {a: [{k: v for k, v in r.items()} for r in rs]
                         for a, rs in rows.items()}}
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8", newline="") as f:
        json.dump(summary, f, indent=1)
    return summary


def print_table(summary, stdout=sys.stdout):
    print(f"{'arm':16}{'reads':>6}{'recall':>8}{'min':>7}{'invent':>7}{'word':>6}"
          f"{'empty':>6}{'cut':>5}{'p50 s':>7}{'max s':>7}{'$':>9}  gate", file=stdout)
    for a, s in summary["arms"].items():
        p50 = f"{s['p50_s']:.1f}" if s["p50_s"] is not None else "-"
        mx = f"{s['max_s']:.1f}" if s["max_s"] is not None else "-"
        print(f"{a:16}{s['reads']:>6}{s['recall_mean']:>8.3f}{s['recall_min']:>7.3f}"
              f"{s['invented']:>7}{s['wording']:>6}{s['empty']:>6}{s['cut_off']:>5}"
              f"{p50:>7}{mx:>7}{s['cost_usd']:>9.4f}  {'PASS' if s['passes'] else 'fail'}",
              file=stdout)
    d = summary["decision"]
    print(f"decision: image -> {d['image']}; pdf -> {d['pdf']}", file=stdout)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="score")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    with open(os.path.join(a.out, "plan.json"), encoding="utf-8") as f:
        plan_arms = json.load(f)["arms"]
    print_table(summarize(a.out, plan_arms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
