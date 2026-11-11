"""Pre-registered scoring for item_support (docs/superpowers/specs/
2026-11-08-item-support-eval-preregistration.md §4-6).

Each held-out bar is its own `score_*` function over a small pre-joined input, so its
pass/fail/insufficient arithmetic can be pinned at the exact boundary with hand-built
fixtures. `fit` and `build_summary` are the glue over the real data/, runs/ and labels/ files.

`results/summary.json` is the one committed file: counts, rates and verdicts only, never a
session id, a folder name, item text or turn text.

    python -m scripts.item_support_eval.score fit
    python -m scripts.item_support_eval.score summary --run-a runs/heldout_a --run-b runs/heldout_b

Locator measurement (spec 2026-11-10 section 6), over Task 6's run files and the locator's own
labels; writes runs/locator/summary.<stage>.json (gitignored, counts only):

    python -m scripts.item_support_eval.score locator --stage fit    # the fit gate
    python -m scripts.item_support_eval.score locator --stage all    # bars 1-8, pooled + halves

Locator re-test (design 2026-11-12, C5), over runs/locator_v2/<wording>/ and
labels/v2/<wording>/; writes runs/locator_v2/<wording>/summary.<stage>.json:

    python -m scripts.item_support_eval.score locator --stage tune --wording w1     # fit, B1
    python -m scripts.item_support_eval.score locator --stage heldout --wording w1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import item_locator
from evidence_match import is_cjk
from item_continuity import KINDS
from item_text_match import cn_to_int, overlap, place_values, tokens, twin_flags  # noqa: F401

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


# ---------------------------------------------------------------------------------------
# Locator measurement (item quote locator spec 2026-11-10 section 6)
# ---------------------------------------------------------------------------------------
#
# Runs are Task 6's run files (A = locator off, B = on; reps 1 and 2). Gold is the locator's
# own label set (labels/*.locator.*), never Task 10's. Every number below is the spec's.

REPORTED_ONLY, PENDING_OWNER = "reported_only", "pending_owner"
MATCH_S = 0.5                  # bar 1: s = max(overlap(a, b), overlap(b, a)) >= 0.5
ADMISSION_SLACK = 0.05         # bar 1: A1-B1 and A2-B2 >= A1-A2 share - 5 points
ITEM_COUNT_BAND = 0.10         # bar 1: B's mean items per session within +-10% of A's
MIN_NO_LOCATOR = 29            # bar 6: 0 of 29 gives a CP upper bound of 9.8%
NO_LOCATOR_ERROR_BAR = 0.10
STABILITY_BAR = 0.90           # bar 7
RUN_KEYS = ("A1", "A2", "B1", "B2")
_EPS = 1e-9                    # inclusive boundaries, safe against float noise
_KIND_ORDER = {k: i for i, k in enumerate(KINDS)}
LOCATOR_REFUSALS = tuple(s for s in item_locator.STATUSES if s not in ("linked", "no_locator"))


def run_items(rec):
    """Items of one locator run file, in `store.iter_items` order (children normalised in
    place first, for A and B alike): item_ref, kind, text, support, the model's raw
    `said_at` (B only) and the topic."""
    topics = rec.get("topics") if isinstance(rec, dict) else None
    if not isinstance(topics, list):
        return []
    raw_rows = {r.get("item"): r for r in rec.get("_raw_said_at") or [] if isinstance(r, dict)}
    out = []
    for ti, ln, ci, child, topic in store.iter_items(topics):
        sup = child.get("support")
        raw = raw_rows.get(f"{ti}:{ln}:{ci}") or {}
        # The child's own status_suggestion `at`: recorded raw before a main stage strips the
        # suggestion (re-test runs), else read off a suggestion that survived (side set).
        sugg = child.get("status_suggestion")
        status_at = raw.get("status_at") if "status_at" in raw else \
            (sugg.get("at") if isinstance(sugg, dict) else None)
        out.append({"item_ref": store.item_ref(rec["ref"], ti, ln, ci), "kind": ln,
                    "text": store.item_text(ln, child),
                    "support": sup if isinstance(sup, dict) else None,
                    "said_at": raw.get("said_at"), "status_at": status_at, "topic": topic})
    return out


def item_status(row):
    return (row.get("support") or {}).get("status") or "no_support"


def linked_turn(support, turns):
    """Whole-session index of the turn a `linked` support names (Task 10's mapping)."""
    from scripts.item_support_eval.run import linked_turn_index
    return linked_turn_index(support, turns)


def locator_link_correct(turn, gold):
    """Spec section 6: the linked turn is gold's turn or one of gold's `several`. No adjacency
    or shared-token allowance (it is vacuously true across languages)."""
    return turn is not None and gold_supported(gold) and turn in gold["turns"]


def match_items(xs, ys):
    """Bar 1's matching: greedy one to one, same kind, s >= 0.5, by s descending, ties broken
    by kind, then item position. xs/ys: [{"kind", "text"}]. Returns sorted (i, j) pairs."""
    cand = []
    for i, a in enumerate(xs):
        for j, b in enumerate(ys):
            if a["kind"] != b["kind"]:
                continue
            s = max(overlap(a["text"], b["text"])[0], overlap(b["text"], a["text"])[0])
            if s >= MATCH_S:
                cand.append((-s, _KIND_ORDER.get(a["kind"], len(_KIND_ORDER)), i, j))
    cand.sort()
    used_x, used_y, out = set(), set(), []
    for _s, _k, i, j in cand:
        if i in used_x or j in used_y:
            continue
        used_x.add(i)
        used_y.add(j)
        out.append((i, j))
    return sorted(out)


def script_of(text):
    """'zh' if CJK characters outnumber Latin letters, 'en' if not, 'none' if neither."""
    cjk = sum(1 for ch in text or "" if is_cjk(ch))
    latin = sum(1 for ch in text or "" if ch.isalpha() and not is_cjk(ch)
                and unicodedata.name(ch, "").startswith("LATIN"))
    if not cjk and not latin:
        return "none"
    return "zh" if cjk > latin else "en"


def score_admission(per_session):
    """Bar 1. per_session: [{"A1": items, "A2": .., "B1": .., "B2": ..}]. Shares pooled as
    sum(matched pairs) / sum(mean(|X|, |Y|)) over the sessions."""
    pairs = {"A1_A2": ("A1", "A2"), "A1_B1": ("A1", "B1"), "A2_B2": ("A2", "B2")}
    matched = {k: 0 for k in pairs}
    denom = {k: 0.0 for k in pairs}
    for runs in per_session:
        for k, (x, y) in pairs.items():
            matched[k] += len(match_items(runs[x], runs[y]))
            denom[k] += (len(runs[x]) + len(runs[y])) / 2
    share = {k: (matched[k] / denom[k] if denom[k] else None) for k in pairs}
    n = len(per_session)
    mean_a = sum(len(r["A1"]) + len(r["A2"]) for r in per_session) / (2 * n) if n else None
    mean_b = sum(len(r["B1"]) + len(r["B2"]) for r in per_session) / (2 * n) if n else None
    delta = (mean_b - mean_a) / mean_a if mean_a else None
    if not n or share["A1_A2"] is None or not mean_a:
        verdict = INSUFFICIENT
    else:
        floor = share["A1_A2"] - ADMISSION_SLACK
        ok_share = all(share[k] is not None and share[k] >= floor - _EPS
                       for k in ("A1_B1", "A2_B2"))
        ok_count = abs(mean_b - mean_a) <= ITEM_COUNT_BAND * mean_a + _EPS
        verdict = PASS if ok_share and ok_count else FAIL
    return {"sessions": n, "matched": matched, "share": share,
            "items_per_session": {"A": mean_a, "B": mean_b}, "item_delta": delta,
            "verdict": verdict}


def score_coverage(items, linked):
    """Bar 5: linked / B1 final items (every item, refusals included) >= 60%."""
    cov = linked / items if items else None
    verdict = INSUFFICIENT if cov is None else (PASS if cov >= COVERAGE_LINE - _EPS else FAIL)
    return {"items": items, "linked": linked, "coverage": cov, "verdict": verdict}


def score_no_locator(rows, missing_gold=0):
    """Bar 6. rows: [{"supported": bool}] per `no_locator` item with gold. Fewer than 29 items
    -> reported only and `inferred` stays off. `inferred_on` is the section 4.4 decision."""
    n = len(rows) + missing_gold
    sup = sum(1 for r in rows if r["supported"])
    ub = cp_upper(sup, len(rows)) if rows else None
    if n < MIN_NO_LOCATOR:
        verdict = REPORTED_ONLY
    elif missing_gold:
        verdict = INSUFFICIENT
    else:
        verdict = PASS if ub <= NO_LOCATOR_ERROR_BAR else FAIL
    return {"no_locator": n, "supported": sup, "missing_gold": missing_gold, "cp_upper": ub,
            "verdict": verdict, "inferred_on": verdict == PASS}


def score_stability(pairs):
    """Bar 7. pairs: [(B1 turn, B2 turn)] over matched items both runs linked."""
    n = len(pairs)
    same = sum(1 for a, b in pairs if a is not None and a == b)
    share = same / n if n else None
    verdict = INSUFFICIENT if not n else (PASS if share >= STABILITY_BAR - _EPS else FAIL)
    return {"pairs": n, "same_turn": same, "share": share, "verdict": verdict}


def score_owner_agreement(pairs, missing_owner, missing_agent=0):
    """Bar 8: agent vs owner on the owner sample. Any owner answer still owed (or no sample)
    -> pending_owner, never pass. An item the owner answered but the agent did not is
    `missing_agent` (the labeller's gap, not the owner's) -> insufficient."""
    base = {"n": len(pairs), "missing_owner": missing_owner, "missing_agent": missing_agent}
    if missing_owner or not (pairs or missing_agent):
        return {**base, "agreement": None, "verdict": PENDING_OWNER}
    if missing_agent:
        return {**base, "agreement": None, "verdict": INSUFFICIENT}
    return {**base, **score_agreement(pairs)}


def combine_locator(verdicts):
    """fail > insufficient > pending_owner > pass; reported bars are not gated."""
    vs = {v for v in verdicts if v not in (REPORTED, REPORTED_ONLY)}
    for v in (FAIL, INSUFFICIENT, PENDING_OWNER):
        if v in vs:
            return v
    return PASS


def primary_model(runs):
    """The most common `model` over the A1 runs (ties: the alphabetically first); None when
    no A1 run reports one. runs: {ref: {"A1": rec, ..}}."""
    c = Counter(r["A1"].get("model") for r in runs.values()
                if isinstance(r.get("A1"), dict) and not r["A1"].get("error")
                and r["A1"].get("model") is not None)
    return sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0] if c else None


