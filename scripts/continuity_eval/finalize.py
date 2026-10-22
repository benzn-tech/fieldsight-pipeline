"""Task 12 scoring glue: derive the four pre-computed inputs build_summary takes (admission
drift, transcript support, latency, agent-owner agreement) plus text-only recall, then call
score.build_summary. Run from the repo root.

usage: python scripts/continuity_eval/finalize.py RUN_DIR AGENT_DONE OUT_SUMMARY [OWNER_DECISIONS REVIEW_PRIVATE]

OWNER_DECISIONS is {review_id: {counterpart, supported}} exported from the review page;
REVIEW_PRIVATE maps review_id -> assignment_id. Owner labels override agent labels (gold),
and the random-sample rows give the agreement pairs.

Rulings applied here (recorded in the plan ledger when committed):
  C7 transcript support: "absent from all baselines" is judged with Track B's own
     carry_forward.match (exact + fuzzy >= 0.90) against every baseline run of the same
     session/shape/step; symmetric for baseline-only. Cross-arm identity is unlabelled, so
     text is the only operable rule; rewordings land on both sides alike.
  C8 latency: "added final-pass latency" = p90(with-block final passes) - p90(baseline final
     passes); n is the with-block final-pass count. Absolute p90s reported alongside.
  C9 admission drift unit: one (session, shape) cell = "session"; counts are per kind from
     each chain's final step; chains with an error step are skipped.
  C11 owner decisions made on a prior list that differs from the one the model saw (the
     label.py steps[i-1] bug) are dropped and counted, not applied.
"""
import itertools
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, ".")
import carry_forward  # noqa: E402
import item_continuity  # noqa: E402
from scripts.continuity_eval import label as label_mod  # noqa: E402
from scripts.continuity_eval import score  # noqa: E402

KINDS = list(item_continuity.KINDS)


def load_steps(chain_dir):
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(chain_dir.glob("*.json"), key=lambda p: int(p.stem))]


def chains(run_dir):
    root = Path(run_dir) / "runs"
    for shape_dir in sorted(root.iterdir()):
        for arm_dir in sorted(shape_dir.iterdir()):
            for rep_dir in sorted(arm_dir.iterdir()):
                for sess_dir in sorted(rep_dir.iterdir()):
                    yield shape_dir.name, arm_dir.name, rep_dir.name, sess_dir.name, sess_dir


def kind_counts(topics):
    counts = {k: 0 for k in KINDS}
    for list_name, child in item_continuity._children(topics):
        text = (child.get(item_continuity.KINDS[list_name].text_field) or "").strip()
        if text:
            counts[list_name] += 1
    return counts


def items_of(topics):
    out = []
    for list_name, child in item_continuity._children(topics):
        text = (child.get(item_continuity.KINDS[list_name].text_field) or "").strip()
        if text:
            out.append((list_name, text))
    return out


def gather(run_dir):
    finals = {}      # (session, shape) -> arm -> [counts per rep]
    latencies = {"baseline": [], "with_block": []}
    step_items = {}  # (session, shape, step) -> arm -> [[(list_name, text)] per rep]
    for shape, arm, rep, sess, d in chains(run_dir):
        steps = load_steps(d)
        if not steps or any(s.get("error") for s in steps):
            continue
        final = steps[-1]
        topics = (final.get("extraction") or {}).get("topics")
        finals.setdefault((sess, shape), {}).setdefault(arm, []).append(kind_counts(topics))
        latencies[arm].append(final["latency_ms"])
        for i, s in enumerate(steps):
            step_items.setdefault((sess, shape, i), {}).setdefault(arm, []).append(
                items_of((s.get("extraction") or {}).get("topics")))
    return finals, latencies, step_items


def admission_drift(finals):
    metric, tolerance = {}, {}
    for cell, arms in finals.items():
        w, b = arms.get("with_block", []), arms.get("baseline", [])
        if len(w) < 2 or len(b) < 2:
            continue
        m_k, t_k = [], []
        for k in KINDS:
            m_k.append(statistics.mean(abs(x[k] - y[k]) for x in w for y in b))
            within = [max(abs(x[k] - y[k]) for x, y in itertools.combinations(arm, 2))
                      for arm in (w, b)]
            t_k.append(statistics.mean(within))
        key = "|".join(cell)
        metric[key], tolerance[key] = statistics.mean(m_k), statistics.mean(t_k)
    return metric, tolerance


def arm_only_keys(step_items):
    """{(session, shape, step, arm, rep, new_index_in_step): True} for items absent from every
    run of the other arm (carry_forward.match exact or fuzzy, same kind)."""
    only = set()
    for (sess, shape, step), arms in step_items.items():
        for arm, other in (("with_block", "baseline"), ("baseline", "with_block")):
            for rep_idx, items in enumerate(arms.get(arm, [])):
                for idx, (list_name, text) in enumerate(items):
                    present = False
                    for other_items in arms.get(other, []):
                        olds = [{"id": str(j), "text": t, "human_touched": False}
                                for j, (ln, t) in enumerate(other_items) if ln == list_name]
                        if not olds:
                            continue
                        pairs, _ = carry_forward.match(
                            olds, [{"id": "n", "text": text, "human_touched": False}])
                        if pairs:
                            present = True
                            break
                    if not present:
                        only.add((sess, shape, step, arm, rep_idx, idx))
    return only


