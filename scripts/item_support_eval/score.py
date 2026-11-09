"""Pre-registered scoring for item_support (docs/superpowers/specs/
2026-11-08-item-support-eval-preregistration.md §4-6).

Each held-out bar is its own `score_*` function over a small pre-joined input, so its
pass/fail/insufficient arithmetic can be pinned at the exact boundary with hand-built
fixtures. `fit` and `build_summary` are the glue over the real data/, runs/ and labels/ files.

`results/summary.json` is the one committed file: counts, rates and verdicts only, never a
session id, a folder name, item text or turn text.

    python -m scripts.item_support_eval.score fit
    python -m scripts.item_support_eval.score summary --run-a runs/heldout_a --run-b runs/heldout_b
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from item_text_match import cn_to_int, place_values, tokens, twin_flags  # noqa: F401

from scripts.item_support_eval import store

PASS, FAIL, INSUFFICIENT, REPORTED = "pass", "fail", "insufficient", "reported"

ALPHA = 0.05
LINK_ERROR_BAR = 0.05
MIN_LINKS = 59                 # smallest n where 0 errors gives a CP upper bound <= 5%
INFERRED_ERROR_BAR = 0.10
MIN_INFERRED = 20
AGREEMENT_BAR = 0.85
MIN_SESSIONS = 40
FIT_LINK_PRECISION = 0.97
FIT_INFERRED_PRECISION = 0.95
COVERAGE_LINE = 0.60
PATH_NAMES = ("quote", "window")


# ---------------------------------------------------------------------------------------
# Clopper-Pearson, no scipy
# ---------------------------------------------------------------------------------------

def _binom_cdf(k, n, p):
    """P(X <= k) for X ~ Bin(n, p), in log space so large n cannot overflow."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    lg = math.lgamma(n + 1)
    return min(1.0, sum(math.exp(lg - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                                 + i * lp + (n - i) * lq) for i in range(k + 1)))


def cp_upper(k, n, alpha=ALPHA):
    """One-sided (1 - alpha) Clopper-Pearson upper bound for k events in n trials."""
    if n <= 0 or k >= n:
        return 1.0
    lo, hi = k / n, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


# ---------------------------------------------------------------------------------------
# Gold
# ---------------------------------------------------------------------------------------

def normalise_gold(row):
    """{"label", "turns"} or ValueError. turn: exactly one index; several: two or more;
    none: no indices."""
    label = row.get("label")
    turns = sorted({int(t) for t in row.get("turns") or []})
    if label == "turn" and len(turns) == 1 or label == "several" and len(turns) >= 2 \
            or label == "none" and not turns:
        return {"label": label, "turns": turns}
    raise ValueError(f"bad gold row for {row.get('item_ref')}: {label!r} with {len(turns)} turns")


def gold_by_ref(rows):
    return {r["item_ref"]: normalise_gold(r) for r in rows}


def merge_gold(agent, owner=None, adjudicated=None):
    """The owner always wins: adjudication, else the owner's blind answer, else the agent's."""
    out = dict(agent)
    out.update(owner or {})
    out.update(adjudicated or {})
    return out


def gold_supported(gold):
    return gold["label"] in ("turn", "several")


def is_link_correct(linked, gold, turns, item_text):
    """Pre-registration §3: the gold turn (or one of `several`), or the turn next to one of them
    with the same speaker label that carries every item token the gold turn carries."""
    if linked is None or not gold_supported(gold):
        return False
    if linked in gold["turns"]:
        return True
    item_toks = set(tokens(item_text))
    linked_turn = turns[linked]
    linked_toks = set(tokens(linked_turn.get("text") or ""))
    for g in gold["turns"]:
        if abs(g - linked) != 1 or not 0 <= g < len(turns):
            continue
        sp = turns[g].get("speaker")
        if sp is None or sp != linked_turn.get("speaker"):
            continue
        if (item_toks & set(tokens(turns[g].get("text") or ""))) <= linked_toks:
            return True
    return False


# ---------------------------------------------------------------------------------------
# Bar 2's mechanical twin check
# ---------------------------------------------------------------------------------------

def flag_id(row):
    return f"{row['item_ref']}|{row['turn']}"


# ---------------------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------------------