def session_status(four, primary):
    """eligible, or why the session is out of the paired bars: missing (a run file absent),
    errored, model_unknown (a run reports no model while A1 runs do), fallback (a run was
    answered by a model other than the primary)."""
    recs = [four.get(k) for k in RUN_KEYS]
    if any(r is None for r in recs):
        return "missing"
    if any(r.get("error") or not isinstance(r.get("topics"), list) for r in recs):
        return "errored"
    if primary is not None:
        if any(r.get("model") is None for r in recs):
            return "model_unknown"
        if any(r.get("model") != primary for r in recs):
            return "fallback"
    return "eligible"


def locator_flags(sessions, runs):
    """Bar 4 candidates: every B1 link (non-errored B1 run) whose item and linked turn carry
    the same place class with different values (`twin_flags`). Holds texts: for the owner's
    flag file only, never for the summary."""
    out = []
    for s in sessions:
        rec = (runs.get(s["ref"]) or {}).get("B1")
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        for row in run_items(rec):
            t = linked_turn(row["support"], s["turns"])
            if t is None:
                continue
            turn_text = s["turns"][t].get("text") or ""
            classes = twin_flags(row["text"], turn_text)
            if classes:
                out.append({"flag_id": f"{row['item_ref']}|{t}", "item_ref": row["item_ref"],
                            "classes": classes, "item_text": row["text"],
                            "turn_text": turn_text})
    return out


