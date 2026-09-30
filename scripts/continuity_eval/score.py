"""Pre-registered scoring for the item-continuity measurement (spec S7 "Metrics and bars").

Every bar in spec S7's table is its own function here (`score_*`), each taking a small,
already-joined input shape so it can be driven from tiny hand-built fixtures -- no run
directory, no gold file, no AWS needed to test the bar's own pass/fail/insufficient arithmetic
at its exact boundary, which is the thing spec S7 pins down before any real result exists.

`build_summary` is the glue that assembles those small inputs from a real run directory (Task
10's `runs/<shape>/<arm>/<rep>/<session>/<step>.json` files) plus a gold `assignments_done`
file (Task 11's `label.py` output, after owner adjudication). It is exercised on a tiny
hand-built run directory too, but it is deliberately not the surface spec S7's bar table pins:
the bar functions are.

`results/summary.json` is the one committed file: counts, rates and verdicts, never item text
or transcript text.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

import carry_forward
import item_continuity

from scripts.continuity_eval import label as label_mod

HARD_NEGATIVE_MIN_PAIRS = 30
LATENCY_MIN_N = 30
LATENCY_P90_MS_BAR = 20_000
CARRY_RECALL_BAR = 0.80
TRANSCRIPT_SUPPORT_TOLERANCE_PP = 0.05
AGREEMENT_BAR = 0.90

PASS, FAIL, INSUFFICIENT = "pass", "fail", "insufficient"


# ---------------------------------------------------------------------------------------
# Correctness rule shared by bars 1 and 2 (spec S7 "Pre-registered scoring")
# ---------------------------------------------------------------------------------------

def is_claim_correct(claimed_alias, gold_counterparts):
    """A claim is correct iff its alias is one of the new item's gold counterparts.

    `gold_counterparts` holding more than one alias IS how a merge scores correct (the new
    item legitimately continues several prior items, so a claim naming any one of them is
    right); the SAME prior alias being the sole gold counterpart of more than one new item IS
    how a split scores correct on each half. Neither needs special-case code -- both fall out
    of plain set membership (spec S7: "a claim onto an item that merged the prior item with
    another counts as correct; a claim onto one half of a split counts as correct")."""
    return claimed_alias in gold_counterparts


# ---------------------------------------------------------------------------------------
# Bar: wrong carries
# ---------------------------------------------------------------------------------------

def score_wrong_carries(accepted_claims):
    """accepted_claims: [{"session": str, "correct": bool}, ...] -- one row per claim the
    guards ACCEPTED (`item_continuity.resolve`'s outcome == "accepted"), already scored
    against gold via `is_claim_correct`.

    Bar (spec S7): 0 wrong overall, and reported per session -- "no '<5%' rate is claimed"
    because items within a session are correlated. `insufficient` when there are no accepted
    claims at all: nothing to pass or fail on."""
    by_session = {}
    for row in accepted_claims:
        s = by_session.setdefault(row["session"], {"accepted": 0, "wrong": 0})
        s["accepted"] += 1
        if not row["correct"]:
            s["wrong"] += 1
    total = len(accepted_claims)
    wrong = total - sum(1 for r in accepted_claims if r["correct"])
    verdict = INSUFFICIENT if total == 0 else (PASS if wrong == 0 else FAIL)
    return {"accepted": total, "wrong": wrong, "by_session": by_session, "verdict": verdict}


# ---------------------------------------------------------------------------------------
# Bar: hard-negative wrong carries
# ---------------------------------------------------------------------------------------

def score_hard_negative_wrong_carries(hard_negative_pairs, min_pairs=HARD_NEGATIVE_MIN_PAIRS):
    """hard_negative_pairs: [{"session": str, "wrongly_carried": bool}, ...] -- one row per
    pair in the hard-negative group (spec S7 "Sessions": same-topic, same-kind items gold says
    are different work), whether or not the model made a claim on it. `wrongly_carried` is
    True when an ACCEPTED claim linked the new item to this specific distractor.

    Bar: at least `min_pairs` pairs, else `insufficient` (spec S7: "with fewer, the result is
    'insufficient', not 'pass'"); otherwise 0 wrong."""
    n = len(hard_negative_pairs)
    if n < min_pairs:
        return {"pairs": n, "wrong": 0, "verdict": INSUFFICIENT}
    by_session = {}
    for row in hard_negative_pairs:
        s = by_session.setdefault(row["session"], {"pairs": 0, "wrong": 0})
        s["pairs"] += 1
        if row["wrongly_carried"]:
            s["wrong"] += 1
    wrong = sum(1 for r in hard_negative_pairs if r["wrongly_carried"])
    return {"pairs": n, "wrong": wrong, "by_session": by_session,
            "verdict": PASS if wrong == 0 else FAIL}


# ---------------------------------------------------------------------------------------
# Bar: carry recall vs text-only recall
# ---------------------------------------------------------------------------------------

def text_only_recall(prior_items, new_items, gold_pairs):
    """Text-only recall on the SAME (prior, new) pairs, computed with Track B's real
    `carry_forward.match` -- not re-derived here (controller ruling). `prior_items`/
    `new_items` are `carry_forward.match`'s own `{"id", "text", "human_touched"}` shape;
    `gold_pairs` is `[(prior_id, new_id), ...]` gold calls the same identity. `None` when
    there is nothing to recall (no gold-positive pairs)."""
    if not gold_pairs:
        return None
    pairs, _orphans = carry_forward.match(prior_items, new_items)
    matched = {(old_id, new_id) for old_id, new_id, _how in pairs}
    hit = sum(1 for pair in gold_pairs if pair in matched)
    return hit / len(gold_pairs)


def carry_recall(scored_assignments):
    """scored_assignments: [{"gold_positive": bool, "carried": bool}, ...] -- one row per
    assignment with a gold label. `gold_positive` is True when gold named at least one
    counterpart (this new item DOES continue prior work); `carried` is True when the new
    item's own assigned item_id already equals one of its gold counterparts' item_ids (item_id
    inheritance or the exact-text pass gave it the right identity with no fuzzy fallback
    needed). `None` when there are no gold-positive assignments to recall."""
    positives = [r for r in scored_assignments if r["gold_positive"]]
    if not positives:
        return None
    hit = sum(1 for r in positives if r["carried"])
    return hit / len(positives)


def score_carry_recall(carry_recall_value, text_only_recall_value, bar=CARRY_RECALL_BAR):
    """Bar (spec S7): carry recall >= 80%. Text-only recall on the same pairs is reported
    alongside for context; it is not itself gated."""
    verdict = INSUFFICIENT if carry_recall_value is None else \
        (PASS if carry_recall_value >= bar else FAIL)
    return {"carry_recall": carry_recall_value, "text_only_recall": text_only_recall_value,
            "verdict": verdict}


# ---------------------------------------------------------------------------------------
# Bar: admission drift
# ---------------------------------------------------------------------------------------

def score_admission_drift(session_metric, session_tolerance):
    """session_metric: {session: mean |with-block - baseline| over the 9 run pairs (3
    with-block reps x 3 baseline reps), averaged over kind, for that session}.
    session_tolerance: {session: the largest within-arm pairwise difference, averaged over
    both arms' 3+3 pairs and over kind, for the same session}.

    Bar: mean(session_metric) <= mean(session_tolerance), both averaged over sessions (spec
    S7: "A tolerance, not a 'must beat the noise' test: with no real effect the two
    quantities are equal in expectation" -- equality itself passes)."""
    if not session_metric or not session_tolerance:
        return {"metric": None, "tolerance": None, "verdict": INSUFFICIENT}
    metric = statistics.mean(session_metric.values())
    tolerance = statistics.mean(session_tolerance.values())
    return {"metric": metric, "tolerance": tolerance,
            "verdict": PASS if metric <= tolerance else FAIL}


# ---------------------------------------------------------------------------------------
# Bar: transcript support
# ---------------------------------------------------------------------------------------

def score_transcript_support(block_only_unsupported, block_only_total,
                              baseline_only_unsupported, baseline_only_total,
                              tolerance_pp=TRANSCRIPT_SUPPORT_TOLERANCE_PP):
    """Counts of items unique to one arm (absent from every run of the other arm, for the
    same session/shape/step) that gold judged unsupported by the transcript, over the total
    number of such arm-only items. Bar: block-only unsupported share <= baseline-only
    unsupported share + 5 percentage points."""
    if block_only_total == 0 or baseline_only_total == 0:
        return {"block_only_share": None, "baseline_only_share": None, "verdict": INSUFFICIENT}
    block_share = block_only_unsupported / block_only_total
    baseline_share = baseline_only_unsupported / baseline_only_total
    verdict = PASS if block_share <= baseline_share + tolerance_pp else FAIL
    return {"block_only_share": block_share, "baseline_only_share": baseline_share,
            "block_only_total": block_only_total, "baseline_only_total": baseline_only_total,
            "verdict": verdict}


# ---------------------------------------------------------------------------------------
# Bar: added final-pass latency
# ---------------------------------------------------------------------------------------

def _percentile(values, pct):
    """Nearest-rank percentile over a sorted copy of `values`. No numpy dependency for one
    report-only number with n in the tens."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(0, min(len(ordered) - 1, -(-len(ordered) * pct // 100) - 1))
    return ordered[rank]


def score_latency_p90(latencies_ms, min_n=LATENCY_MIN_N, bar_ms=LATENCY_P90_MS_BAR):
    """latencies_ms: one entry per final pass (spec S7: "p90 over at least 30 final passes").
    Bar: p90 <= 20s; below `min_n` samples the result is `insufficient`, not a verdict on the
    number itself."""
    n = len(latencies_ms)
    if n < min_n:
        return {"n": n, "p90_ms": None, "verdict": INSUFFICIENT}
    p90 = _percentile(latencies_ms, 90)
    return {"n": n, "p90_ms": p90, "verdict": PASS if p90 <= bar_ms else FAIL}


# ---------------------------------------------------------------------------------------
# Bar: agent-owner agreement
# ---------------------------------------------------------------------------------------

def score_agent_owner_agreement(pairs, bar=AGREEMENT_BAR):
    """pairs: [(agent_counterparts: set, owner_counterparts: set), ...] over the
    owner-adjudicated sample. Agreement is the fraction where the two sets are equal.

    Below the bar, spec S7 says the agent labels are not usable alone (and the owner labels
    another 10%) -- surfaced as `usable_alone` plus a `note`, since this bar's failure changes
    the labelling PROCESS rather than the continuity system's own numbers."""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "agreement": None, "usable_alone": False, "verdict": INSUFFICIENT}
    agree = sum(1 for a, o in pairs if a == o)
    rate = agree / n
    usable = rate >= bar
    result = {"n": n, "agreement": rate, "usable_alone": usable,
              "verdict": PASS if usable else FAIL}
    if not usable:
        result["note"] = "agent labels are not usable alone"
    return result


# ---------------------------------------------------------------------------------------
# Overall verdict
# ---------------------------------------------------------------------------------------

def overall_verdict(bar_verdicts):
    """`fail` beats `insufficient` beats `pass`: one bar actively failing means the flag stays
    off regardless of what else is still under-sampled (spec S7 "Decision rule": "Any bar
    fails: the flag stays off"). A bar that is merely under-sampled, with no fail anywhere
    else, means "not yet decided" rather than "pass" -- spec S7's "Decision rule" only ever
    licenses turning the flag on when EVERY bar passed."""
    verdicts = set(bar_verdicts)
    if FAIL in verdicts:
        return FAIL
    if INSUFFICIENT in verdicts:
        return INSUFFICIENT
    return PASS


# ---------------------------------------------------------------------------------------
# Also reported, not gated (spec S7 "Also reported, not gated")
# ---------------------------------------------------------------------------------------

def label_rate(scored_rows):
    """How often the model makes ANY continuity claim (accepted or rejected -- a rejected
    claim still shows the model tried) on an assignment gold calls positive. `scored_rows`:
    `scored_assignments`'s output rows (needs `gold_positive` and `claim_alias`)."""
    positives = [r for r in scored_rows if r["gold_positive"]]
    if not positives:
        return None
    labelled = sum(1 for r in positives if r["claim_alias"] is not None)
    return labelled / len(positives)


def pre_guard_claim_precision(scored_claims):
    """scored_claims: [{"correct": bool}, ...] for EVERY claim the model made -- accepted and
    rejected alike -- scored against gold. `None` if the model made no claims in this run."""
    if not scored_claims:
        return None
    correct = sum(1 for c in scored_claims if c["correct"])
    return correct / len(scored_claims)


def false_rejects_per_guard(scored_claims):
    """scored_claims: [{"guard": str|None, "correct": bool}, ...]. A false reject is a claim a
    guard rejected (guard is not None) that gold says was actually right -- the guard threw
    out a good claim. Grouped by guard name."""
    counts = Counter()
    for c in scored_claims:
        if c.get("guard") and c.get("correct"):
            counts[c["guard"]] += 1
    return dict(counts)


def hallucinated_alias_rate(claims):
    """Share of claims naming an alias that never existed in the offered prior list
    (`item_continuity.resolve`'s "existence" guard)."""
    if not claims:
        return None
    n = sum(1 for c in claims if c.get("guard") == "existence")
    return n / len(claims)


def cap_hits(raw_counts_by_kind, cap=item_continuity.PRIOR_CAP_PER_KIND):
    """raw_counts_by_kind: [{"session": ..., "list_name": ..., "count": n}, ...] -- eligible
    (non-empty text, valid item_id) prior children per kind, BEFORE
    `item_continuity.prior_items` caps the offered list. Returns how many such (session, kind)
    cells hit the cap."""
    return sum(1 for row in raw_counts_by_kind if row["count"] >= cap)


def added_prompt_size(continuity_block_text):
    """Ruling C4: `call_llm` returns no usage dict, so "added prompt tokens" is reported as
    prompt characters plus a chars/4 estimate, labelled as an estimate; report-only, not
    gated. `continuity_block_text` is the rendered `<<<PRIOR_ITEMS>>>` block itself -- the
    prompt content the flag actually ADDS -- not a diff of two full prompts."""
    chars = len(continuity_block_text or "")
    return {"chars": chars, "estimated_tokens": round(chars / 4),
            "note": "estimate (chars/4; call_llm has no usage dict -- ruling C4)"}


# ---------------------------------------------------------------------------------------
# Glue: real run directory + gold file -> the bar functions' small inputs
# ---------------------------------------------------------------------------------------
# This section is exercised on a tiny hand-built run directory in
# tests/unit/test_continuity_eval_score.py, but it is not the surface spec S7's bar table
# pins down -- the bar functions above are. Kept deliberately simple: one gold-scored row per
# assignment, joined by `assignment_id`, is enough to drive every bar above.

def _gold_by_assignment(gold_done_path):
    return {row["assignment_id"]: row for row in label_mod._read_jsonl(gold_done_path)}


def session_refs(session_ids):
    """Deterministic opaque per-session reference (`s01`, `s02`, ... in sorted order) so
    `results/summary.json` -- the one committed file -- never contains a raw session id.
    Run-directory session ids are `user_folder__date__session_base`, and `user_folder` is a
    person's name; the id -> ref map is meant to be written only into the gitignored run
    directory (`build_summary` does this), never into summary.json itself."""
    ordered = sorted(set(session_ids))
    width = max(2, len(str(len(ordered))))
    return {sid: f"s{str(i + 1).zfill(width)}" for i, sid in enumerate(ordered)}


def _text_key(text):
    """The same normalisation `carry_forward.match` uses, for identifying an item unique to
    one arm by text (spec S7 "transcript support": "block-only" / "baseline-only")."""
    return carry_forward._key(text or "")


def scored_assignments(run_dir, gold_done_path):
    """One row per assignment that has a gold label, joining `label.iter_assignments`
    (private `model_claim_alias`/`model_claim_guard`/`_alias_to_item_id`/`new_item_id` fields
    included) against the gold done file. Each row carries everything the bar functions above
    need: `session`, `arm`, `list_name`, `gold_counterparts` (alias set), `gold_positive`,
    `carried`, `claim_alias`/`claim_outcome`/`claim_guard` (the model's own claim, if any),
    `hard_negatives` (the gold done row's `hard_negatives` list -- prior aliases from THIS
    assignment's own prior list that the labeller judged same-kind/same-topic but different
    work; Ruling C5 -- elicited per assignment, not a corpus-wide auto-derived flag)."""
    gold = _gold_by_assignment(gold_done_path)
    out = []
    for a in label_mod.iter_assignments(run_dir):
        done = gold.get(a["assignment_id"])
        if done is None:
            continue
        gold_counterparts = label_mod._counterparts(done)
        alias_to_id = a.get("_alias_to_item_id", {})
        gold_counterpart_ids = {alias_to_id[al] for al in gold_counterparts if al in alias_to_id}
        carried = bool(gold_counterpart_ids) and a.get("new_item_id") in gold_counterpart_ids
        out.append({
            "assignment_id": a["assignment_id"], "session": a["session"], "arm": a["arm"],
            "list_name": a["list_name"], "gold_counterparts": gold_counterparts,
            "gold_counterpart_ids": gold_counterpart_ids, "new_item_id": a.get("new_item_id"),
            "gold_positive": bool(gold_counterparts), "carried": carried,
            "supported": done.get("supported"),
            "hard_negatives": list(done.get("hard_negatives") or []),
            "claim_alias": a.get("model_claim_alias"),
            "claim_outcome": a.get("model_claim_outcome"),
            "claim_guard": a.get("model_claim_guard"),
        })
    return out


def _ref(session_ref, session):
    return (session_ref or {}).get(session, session)


def accepted_claims_rows(rows, session_ref=None):
    """`score_wrong_carries` input from `scored_assignments` rows: one per accepted claim.
    `session_ref`, if given, maps each raw session id to an opaque reference (`session_refs`)
    so `session` never carries a raw folder name into a bar's output; omitted, the raw session
    id is used as-is (a caller working entirely with synthetic ids, e.g. a unit test)."""
    return [{"session": _ref(session_ref, r["session"]),
              "correct": is_claim_correct(r["claim_alias"], r["gold_counterparts"])}
            for r in rows if r["claim_outcome"] == "accepted"]


def hard_negative_rows(rows, session_ref=None):
    """`score_hard_negative_wrong_carries` input (Ruling C5): one row per (new item,
    hard-negative alias) pair the gold labeller named in that assignment's own
    `hard_negatives` list -- NOT one row per assignment. `wrongly_carried` is True only when
    an ACCEPTED claim on that assignment named exactly that hard-negative alias -- the model
    carried the new item onto the specific distractor gold flagged, not merely onto something
    gold disagrees with in general (that is the separate, broader `wrong_carries` bar)."""
    return [{"session": _ref(session_ref, r["session"]),
              "wrongly_carried": r["claim_outcome"] == "accepted" and r["claim_alias"] == alias}
            for r in rows for alias in r["hard_negatives"]]


def carry_recall_rows(rows, arm="with_block"):
    return [{"gold_positive": r["gold_positive"], "carried": r["carried"]}
            for r in rows if r["arm"] == arm]


def scored_claims_rows(rows):
    """`pre_guard_claim_precision`/`false_rejects_per_guard` input: every assignment the model
    made ANY claim on (accepted or rejected). `guard` is the REAL guard name
    (`item_continuity.resolve`'s "existence"/"kind"/"echo"/"one_to_one"/"prior_exact_matched"/
    "malformed", or None for an accepted claim) -- not the claim's coarse "accepted"/"rejected"
    outcome, which is the same literal string "rejected" for every guard and would make every
    guard indistinguishable from every other."""
    return [{"guard": r["claim_guard"],
              "correct": is_claim_correct(r["claim_alias"], r["gold_counterparts"])}
            for r in rows if r["claim_alias"] is not None]


def build_summary(run_dir, gold_done_path, out_path, *,
                   latencies_ms=(), block_only_unsupported=0, block_only_total=0,
                   baseline_only_unsupported=0, baseline_only_total=0,
                   session_admission_metric=None, session_admission_tolerance=None,
                   agreement_pairs=(), text_only_recall_value=None):
    """Assembles every bar plus the reported-not-gated stats into `results/summary.json`.

    Admission drift, transcript support, latency and agent-owner agreement need data this
    module cannot derive from `run_dir`/`gold_done_path` alone (drift needs the harness's own
    per-kind counts across reps; support and latency need the run files' timing/void fields
    cross-referenced with gold; agreement needs the separate owner-adjudication sample) -- so
    those are accepted as pre-computed arguments here, the same way the bar functions
    themselves take pre-joined inputs. Task 12 (which actually runs the measurement) supplies
    them; this function's job is only to route real assignment-level data through the bar
    functions and write one JSON file."""
    rows = scored_assignments(run_dir, gold_done_path)

    # Opaque per-session refs so no `by_session` breakdown below, and therefore no serialised
    # summary.json, ever contains a raw session id (a real run's session id is
    # `user_folder__date__session_base`, and `user_folder` is a person's name). The real
    # mapping is written only into the gitignored run directory, never returned here.
    refs = session_refs(r["session"] for r in rows)
    Path(run_dir, "session_refs.json").write_text(
        json.dumps(refs, indent=2, sort_keys=True), encoding="utf-8")

    accepted = accepted_claims_rows(rows, session_ref=refs)
    hard_neg = hard_negative_rows(rows, session_ref=refs)
    recall_rows = carry_recall_rows(rows)
    scored_claims = scored_claims_rows(rows)

    bars = {
        "wrong_carries": score_wrong_carries(accepted),
        "hard_negative_wrong_carries": score_hard_negative_wrong_carries(hard_neg),
        "carry_recall": score_carry_recall(carry_recall(recall_rows), text_only_recall_value),
        "admission_drift": score_admission_drift(session_admission_metric or {},
                                                   session_admission_tolerance or {}),
        "transcript_support": score_transcript_support(
            block_only_unsupported, block_only_total,
            baseline_only_unsupported, baseline_only_total),
        "latency_p90": score_latency_p90(list(latencies_ms)),
        "agent_owner_agreement": score_agent_owner_agreement(list(agreement_pairs)),
    }
    reported = {
        "label_rate": label_rate(rows),
        "pre_guard_claim_precision": pre_guard_claim_precision(scored_claims),
        "false_rejects_per_guard": false_rejects_per_guard(scored_claims),
        "hallucinated_alias_rate": hallucinated_alias_rate(
            [{"guard": r["claim_guard"]} for r in rows if r["claim_alias"] is not None]),
    }
    summary = {"verdict": overall_verdict(b["verdict"] for b in bars.values()),
               "bars": bars, "reported": reported}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Run directory (Task 10 output).")
    parser.add_argument("--gold", required=True, help="Canonical assignments_done.jsonl.")
    parser.add_argument("--out", default=None, help="Where to write summary.json.")
    args = parser.parse_args(argv)
    out = args.out or str(Path(args.run) / "results" / "summary.json")
    summary = build_summary(args.run, args.gold, out)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


if __name__ == "__main__":
    main()