def score_link_precision(rows, missing_gold=0):
    """Bar 1. rows: [{"correct": bool}] per accepted link with gold."""
    n = len(rows)
    wrong = sum(1 for r in rows if not r["correct"])
    ub = cp_upper(wrong, n) if n else None
    if missing_gold or n < MIN_LINKS:
        verdict = INSUFFICIENT
    else:
        verdict = PASS if ub <= LINK_ERROR_BAR else FAIL
    return {"accepted": n, "wrong": wrong, "missing_gold": missing_gold,
            "cp_upper": ub, "verdict": verdict}


def score_twins(flags):
    """Bar 2. flags: [{"flag_id", "confirmed": True|False|None}] -- None = not yet answered."""
    confirmed = sum(1 for f in flags if f.get("confirmed") is True)
    open_ = sum(1 for f in flags if f.get("confirmed") is None)
    verdict = FAIL if confirmed else (INSUFFICIENT if open_ else PASS)
    return {"flags": len(flags), "confirmed": confirmed, "unanswered": open_,
            "verdict": verdict}


def score_run_to_run(digest_a, digest_b):
    """Bar 3."""
    if not digest_a or not digest_b:
        return {"identical": None, "verdict": INSUFFICIENT}
    same = digest_a == digest_b
    return {"identical": same, "verdict": PASS if same else FAIL}


def score_agreement(pairs, missing=0):
    """Bar 4. pairs: [(agent_gold, owner_gold)] over the owner's blind sample."""
    n = len(pairs)
    if n == 0 or missing:
        return {"n": n, "missing": missing, "agreement": None, "verdict": INSUFFICIENT}
    agree = sum(1 for a, o in pairs if a == o)
    rate = agree / n
    return {"n": n, "missing": 0, "agree": agree, "agreement": rate,
            "verdict": PASS if rate >= AGREEMENT_BAR else FAIL}


def score_inferred(rows, missing_gold=0):
    """Bar 5. rows: [{"supported": bool}] per held-out item labelled `inferred`.
    Fewer than MIN_INFERRED -> reported only (not part of the overall verdict)."""
    n = len(rows) + missing_gold
    supported = sum(1 for r in rows if r["supported"])
    ub = cp_upper(supported, len(rows)) if rows else None
    if n < MIN_INFERRED:
        verdict = REPORTED
    elif missing_gold:
        verdict = INSUFFICIENT
    else:
        verdict = PASS if ub <= INFERRED_ERROR_BAR else FAIL
    return {"inferred": n, "supported": supported, "missing_gold": missing_gold,
            "cp_upper": ub, "verdict": verdict}


def _combine(*verdicts):
    vs = set(verdicts)
    return FAIL if FAIL in vs else INSUFFICIENT if INSUFFICIENT in vs else PASS


def score_paths(link_rows, flags, missing_gold_by_path=None, paths=PATH_NAMES):
    """Bar 6. link_rows: [{"path", "correct"}]; flags: [{"path", "confirmed"}]. A path ships
    only if bars 1 and 2 both pass on its own links."""
    missing = missing_gold_by_path or {}
    per = {}
    for p in paths:
        b1 = score_link_precision([r for r in link_rows if r["path"] == p], missing.get(p, 0))
        b2 = score_twins([f for f in flags if f.get("path") == p])
        per[p] = {"link_precision": b1, "twins": b2,
                  "verdict": _combine(b1["verdict"], b2["verdict"])}
    ship = [p for p in paths if per[p]["verdict"] == PASS]
    if ship:
        verdict = PASS
    else:
        verdict = FAIL if any(v["verdict"] == FAIL for v in per.values()) else INSUFFICIENT
    return {"per_path": per, "ship_paths": ship, "verdict": verdict}


def overall_verdict(gated, n_sessions, min_sessions=MIN_SESSIONS):
    """fail > insufficient > pass; a `reported` bar is not gated."""
    vs = {v for v in gated if v != REPORTED}
    if FAIL in vs:
        return FAIL
    if INSUFFICIENT in vs or n_sessions < min_sessions:
        return INSUFFICIENT
    return PASS


# ---------------------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------------------