def _bars(sessions, runs, status, gold, decisions):
    """Bars 1-7 over one set of sessions. 1/2/3/5/6/7 read eligible sessions only; bar 4
    reads every non-errored B1 link (more links can only add flags)."""
    elig = [s for s in sessions if status[s["ref"]] == "eligible"]
    items = {s["ref"]: {k: run_items(runs[s["ref"]][k]) for k in RUN_KEYS} for s in elig}
    b1_rows = [(s, row) for s in elig for row in items[s["ref"]]["B1"]]
    statuses = Counter(item_status(r) for _s, r in b1_rows)

    with_said = [r for _s, r in b1_rows if r["said_at"] is not None]
    n_ws_linked = sum(1 for r in with_said if item_status(r) == "linked")
    b2 = {"items": len(b1_rows), "with_said_at": len(with_said), "linked": n_ws_linked,
          "share": n_ws_linked / len(with_said) if with_said else None,
          "refusals": {k: statuses[k] for k in LOCATOR_REFUSALS if statuses.get(k)},
          "no_locator": statuses.get("no_locator", 0),
          "no_support": statuses.get("no_support", 0),
          "verdict": REPORTED}

    link_rows, missing = [], 0
    for s, r in b1_rows:
        if item_status(r) != "linked":
            continue
        g = gold.get(r["item_ref"])
        if g is None:
            missing += 1
            continue
        link_rows.append({"correct": locator_link_correct(linked_turn(r["support"], s["turns"]),
                                                          g)})
    b3 = score_link_precision(link_rows, missing)
    b3["linked"] = len(link_rows) + missing

    b4 = score_twins([{"flag_id": f["flag_id"], "confirmed": decisions.get(f["flag_id"])}
                      for f in locator_flags(sessions, runs)])
    b5 = score_coverage(len(b1_rows), statuses.get("linked", 0))

    nl = [gold.get(r["item_ref"]) for _s, r in b1_rows if item_status(r) == "no_locator"]
    b6 = score_no_locator([{"supported": gold_supported(g)} for g in nl if g is not None],
                          sum(1 for g in nl if g is None))

    pairs = []
    for s in elig:
        x, y = items[s["ref"]]["B1"], items[s["ref"]]["B2"]
        for i, j in match_items(x, y):
            if item_status(x[i]) == "linked" and item_status(y[j]) == "linked":
                pairs.append((linked_turn(x[i]["support"], s["turns"]),
                              linked_turn(y[j]["support"], s["turns"])))
    b7 = score_stability(pairs)
    return {"admission": score_admission(list(items.values())), "follow_through": b2,
            "link_precision": b3, "twin_hard_negatives": b4, "coverage": b5,
            "inferred_precision": b6, "stability": b7}


def _cov(rows):
    n, k = len(rows), sum(1 for r in rows if item_status(r) == "linked")
    return {"items": n, "linked": k, "coverage": k / n if n else None}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _mean(xs):
    xs = [x for x in xs if _num(x)]
    return sum(xs) / len(xs) if xs else None


def _out_tokens(rec):
    u = rec.get("usage")
    if not isinstance(u, dict):
        return None
    v = u.get("completion_tokens")
    if not _num(v):
        v = u.get("output_tokens")
    return v if _num(v) else None


def _paired_delta(elig, runs, value):
    """Mean over sessions of (mean B value - mean A value), sessions with both only."""
    deltas, a_all, b_all = [], [], []
    for s in elig:
        four = runs[s["ref"]]
        a = _mean([value(four[k]) for k in ("A1", "A2")])
        b = _mean([value(four[k]) for k in ("B1", "B2")])
        if a is None or b is None:
            continue
        deltas.append(b - a)
        a_all.append(a)
        b_all.append(b)
    return {"sessions": len(deltas), "mean_a": _mean(a_all), "mean_b": _mean(b_all),
            "delta_b_minus_a": _mean(deltas)}