def transcript_support(run_dir, gold, step_items):
    only = arm_only_keys(step_items)
    reps_sorted = {}
    tallies = {"with_block": [0, 0], "baseline": [0, 0]}
    overall = {"with_block": [0, 0], "baseline": [0, 0]}
    for a in label_mod.iter_assignments(run_dir):
        g = gold.get(a["assignment_id"])
        if g is None or g.get("supported") is None:
            continue
        sess, shape, arm, rep, step, idx = a["assignment_id"].split("|")
        arm_dir = Path(run_dir) / "runs" / shape / arm
        order = reps_sorted.setdefault((shape, arm, sess), sorted(
            p.name for p in arm_dir.iterdir() if (p / sess).exists()
            and not any(s.get("error") for s in load_steps(p / sess))))
        unsupported = 0 if g["supported"] else 1
        overall[arm][0] += unsupported
        overall[arm][1] += 1
        if rep not in order:
            continue
        if (sess, shape, int(step), arm, order.index(rep), int(idx)) in only:
            tallies[arm][0] += unsupported
            tallies[arm][1] += 1
    return tallies, overall


def text_only_recall(run_dir, gold):
    """Same gold-positive with-block pairs, re-matched by carry_forward.match on text alone."""
    hit = total = 0
    by_step = {}
    for a in label_mod.iter_assignments(run_dir):
        if a["arm"] != "with_block":
            continue
        g = gold.get(a["assignment_id"])
        if g is None:
            continue
        cps = label_mod._counterparts(g)
        if not cps:
            continue
        by_step.setdefault(a["assignment_id"].rsplit("|", 1)[0], []).append((a, cps))
    for key, rows in by_step.items():
        a0 = rows[0][0]
        prior = [{"id": p["alias"], "text": p["text"], "human_touched": False}
                 for a, _ in rows for p in a["prior_list"]]
        prior = list({p["id"]: p for p in prior}.values())
        news = [{"id": a["assignment_id"], "text": a["new_item_text"], "human_touched": False}
                for a, _ in rows]
        pairs, _ = carry_forward.match(prior, news)
        matched = {(o, n) for o, n, _h in pairs}
        for a, cps in rows:
            total += 1
            if any((al, a["assignment_id"]) in matched for al in cps):
                hit += 1
    return (hit / total) if total else None


def main():
    run_dir, agent_done, out = sys.argv[1:4]
    gold = {r["assignment_id"]: r for r in label_mod._read_jsonl(agent_done)}
    agreement_pairs, owner_n, owner_stale = [], 0, 0
    if len(sys.argv) > 5:
        decisions = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
        private = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))
        # Ruling C11: an owner decision only counts if the owner judged the same prior list
        # the model was shown. review_items.json (next to review_private.json) records what
        # the review page displayed; a decision made on a list the labeller bug got wrong
        # (label.py paired against steps[i-1] instead of the last non-empty step) is dropped.
        shown = {i["id"]: [(p["alias"], p["text"]) for p in i["prior"]]
                 for i in json.loads((Path(sys.argv[5]).parent / "review_items.json")
                                     .read_text(encoding="utf-8"))}
        current = {a["assignment_id"]: [(p["alias"], p["text"]) for p in a["prior_list"]]
                   for a in label_mod.iter_assignments(run_dir)}
        for rid, dec in decisions.items():
            p = private.get(rid)
            if not p or p["assignment_id"] not in gold:
                continue
            if rid in shown and shown[rid] != current.get(p["assignment_id"]):
                owner_stale += 1
                continue
            agent_row = gold[p["assignment_id"]]
            owner_row = {**agent_row, "counterpart": dec["counterpart"],
                         "supported": dec["supported"], "labeller": "owner"}
            if p.get("reason") == "sample":
                agreement_pairs.append((label_mod._counterparts(agent_row),
                                        label_mod._counterparts(owner_row)))
            gold[p["assignment_id"]] = owner_row
            owner_n += 1
    merged = Path(run_dir).parent / (Path(agent_done).parent.name + "-gold.jsonl")
    with open(merged, "w", encoding="utf-8") as fh:
        for r in gold.values():
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    finals, latencies, step_items = gather(run_dir)
    metric, tolerance = admission_drift(finals)
    tallies, overall = transcript_support(run_dir, gold, step_items)
    p90w = score._percentile(latencies["with_block"], 90)
    p90b = score._percentile(latencies["baseline"], 90)
    added = [] if p90w is None or p90b is None else [p90w - p90b] * len(latencies["with_block"])
    tor = text_only_recall(run_dir, gold)

    summary = score.build_summary(
        run_dir, merged, out, latencies_ms=added,
        block_only_unsupported=tallies["with_block"][0], block_only_total=tallies["with_block"][1],
        baseline_only_unsupported=tallies["baseline"][0], baseline_only_total=tallies["baseline"][1],
        session_admission_metric=metric, session_admission_tolerance=tolerance,
        agreement_pairs=agreement_pairs, text_only_recall_value=tor)
    # Ruling C10: label rate is a with-block statistic (baseline never sees the block, so its
    # rows have no claims and would halve the rate). And "exact_matched_item" is not a reject
    # that loses anything: the new item's text already matched a prior exactly, so the exact
    # pass carries it; it is reported as a deferral, not a false reject.
    rows = score.scored_assignments(run_dir, merged)
    summary["reported"]["label_rate"] = score.label_rate(
        [r for r in rows if r["arm"] == "with_block"])
    fr = summary["reported"]["false_rejects_per_guard"]
    summary["reported"]["deferred_to_exact_match"] = fr.pop("exact_matched_item", 0)
    summary["context"] = {
        "owner_labels_applied": owner_n,
        "owner_labels_dropped_stale_prior": owner_stale,
        "final_pass_p90_ms": {"with_block": p90w, "baseline": p90b},
        "final_pass_n": {k: len(v) for k, v in latencies.items()},
        "unsupported_overall": {k: {"unsupported": v[0], "total": v[1]} for k, v in overall.items()},
        "drift_cells": len(metric),
        "gold": "agent+owner" if owner_n else "agent only (preliminary)",
    }
    Path(out).write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