def evaluate_point(rows, correct, supported):
    """Fit-half numbers for one grid point. `correct(row)` / `supported(row)` return a bool,
    or None when the item has no gold."""
    linked = [r for r in rows if r["status"] == "linked"]
    inferred = [r for r in rows if r["status"] == "none_close"]
    c = [correct(r) for r in linked]
    s = [supported(r) for r in inferred]
    wrong = sum(1 for x in c if x is False)
    sup = sum(1 for x in s if x is True)
    n_l, n_i = sum(1 for x in c if x is not None), sum(1 for x in s if x is not None)
    return {"items": len(rows), "linked": len(linked), "wrong": wrong,
            "inferred": len(inferred), "inferred_supported": sup,
            "missing_gold": sum(1 for x in c + s if x is None),
            "coverage": len(linked) / len(rows) if rows else 0.0,
            "link_precision": (1 - wrong / n_l) if n_l else None,
            "inferred_precision": (1 - sup / n_i) if n_i else None}


def feasible(ev):
    return (ev["missing_gold"] == 0
            and (ev["link_precision"] is None or ev["link_precision"] >= FIT_LINK_PRECISION)
            and (ev["inferred_precision"] is None
                 or ev["inferred_precision"] >= FIT_INFERRED_PRECISION))


def choose_point(evaluated):
    """evaluated: [((floor, margin, nothing_close), eval)]. Highest coverage among feasible
    points; ties -> higher floor, higher margin, higher nothing_close. None if none feasible."""
    ok = [(pt, ev) for pt, ev in evaluated if feasible(ev)]
    if not ok:
        return None
    return max(ok, key=lambda pe: (round(pe[1]["coverage"], 12), *pe[0]))


# ---------------------------------------------------------------------------------------
# Reported, not gated
# ---------------------------------------------------------------------------------------

def coverage_report(rows):
    def cov(rs):
        n, k = len(rs), sum(1 for r in rs if r["status"] == "linked")
        return {"items": n, "linked": k, "coverage": k / n if n else None}

    def by(field):
        groups = defaultdict(list)
        for r in rows:
            groups[str(r.get(field))].append(r)
        return {g: cov(rs) for g, rs in sorted(groups.items())}

    n = len(rows)
    per_path = {p: {"linked": sum(1 for r in rows if r.get("path") == p and
                                  r["status"] == "linked")} for p in PATH_NAMES}
    for v in per_path.values():
        v["coverage"] = v["linked"] / n if n else None
    return {"all": cov(rows), "by_kind": by("kind"), "by_lang": by("lang"),
            "by_evidence_status": by("evidence_status"), "by_path": per_path,
            "status_counts": dict(sorted(Counter(str(r["status"]) for r in rows).items())),
            "guard_refusals": sum(1 for r in rows if r.get("guard") == "distinguishing_token")}


def claim_type_report(rows, supplied=None):
    """Offline only `inferred` vs unknown is derivable; observed/stated need a TEST count."""
    if supplied:
        return {"source": "supplied", **supplied}
    c = Counter(r.get("claim_type") for r in rows)
    return {"source": "offline", "inferred": c.get("inferred", 0),
            "unknown": c.get(None, 0), "observed": "not measured", "stated": "not measured"}


# ---------------------------------------------------------------------------------------
# Glue over the real files
# ---------------------------------------------------------------------------------------

class Judge:
    """Gold lookups for run rows, with the session texts the correctness rule needs."""

    def __init__(self, sessions, gold):
        self.sessions = {s["ref"]: s for s in sessions}
        self.gold = gold

    def correct(self, row):
        g = self.gold.get(row["item_ref"])
        if g is None:
            return None
        s = self.sessions[row["session"]]
        return is_link_correct(row["turn"], g, s["turns"], store.item_text_at(s, row["item_ref"]))

    def supported(self, row):
        g = self.gold.get(row["item_ref"])
        return None if g is None else gold_supported(g)

    def flags(self, rows, decisions):
        out = []
        for r in rows:
            if r["status"] != "linked" or r["turn"] is None:
                continue
            s = self.sessions[r["session"]]
            if twin_flags(store.item_text_at(s, r["item_ref"]),
                          s["turns"][r["turn"]].get("text") or ""):
                out.append({"flag_id": flag_id(r), "path": r["path"],
                            "confirmed": decisions.get(flag_id(r))})
        return out