def locator_diagnostics(sessions, runs, status):
    """Spec section 6 "Reported, not gated", over eligible sessions' B1 items. The script pair
    is the item's script -> the linked turn's, or for an unlinked item the majority script of
    its topic's labeller view (`label.view_indices`)."""
    from scripts.item_support_eval import label as label_mod   # label imports this module

    elig = [s for s in sessions if status[s["ref"]] == "eligible"]
    rows, by_pair, by_kind = [], defaultdict(list), defaultdict(list)
    ev_total = ev_seconds = 0
    for s in elig:
        rec = runs[s["ref"]]["B1"]
        for topic in rec["topics"]:
            for ev in (topic.get("evidence") if isinstance(topic, dict) else None) or []:
                if isinstance(ev, dict) and ev.get("at") is not None:
                    ev_total += 1
                    ev_seconds += item_locator._parse_stamp(str(ev["at"])) is not None
        view_script = {}
        for r in run_items(rec):
            t = linked_turn(r["support"], s["turns"])
            if t is not None:
                turn_script = script_of(s["turns"][t].get("text") or "")
            else:
                key = id(r["topic"])
                if key not in view_script:
                    idx = label_mod.view_indices(r["topic"], s["turns"], s["date"])
                    view_script[key] = script_of(" ".join(s["turns"][i].get("text") or ""
                                                          for i in idx))
                turn_script = view_script[key]
            rows.append(r)
            by_kind[r["kind"]].append(r)
            by_pair[f"{script_of(r['text'])}->{turn_script}"].append(r)

    linked = [r for r in rows if item_status(r) == "linked"]

    def rate(count, of):
        return {"count": count, "of": of, "rate": count / of if of else None}

    copied = 0
    with_said = [r for r in linked if r["said_at"] is not None]
    for r in with_said:
        stamp = item_locator._parse_stamp(r["said_at"])
        ats = {item_locator._parse_stamp(str(ev.get("at")))
               for ev in r["topic"].get("evidence") or [] if isinstance(ev, dict)}
        copied += stamp is not None and stamp in ats - {None}
    stray = [rec.get("status_suggestions_stripped") for s in elig
             for rec in (runs[s["ref"]]["B1"], runs[s["ref"]]["B2"])]
    stray = [x for x in stray if _num(x)]
    return {
        "coverage": {"all": _cov(rows),
                     "by_kind": {k: _cov(v) for k, v in sorted(by_kind.items())},
                     "by_script_pair": {k: _cov(v) for k, v in sorted(by_pair.items())}},
        "rates": {
            "ambiguous_stamp": rate(sum(1 for r in rows if item_status(r) == "ambiguous_stamp"),
                                    len(rows)),
            "not_shown": rate(sum(1 for r in rows if item_status(r) == "not_shown"), len(rows)),
            "range_checked_false": rate(sum(1 for r in linked
                                            if r["support"].get("range_checked") is False),
                                        len(linked))},
        # The final schema asks for HH:MM:SS evidence stamps; a stamp without seconds can
        # never match, so the share is read beside how many stamps carry seconds.
        "said_at_equals_evidence_at": {"links": len(with_said), "equal": copied,
                                       "share": copied / len(with_said) if with_said else None,
                                       "evidence_stamps": ev_total,
                                       "evidence_stamps_with_seconds": ev_seconds},
        "output_tokens": _paired_delta(elig, runs, _out_tokens),
        "latency_s": _paired_delta(elig, runs, lambda rec: rec.get("latency_s")),
        "stray_status_suggestions_on_b": {"count": sum(stray), "runs_reporting": len(stray)},
    }


def side_table(side_runs):
    """Side set (reported only): status suggestions offered / kept / dropped_by per arm and
    rep, the mean kept per run under each arm, and a flag when kept drops under B."""
    per = {f"{a}{r}": {"runs": 0, "offered": 0, "kept": 0, "dropped_by": {}, "no_counts": 0}
           for a in ("A", "B") for r in (1, 2)}
    kept = {"A": [], "B": []}
    for rec in side_runs:
        key = f"{rec.get('arm')}{rec.get('rep')}"
        if key not in per:
            continue
        p = per[key]
        p["runs"] += 1
        c = rec.get("status_suggestions")
        if rec.get("error") or not isinstance(c, dict) or c.get("error") \
                or not _num(c.get("kept")):
            p["no_counts"] += 1
            continue
        p["offered"] += int(c.get("offered") or 0)
        p["kept"] += int(c["kept"])
        for reason, n in (c.get("dropped_by") or {}).items():
            p["dropped_by"][reason] = p["dropped_by"].get(reason, 0) + int(n or 0)
        kept[rec["arm"]].append(c["kept"])
    mean = {a: _mean(v) for a, v in kept.items()}
    delta = mean["B"] - mean["A"] if mean["A"] is not None and mean["B"] is not None else None
    return {"per_run": per, "mean_kept": mean, "kept_delta_b_minus_a": delta,
            "kept_drops_under_b": delta is not None and delta < 0}


def _session_counts(sessions, status, primary):
    c = Counter(status[s["ref"]] for s in sessions)
    return {"scored": len(sessions), "eligible": c.get("eligible", 0),
            "fallback_excluded": c.get("fallback", 0),
            "model_unknown_excluded": c.get("model_unknown", 0),
            "errored_excluded": c.get("errored", 0), "missing_excluded": c.get("missing", 0),
            "primary_model": primary}


def score_locator(sessions, runs, *, gold, owner_gold=None, owner_todo=None,
                  flag_decisions=None, side_runs=None, stage="all", primary=None):
    """Bars 1-8, the fit gate, pooled and per-half figures and the reported diagnostics.

    sessions: session dicts (ref, split, date, decoded turns); runs: {ref: {"A1": rec, ..}};
    gold: the blind agent's answers {item_ref: gold}; owner_gold: the owner's blind answers on
    the sample (they win over the agent's for bars 3 and 6); owner_todo: the owner sample
    rows; flag_decisions: {flag_id: bool}. stage "fit" scores the fit half and its gate only.
    Paired bars (1, 7) and B1's bars 2/3/5/6 read eligible sessions only (`session_status`).
    `primary` (the fallback test's model) defaults to the mode over every A1 run in `runs`
    (all stages on disk), so the fit gate, the halves and the owner sample share one
    exclusion set. Bar 6 never gates the verdict: it sets `inferred_on` only."""
    owner_gold = owner_gold or {}
    merged = merge_gold(gold, owner_gold)
    decisions = flag_decisions or {}
    scored = [s for s in sessions if stage == "all" or s["split"] == stage]
    primary = primary_model(runs) if primary is None else primary
    status = {s["ref"]: session_status(runs.get(s["ref"]) or {}, primary) for s in scored}
    halves = {h: [s for s in scored if s["split"] == h]
              for h in (("fit", "heldout") if stage == "all" else (stage,))}
    per_half = {h: _bars(ss, runs, status, merged, decisions) for h, ss in halves.items()}
    pooled = _bars(scored, runs, status, merged, decisions)

    fit = per_half.get("fit") or _bars([], runs, status, merged, decisions)
    gate_bars = {k: fit[k]["verdict"] for k in ("admission", "link_precision",
                                                 "twin_hard_negatives")}
    links = fit["link_precision"]["linked"]
    gate = INSUFFICIENT if links < MIN_LINKS else combine_locator(gate_bars.values())
    fit_gate = {"links": links, "bars": gate_bars, "verdict": gate}

    summary = {"stage": stage, "fit_gate": fit_gate, "bars": pooled, "per_half": per_half,
               "sessions": {**_session_counts(scored, status, primary),
                            "per_half": {h: _session_counts(ss, status, primary)
                                         for h, ss in halves.items()}},
               "reported": locator_diagnostics(scored, runs, status),
               "side_set": side_table(side_runs) if side_runs else None}
    if stage != "all":
        summary["verdict"] = gate
        return summary

    todo = owner_todo or []
    refs = [t["item_ref"] for t in todo]
    pairs = [(gold[r], owner_gold[r]) for r in refs if r in gold and r in owner_gold]
    pooled["agent_owner_agreement"] = score_owner_agreement(
        pairs, sum(1 for r in refs if r not in owner_gold),
        sum(1 for r in refs if r in owner_gold and r not in gold))
    gated = [pooled[k]["verdict"] for k in ("admission", "link_precision",
                                            "twin_hard_negatives", "coverage", "stability",
                                            "agent_owner_agreement")]
    if any(not summary["sessions"]["per_half"][h]["eligible"] for h in halves):
        gated.append(INSUFFICIENT)
    summary["verdict"] = combine_locator(gated)
    summary["inferred_on"] = pooled["inferred_precision"]["inferred_on"]
    return summary


# ---------------------------------------------------------------------------------------
# Locator re-test (design 2026-11-12, C5): runs/locator_v2/<wording>/, tune and held-out
# ---------------------------------------------------------------------------------------
#
# Eligibility reads the deployed primary each re-test run file records (`primary_model`,
# read back from llm_utils at run time), never a mode over A1 runs: the tune step has no A
# runs at all. The tune decision is section 4 step 2's rule.

CONTINUE, ASK_OWNER, STOP, PENDING_LABELS = "continue", "ask_owner", "stop", "pending_labels"
TUNE_CONTINUE_MAX_WRONG = 5    # continue: wrong <= 5 and coverage >= 60%
TUNE_STOP_ABOVE_WRONG = 9      # stop: wrong > 9 or coverage < 60%; ask_owner in between
B1_ONLY = ("B1",)
A_KEYS = ("A1", "A2")
_FIRST_TEST_EFFORT = ("not recorded: the first test copied no LLM_REASONING_EFFORT and ran "
                      "at the provider default (design 2026-11-12 section 1)")


def session_status_recorded(four, keys=RUN_KEYS):
    """eligible, or why not, for re-test runs: missing, errored, model_unknown (the run
    reports no answering model), primary_unknown (the run file records no deployed primary),
    fallback (answered by a model other than the deployed primary it records)."""
    recs = [four.get(k) for k in keys]
    if any(r is None for r in recs):
        return "missing"
    if any(r.get("error") or not isinstance(r.get("topics"), list) for r in recs):
        return "errored"
    if any(r.get("model") is None for r in recs):
        return "model_unknown"
    if any(r.get("primary_model") is None for r in recs):
        return "primary_unknown"
    if any(r.get("model") != r.get("primary_model") for r in recs):
        return "fallback"
    return "eligible"


def _recorded_counts(sessions, status, runs, keys):
    c = Counter(status[s["ref"]] for s in sessions)
    recs = [runs.get(s["ref"], {}).get(k) for s in sessions for k in keys]
    recs = [r for r in recs if isinstance(r, dict)]
    return {"scored": len(sessions), "eligible": c.get("eligible", 0),
            "fallback_excluded": c.get("fallback", 0),
            "model_unknown_excluded": c.get("model_unknown", 0),
            "primary_unknown_excluded": c.get("primary_unknown", 0),
            "errored_excluded": c.get("errored", 0), "missing_excluded": c.get("missing", 0),
            "primary_models": sorted({str(r.get("primary_model")) for r in recs}),
            "reasoning_effort": sorted({str(r.get("reasoning_effort")) for r in recs})}


def tune_decision(wrong, coverage, missing_gold=0):
    """Section 4 step 2. A link without gold makes the count unknown: pending_labels, never
    a decision on partial gold. No items at all: insufficient."""
    if missing_gold:
        return PENDING_LABELS
    if coverage is None:
        return INSUFFICIENT
    if wrong > TUNE_STOP_ABOVE_WRONG or coverage < COVERAGE_LINE - _EPS:
        return STOP
    return CONTINUE if wrong <= TUNE_CONTINUE_MAX_WRONG else ASK_OWNER


def status_at_diagnostic(rows):
    """Reported only: links whose `said_at` equals the child's own status_suggestion `at`
    (both parsed as stamps). rows: B1 item rows (`run_items`)."""
    linked = [r for r in rows if item_status(r) == "linked"]
    with_at = [r for r in linked
               if item_locator._parse_stamp(str(r.get("status_at"))) is not None]
    equal = sum(1 for r in with_at
                if item_locator._parse_stamp(str(r.get("said_at"))) is not None
                and item_locator._parse_stamp(str(r["said_at"]))
                == item_locator._parse_stamp(str(r["status_at"])))
    return {"links": len(linked), "with_status_at": len(with_at), "equal": equal,
            "share_of_links": equal / len(linked) if linked else None,
            "share_of_with_status_at": equal / len(with_at) if with_at else None,
            "verdict": REPORTED}