def paths_match(run_paths, ship_paths):
    """The scored run used exactly the paths bar 6 keeps (as a set: order is not the claim)."""
    return set(run_paths or []) == set(ship_paths or [])


def flag_decisions(rows):
    """{flag_id: True|False}. Only a JSON boolean is an answer: null, "no", 0 and the like are
    unanswered, so bar 2 stays insufficient on them (pre-registration §5.2)."""
    return {r["flag_id"]: r["confirmed"] for r in rows
            if isinstance(r.get("confirmed"), bool)}


def load_gold(labels_dir=store.LABELS_DIR, data_dir=store.DATA_DIR):
    store.require_labels_on_manifest(labels_dir, data_dir)
    d = Path(labels_dir)
    agent = gold_by_ref(store.read_jsonl(d / "done.agent.jsonl"))
    owner = gold_by_ref(store.read_jsonl(d / "done.owner.jsonl"))
    adj = gold_by_ref(store.read_jsonl(d / "done.adjudicate.owner.jsonl"))
    return agent, owner, adj


def fit(grid_dir=store.RUNS_DIR / "grid", data_dir=store.DATA_DIR,
        labels_dir=store.LABELS_DIR, out_path=None):
    gmeta = store.read_json(Path(grid_dir, "meta.json"))
    if gmeta.get("split") != "fit":
        raise SystemExit(f"Refusing: the grid run is on split {gmeta.get('split')!r}, not 'fit'")
    digest = store.require_same_manifest(gmeta.get("manifest_digest"), data_dir, "grid run")
    agent, owner, adj = load_gold(labels_dir, data_dir)
    judge = Judge(store.load_sessions(data_dir, split="fit"), merge_gold(agent, owner, adj))
    by_point = defaultdict(list)
    for r in store.read_jsonl(Path(grid_dir, "rows.jsonl")):
        by_point[tuple(r["point"])].append(r)
    evaluated = [(pt, evaluate_point(rs, judge.correct, judge.supported))
                 for pt, rs in sorted(by_point.items())]
    chosen = choose_point(evaluated)
    result = {"manifest_digest": digest, "points": len(evaluated), "feasible": sum(1 for _, e in evaluated if feasible(e)),
              "chosen": None if chosen is None else
              {"floor": chosen[0][0], "margin": chosen[0][1], "nothing_close": chosen[0][2],
               **chosen[1]},
              "verdict": FAIL if chosen is None else PASS}
    store.write_json(out_path or store.RUNS_DIR / "fit" / "choice.json", result)
    return result