def _link_rows(b1_rows, gold):
    """(bar-3 rows with gold, missing-gold count) over (session, item row) pairs."""
    rows, missing = [], 0
    for s, r in b1_rows:
        if item_status(r) != "linked":
            continue
        g = gold.get(r["item_ref"])
        if g is None:
            missing += 1
            continue
        rows.append({"correct": locator_link_correct(linked_turn(r["support"], s["turns"]), g)})
    return rows, missing


def admission_early(sessions, a_runs, b_runs, b_status):
    """Reported only, 0 calls: the first test's fit A1/A2 (A sha unchanged) against the
    re-test's B1. Sessions: B1 eligible here and A1/A2 both eligible in the first test (its
    own fallback rule: the mode over its A1 runs). Shares pooled as in bar 1; the item delta
    is B1's mean items per session against the mean of A1 and A2."""
    primary = primary_model(a_runs)
    used, matched_ab, denom_ab, matched_aa, denom_aa = [], 0, 0.0, 0, 0.0
    n_a, n_b = 0, 0
    for s in sessions:
        a = a_runs.get(s["ref"]) or {}
        recs = [a.get(k) for k in A_KEYS]
        if b_status.get(s["ref"]) != "eligible" or any(
                r is None or r.get("error") or not isinstance(r.get("topics"), list)
                or (primary is not None and r.get("model") != primary) for r in recs):
            continue
        a1, a2 = (run_items(r) for r in recs)
        b1 = run_items(b_runs[s["ref"]]["B1"])
        used.append(s["ref"])
        matched_ab += len(match_items(a1, b1))
        denom_ab += (len(a1) + len(b1)) / 2
        matched_aa += len(match_items(a1, a2))
        denom_aa += (len(a1) + len(a2)) / 2
        n_a += len(a1) + len(a2)
        n_b += len(b1)
    k = len(used)
    mean_a = n_a / (2 * k) if k else None
    mean_b = n_b / k if k else None
    b_effort = sorted({str((b_runs[r]["B1"]).get("reasoning_effort")) for r in used})
    a_effort = sorted({str(rec.get("reasoning_effort")) for r in used
                       for rec in (a_runs[r]["A1"], a_runs[r]["A2"])
                       if rec.get("reasoning_effort") is not None})
    return {"sessions": k,
            "share": {"A1_B1": matched_ab / denom_ab if denom_ab else None,
                      "A1_A2_first_test": matched_aa / denom_aa if denom_aa else None},
            "matched": {"A1_B1": matched_ab, "A1_A2_first_test": matched_aa},
            "items_per_session": {"A": mean_a, "B1": mean_b},
            "item_delta": (mean_b - mean_a) / mean_a if mean_a else None,
            "a_primary_model": primary,
            "effort": {"A": a_effort or _FIRST_TEST_EFFORT, "B1": b_effort,
                       "note": "A and B ran at different reasoning effort; the share and "
                               "delta mix the wording change with the effort change"},
            "verdict": REPORTED}


def score_locator_tune(sessions, runs, *, gold, flag_decisions=None, a_runs=None):
    """`score locator --stage tune`: the fit half, B1 only. sessions: fit sessions; runs:
    {ref: {"B1": rec}} from the wording's namespace; gold: the blind agent's answers;
    a_runs: the first test's {ref: {"A1", "A2"}} for `admission_early` (None: not reported)."""
    decisions = flag_decisions or {}
    status = {s["ref"]: session_status_recorded(runs.get(s["ref"]) or {}, B1_ONLY)
              for s in sessions}
    elig = [s for s in sessions if status[s["ref"]] == "eligible"]
    b1_rows = [(s, row) for s in elig for row in run_items(runs[s["ref"]]["B1"])]
    statuses = Counter(item_status(r) for _s, r in b1_rows)
    link_rows, missing = _link_rows(b1_rows, gold)
    b3 = score_link_precision(link_rows, missing)
    b3["linked"] = len(link_rows) + missing
    b4 = score_twins([{"flag_id": f["flag_id"], "confirmed": decisions.get(f["flag_id"])}
                      for f in locator_flags(elig, runs)])
    cov = score_coverage(len(b1_rows), statuses.get("linked", 0))
    decision = tune_decision(b3["wrong"], cov["coverage"], missing)
    return {"stage": "tune",
            "decision": decision,
            "decision_rule": {"continue": f"wrong <= {TUNE_CONTINUE_MAX_WRONG} and coverage "
                                          f">= {COVERAGE_LINE}",
                              "ask_owner": f"wrong {TUNE_CONTINUE_MAX_WRONG + 1}-"
                                           f"{TUNE_STOP_ABOVE_WRONG} and coverage >= "
                                           f"{COVERAGE_LINE}",
                              "stop": f"wrong > {TUNE_STOP_ABOVE_WRONG} or coverage < "
                                      f"{COVERAGE_LINE}"},
            "bars": {"link_precision": b3, "twin_hard_negatives": b4, "coverage": cov},
            "refusals": {k: statuses[k] for k in LOCATOR_REFUSALS if statuses.get(k)},
            "statuses": dict(sorted(statuses.items())),
            "status_suggestion_at": status_at_diagnostic([r for _s, r in b1_rows]),
            "admission_early": None if a_runs is None else
            admission_early(sessions, a_runs, runs, status),
            "sessions": _recorded_counts(sessions, status, runs, B1_ONLY)}


def score_stability_several_aware(pairs):
    """Bar 7, reported beside the strict share: a pair also counts when both picks fall
    inside gold's `several` turns for the B1 item. pairs: [(B1 turn, B2 turn, gold|None)]."""
    n = len(pairs)
    same = sum(1 for a, b, g in pairs if a is not None and (
        a == b or (g is not None and g["label"] == "several" and a in g["turns"]
                   and b in g["turns"])))
    return {"pairs": n, "same_or_both_in_several": same, "share": same / n if n else None,
            "verdict": REPORTED}


def score_locator_heldout(sessions, runs, *, gold, owner_gold=None, owner_todo=None,
                          flag_decisions=None, side_runs=None):
    """`score locator --stage heldout`: the held-out half from the wording's namespace.
    Bars 1, 3, 4, 5, 7 gate (bar 7 strictly; the several-aware share is reported beside it),
    bar 6 is reported only, bar 8 is the owner's agreement; the verdict order is unchanged
    (fail > insufficient > pending_owner > pass)."""
    owner_gold = owner_gold or {}
    merged = merge_gold(gold, owner_gold)
    status = {s["ref"]: session_status_recorded(runs.get(s["ref"]) or {}, RUN_KEYS)
              for s in sessions}
    bars = _bars(sessions, runs, status, merged, flag_decisions or {})
    b6 = bars["inferred_precision"]
    b6.update({"computed_verdict": b6["verdict"], "verdict": REPORTED_ONLY,
               "inferred_on": False})

    elig = [s for s in sessions if status[s["ref"]] == "eligible"]
    pairs, b1_rows = [], []
    for s in elig:
        x = run_items(runs[s["ref"]]["B1"])
        y = run_items(runs[s["ref"]]["B2"])
        b1_rows.extend(x)
        for i, j in match_items(x, y):
            if item_status(x[i]) == "linked" and item_status(y[j]) == "linked":
                pairs.append((linked_turn(x[i]["support"], s["turns"]),
                              linked_turn(y[j]["support"], s["turns"]),
                              merged.get(x[i]["item_ref"])))
    bars["stability_several_aware"] = score_stability_several_aware(pairs)

    todo = owner_todo or []
    refs = [t["item_ref"] for t in todo]
    agree = [(gold[r], owner_gold[r]) for r in refs if r in gold and r in owner_gold]
    bars["agent_owner_agreement"] = score_owner_agreement(
        agree, sum(1 for r in refs if r not in owner_gold),
        sum(1 for r in refs if r in owner_gold and r not in gold))
    gated = [bars[k]["verdict"] for k in ("admission", "link_precision",
                                          "twin_hard_negatives", "coverage", "stability",
                                          "agent_owner_agreement")]
    if not elig:
        gated.append(INSUFFICIENT)
    return {"stage": "heldout", "verdict": combine_locator(gated), "bars": bars,
            "status_suggestion_at": status_at_diagnostic(b1_rows),
            "sessions": _recorded_counts(sessions, status, runs, RUN_KEYS),
            "reported": locator_diagnostics(sessions, runs, status),
            "side_set": side_table(side_runs) if side_runs else None}


# ---------------------------------------------------------------------------------------
# Locator glue over the real files
# ---------------------------------------------------------------------------------------

def load_locator_runs(sessions, runs_dir=store.RUNS_DIR, *, wording=None, keys=RUN_KEYS):
    """{ref: {"A1": rec, ..}} for the run files that exist (re-test: under the wording's
    namespace; `keys` restricts which arms/reps are read)."""
    out = {}
    for s in sessions:
        four = {}
        for key in keys:
            p = store.locator_run_path(key[0], int(key[1]), s["ref"], runs_dir=runs_dir,
                                       wording=wording)
            if p.exists():
                four[key] = store.read_json(p)
        out[s["ref"]] = four
    return out


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_frozen_runs(runs, digest):
    """Refuse runs made on another manifest, or an arm whose runs carry two prompt shas."""
    shas = defaultdict(set)
    for four in runs.values():
        for key, rec in four.items():
            if rec.get("manifest_digest") != digest:
                raise SystemExit(f"Refusing: a {key} run was made on manifest "
                                 f"{rec.get('manifest_digest')}, data/ now holds {digest}.")
            if rec.get("prompt_sha") is not None:
                shas[key[0]].add(rec["prompt_sha"])
    mixed = sorted(arm for arm, s in shas.items() if len(s) > 1)
    if mixed:
        raise SystemExit(f"Refusing: arm(s) {mixed} carry more than one prompt_sha; the prompt "
                         f"is frozen (a change is a dated addendum and a complete new run).")


def require_labels_on_b1(todo_rows, runs_dir=store.RUNS_DIR, wording=None):
    """Labels are on B1 items, which exist only in one B1 output: refuse a todo row whose
    recorded B1 run sha differs from the B1 run file now on disk (a retried run)."""
    current = {}
    for row in todo_rows:
        ref = row["item_ref"].split(":")[0]
        if ref not in current:
            p = store.locator_run_path("B", 1, ref, runs_dir=runs_dir, wording=wording)
            current[ref] = file_sha(p) if p.exists() else None
        if row.get("run_sha") != current[ref]:
            raise SystemExit("Refusing: locator labels were made on another B1 run than the "
                             "one on disk; re-export and relabel that session.")


def _side_runs(data_dir, runs_dir, wording=None):
    base = Path(store.locator_runs_base(runs_dir, wording), store.SIDE_SUBDIR)
    recs = [store.read_json(p) for p in sorted(base.glob("*/*/*.json"))]
    if not recs:
        return None
    m = Path(data_dir, store.SIDE_SUBDIR, "manifest.json")
    digest = file_sha(m) if m.exists() else None
    if any(r.get("manifest_digest") != digest for r in recs):
        raise SystemExit("Refusing: side runs were made on another side manifest.")
    return recs