def build_summary(*, run_a, run_b, out_path, both_paths_run=None, data_dir=store.DATA_DIR,
                  labels_dir=store.LABELS_DIR, latency=None, claim_type_counts=None,
                  fit_choice=store.RUNS_DIR / "fit" / "choice.json"):
    """Bars 1-6 plus the reported numbers -> results/summary.json (counts only).

    `run_a`/`run_b` are two runs of the shipping configuration on the held-out half (bar 3).
    `both_paths_run` is the PATHS=("quote","window") run bar 6 is judged on; it defaults to
    run_a. If bar 6 keeps a different set of paths than run_a used, the summary says so and
    the verdict is `insufficient` until the single-path rerun is scored (pre-registration §5.6).
    """
    from scripts.item_support_eval import run as run_mod

    sessions = store.load_sessions(data_dir)
    agent, owner, adj = load_gold(labels_dir, data_dir)
    judge = Judge(sessions, merge_gold(agent, owner, adj))
    meta = store.read_json(Path(run_a, "meta.json"))
    for rd in (run_a, run_b) + ((both_paths_run,) if both_paths_run is not None else ()):
        store.require_same_manifest(store.read_json(Path(rd, "meta.json")).get("manifest_digest"),
                                    data_dir, f"run {Path(rd).name}")
    if fit_choice is not None and Path(fit_choice).exists():
        store.require_same_manifest(store.read_json(fit_choice).get("manifest_digest"),
                                    data_dir, "fit")
    rows = [r for r in store.read_jsonl(Path(run_a, "rows.jsonl")) if r["split"] == "heldout"]
    decisions = flag_decisions(store.read_jsonl(Path(labels_dir, "done.flags.owner.jsonl")))

    linked = [r for r in rows if r["status"] == "linked"]
    link_rows = [{"path": r["path"], "correct": judge.correct(r)} for r in linked]
    b1 = score_link_precision([r for r in link_rows if r["correct"] is not None],
                              sum(1 for r in link_rows if r["correct"] is None))
    flags = judge.flags(rows, decisions)
    b2 = score_twins(flags)
    b3 = score_run_to_run(run_mod.run_digest(run_a), run_mod.run_digest(run_b))
    todo_owner = store.read_jsonl(Path(labels_dir, "todo.owner.jsonl"))
    pairs = [(agent[t["item_ref"]], owner[t["item_ref"]]) for t in todo_owner
             if t["item_ref"] in agent and t["item_ref"] in owner]
    b4 = score_agreement(pairs, missing=len(todo_owner) - len(pairs))
    inferred = [judge.supported(r) for r in rows if r["status"] == "none_close"]
    b5 = score_inferred([{"supported": x} for x in inferred if x is not None],
                        sum(1 for x in inferred if x is None))

    if both_paths_run is not None:
        bp_rows = [r for r in store.read_jsonl(Path(both_paths_run, "rows.jsonl"))
                   if r["split"] == "heldout"]
        bp_link = [{"path": r["path"], "correct": judge.correct(r)}
                   for r in bp_rows if r["status"] == "linked"]
        bp_flags = judge.flags(bp_rows, decisions)
    else:
        bp_link, bp_flags = link_rows, flags
    missing_by_path = Counter(r["path"] for r in bp_link if r["correct"] is None)
    b6 = score_paths([r for r in bp_link if r["correct"] is not None], bp_flags,
                     dict(missing_by_path))
    ship_matches = paths_match(meta.get("paths"), b6["ship_paths"])

    bars = {"link_precision": b1, "twin_hard_negatives": b2, "run_to_run": b3,
            "agent_owner_agreement": b4, "inferred_precision": b5, "per_path": b6}
    n_sessions = len(sessions)
    verdict = overall_verdict([b["verdict"] for b in bars.values()], n_sessions)
    if verdict == PASS and not ship_matches:
        verdict = INSUFFICIENT
    cov = coverage_report(rows)
    held_cov = cov["all"]["coverage"]
    summary = {
        "verdict": verdict, "bars": bars,
        "point": {k: meta.get(k) for k in ("floor", "margin", "nothing_close")},
        "paths_run": list(meta.get("paths") or []), "ship_paths": b6["ship_paths"],
        "rerun_needed": not ship_matches,
        "sessions": {"all": n_sessions,
                     "heldout": sum(1 for s in sessions if s["split"] == "heldout")},
        "reported": {
            "coverage": cov,
            "claim_type": claim_type_report(rows, claim_type_counts),
            "find_items_latency": latency or "not measured",
        },
        "coverage_decision": None if held_cov is None else
        ("open topic-quote-index pre-registration" if held_cov < COVERAGE_LINE
         else "prompt unchanged"),
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit")
    f.add_argument("--grid", default=str(store.RUNS_DIR / "grid"))
    s = sub.add_parser("summary")
    s.add_argument("--run-a", required=True)
    s.add_argument("--run-b", required=True)
    s.add_argument("--both-paths-run", default=None)
    s.add_argument("--out", default=str(store.RESULTS_DIR / "summary.json"))
    s.add_argument("--latency-json", default=None,
                   help='TEST-invoke latency, e.g. {"p50_ms": .., "p95_ms": .., "n": ..}')
    s.add_argument("--claim-type-json", default=None,
                   help="claim_type counts from TEST, if measured in-VPC")
    a = p.parse_args(argv)
    if a.cmd == "fit":
        out = fit(a.grid)
    else:
        out = build_summary(run_a=a.run_a, run_b=a.run_b, out_path=a.out,
                            both_paths_run=a.both_paths_run,
                            latency=json.loads(a.latency_json) if a.latency_json else None,
                            claim_type_counts=json.loads(a.claim_type_json)
                            if a.claim_type_json else None)
    print(json.dumps(out, indent=2, sort_keys=True))
    return out


if __name__ == "__main__":
    main()