def require_wording_on_runs(runs, wording):
    """Refuse a re-test run file that records another wording than its namespace's."""
    for four in runs.values():
        for key, rec in four.items():
            if rec.get("wording") != wording:
                raise SystemExit(f"Refusing: a {key} run under locator_v2/{wording}/ records "
                                 f"wording {rec.get('wording')!r}.")


def _retest_summary(stage, wording, *, data_dir, runs_dir, labels_dir, out_path):
    """`tune` / `heldout` over runs/locator_v2/<wording>/ and labels/v2/<wording>/ ->
    runs/locator_v2/<wording>/summary.<stage>.json (counts only)."""
    store.check_wording(wording)
    digest = store.manifest_digest(data_dir)
    split = "fit" if stage == "tune" else "heldout"
    sessions = store.load_sessions(data_dir, split=split)
    keys = B1_ONLY if stage == "tune" else RUN_KEYS
    runs = load_locator_runs(sessions, runs_dir, wording=wording, keys=keys)
    require_frozen_runs(runs, digest)
    require_wording_on_runs(runs, wording)
    labels = store.locator_labels_dir(labels_dir, wording)
    store.require_labels_on_manifest(labels, data_dir, store.LOCATOR_LABEL_FILES)
    agent_todo = store.read_jsonl(labels / "todo.locator.agent.jsonl")
    owner_todo = store.read_jsonl(labels / "todo.owner.locator.jsonl")
    require_labels_on_b1(agent_todo + owner_todo, runs_dir, wording)
    gold = gold_by_ref(store.read_jsonl(labels / "done.locator.agent.jsonl"))
    decisions = flag_decisions(store.read_jsonl(labels / "done.flags.owner.locator.jsonl"))
    if stage == "tune":
        # The first test's fit A1/A2 (runs/locator/), for admission_early only.
        a_runs = load_locator_runs(sessions, runs_dir, keys=A_KEYS)
        if any(a_runs.values()):
            require_frozen_runs(a_runs, digest)
        else:
            a_runs = None
        summary = score_locator_tune(sessions, runs, gold=gold, flag_decisions=decisions,
                                     a_runs=a_runs)
    else:
        summary = score_locator_heldout(
            sessions, runs, gold=gold,
            owner_gold=gold_by_ref(store.read_jsonl(labels / "done.owner.locator.jsonl")),
            owner_todo=owner_todo, flag_decisions=decisions,
            side_runs=_side_runs(data_dir, runs_dir, wording))
    summary.update({"wording": wording, "manifest_digest": digest})
    store.write_json(out_path or Path(store.locator_runs_base(runs_dir, wording),
                                      f"summary.{stage}.json"), summary)
    return summary


def locator_summary(stage, *, data_dir=store.DATA_DIR, runs_dir=store.RUNS_DIR,
                    labels_dir=store.LABELS_DIR, out_path=None, wording=None):
    """Score the locator runs -> runs/locator/summary.<stage>.json (counts only). The
    re-test stages `tune` and `heldout` need a wording and read runs/locator_v2/<wording>/;
    `fit` and `all` read the first test's runs/locator/ and take no wording."""
    if stage in ("tune", "heldout"):
        if wording is None:
            raise SystemExit(f"Refusing: --stage {stage} needs --wording w1|w2.")
        return _retest_summary(stage, wording, data_dir=data_dir, runs_dir=runs_dir,
                               labels_dir=labels_dir, out_path=out_path)
    if stage not in ("fit", "all"):
        raise SystemExit(f"Unknown stage {stage!r}")
    if wording is not None:
        raise SystemExit(f"Refusing: --stage {stage} scores the first test (runs/locator/); "
                         f"--wording is for tune and heldout only.")
    digest = store.manifest_digest(data_dir)
    sessions = store.load_sessions(data_dir)      # all stages: one primary, one exclusion set
    runs = load_locator_runs(sessions, runs_dir)
    require_frozen_runs(runs, digest)
    store.require_labels_on_manifest(labels_dir, data_dir, store.LOCATOR_LABEL_FILES)
    d = Path(labels_dir)
    agent_todo = store.read_jsonl(d / "todo.locator.agent.jsonl")
    owner_todo = store.read_jsonl(d / "todo.owner.locator.jsonl")
    require_labels_on_b1(agent_todo + owner_todo, runs_dir)
    summary = score_locator(
        sessions, runs, gold=gold_by_ref(store.read_jsonl(d / "done.locator.agent.jsonl")),
        owner_gold=gold_by_ref(store.read_jsonl(d / "done.owner.locator.jsonl")),
        owner_todo=owner_todo,
        flag_decisions=flag_decisions(store.read_jsonl(d / "done.flags.owner.locator.jsonl")),
        side_runs=_side_runs(data_dir, runs_dir), stage=stage)
    summary["manifest_digest"] = digest
    store.write_json(out_path or Path(runs_dir, "locator", f"summary.{stage}.json"), summary)
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
    lo = sub.add_parser("locator")
    lo.add_argument("--stage", required=True, choices=("fit", "all", "tune", "heldout"))
    lo.add_argument("--wording", default=None, choices=store.WORDINGS,
                    help="Re-test stages (tune, heldout): runs/locator_v2/<wording>/.")
    lo.add_argument("--data", default=str(store.DATA_DIR))
    lo.add_argument("--runs", default=str(store.RUNS_DIR))
    lo.add_argument("--labels", default=str(store.LABELS_DIR))
    lo.add_argument("--out", default=None)
    a = p.parse_args(argv)
    if a.cmd == "fit":
        out = fit(a.grid)
    elif a.cmd == "locator":
        out = locator_summary(a.stage, data_dir=a.data, runs_dir=a.runs, labels_dir=a.labels,
                              out_path=a.out, wording=a.wording)
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
