"""The scored report: one JSON (machine) and one Markdown (people) per scored run.

Counts, rates, pack ids and element keys only; never instruction, conclusion or excerpt text.
`headline` is the flat set of numbers `compare` reads.
"""
from __future__ import annotations

from collections import Counter

from scripts.action_pack_eval import metrics

GATES = ("fabrication", "wrong_target", "recall", "latency_p90")


def build_report(gold, run, labels, *, prices=None, thresholds=None, second=None,
                 gold_sha256=None, warnings=()):
    th = dict(metrics.DEFAULT_THRESHOLDS, **(thresholds or {}))
    fab = metrics.score_fabrication(run, labels, max_rate=th["fabrication_shown_max"])
    wt = metrics.score_wrong_target(gold, run, labels, max_wrong=th["wrong_target_max"])
    rec = metrics.score_recall(gold, run, labels, min_rate=th["recall_min"])
    hon = metrics.score_uncertain_honesty(gold, run, labels)
    lat = metrics.score_latency(run, max_p90_s=th["latency_p90_max_s"],
                                min_n=th["latency_min_n"])
    cost = metrics.score_cost(run, prices)
    m = {"fabrication": fab, "wrong_target": wt, "recall": rec, "uncertain_honesty": hon,
         "latency": lat, "cost": cost}
    if second is not None:
        m["agreement"] = metrics.agreement(labels, second)
    gates = {
        "fabrication": {"value": fab["shown"]["rate"], "threshold": th["fabrication_shown_max"],
                        "op": "<=", "verdict": fab["verdict"]},
        "wrong_target": {"value": wt["shown"]["wrong"], "threshold": th["wrong_target_max"],
                         "op": "<=", "verdict": wt["verdict"]},
        "recall": {"value": rec["shown"]["rate"], "threshold": th["recall_min"], "op": ">=",
                   "verdict": rec["verdict"]},
        "latency_p90": {"value": lat["gate_p90_s"], "threshold": th["latency_p90_max_s"],
                        "op": "<=", "verdict": lat["verdict"]},
    }
    verdicts = [g["verdict"] for g in gates.values()]
    overall = (metrics.FAIL if metrics.FAIL in verdicts else
               metrics.PASS if all(v == metrics.PASS for v in verdicts) else
               metrics.INSUFFICIENT)
    packs = [r.get("pack") for r in run.get("packs") or []]
    return {
        "run_id": run.get("run_id"), "config_label": run.get("config_label"),
        "gold_sha256": gold_sha256,
        "submitted_inputs_sha256": run.get("inputs_sha256"),
        "counts": {"instructions": len(gold), "packs": sum(1 for p in packs if p),
                   "no_pack": sum(1 for p in packs if not p),
                   "by_state": dict(Counter((p or {}).get("state") for p in packs if p))},
        "thresholds": th, "metrics": m, "gates": gates, "overall": overall,
        "headline": {
            "fabrication_shown_rate": fab["shown"]["rate"],
            "fabrication_raw_rate": fab["raw"]["rate"],
            "wrong_target_rate": wt["shown"]["rate"],
            "recall": rec["shown"]["rate"],
            "uncertain_honesty": hon["rate"],
            "latency_p50_s": lat["gate_p50_s"], "latency_p90_s": lat["gate_p90_s"],
            "cost_mean": cost["cost_mean"],
        },
        "warnings": list(warnings),
    }


def _pct(v):
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _num(v, unit=""):
    if v is None:
        return "n/a"
    return (f"{v:.0f}" if float(v).is_integer() else f"{v:.2f}") + unit


def render_markdown(rep) -> str:
    m, g = rep["metrics"], rep["gates"]
    fab, wt, rec, hon = m["fabrication"], m["wrong_target"], m["recall"], m["uncertain_honesty"]
    lat, cost = m["latency"], m["cost"]
    lines = [f"# Action pack eval: {rep.get('run_id')}", "",
             f"Config: `{rep.get('config_label')}` · gold sha256 `{rep.get('gold_sha256')}` · "
             f"instructions {rep['counts']['instructions']} · packs {rep['counts']['packs']} "
             f"(no pack: {rep['counts']['no_pack']}; states {rep['counts']['by_state']})", "",
             f"**Overall: {rep['overall']}**", "",
             "## Gates (spec §8.3)", "",
             "| Gate | Value | Threshold | Verdict |", "|---|---|---|---|",
             f"| fabrication | {_pct(g['fabrication']['value'])} | <= "
             f"{_pct(g['fabrication']['threshold'])} of shown | {g['fabrication']['verdict']} |",
             f"| wrong_target | {g['wrong_target']['value']} | <= "
             f"{g['wrong_target']['threshold']} | {g['wrong_target']['verdict']} |",
             f"| recall | {_pct(g['recall']['value'])} | >= {_pct(g['recall']['threshold'])} "
             f"| {g['recall']['verdict']} |",
             f"| latency_p90 | {_num(g['latency_p90']['value'], ' s')} | <= "
             f"{_num(g['latency_p90']['threshold'], ' s')} (n >= {lat['min_n']}) "
             f"| {g['latency_p90']['verdict']} |", "",
             "## Raw writer vs post-verifier (spec §14 S16)", "",
             "| Metric | Raw writer (all stored) | Post-verifier (shown) |", "|---|---|---|",
             f"| fabrication rate | {_pct(fab['raw']['rate'])} ({fab['raw']['no']}/"
             f"{fab['raw']['labelled']}) | {_pct(fab['shown']['rate'])} ({fab['shown']['no']}/"
             f"{fab['shown']['labelled']}) |",
             f"| fabrication, unclear counted as no | {_pct(fab['raw']['rate_unclear_as_no'])} "
             f"| {_pct(fab['shown']['rate_unclear_as_no'])} |",
             f"| unlabelled elements | {fab['raw']['unlabelled']} | {fab['shown']['unlabelled']} |",
             f"| wrong-target rate | {_pct(wt['raw']['rate'])} ({wt['raw']['wrong']}/"
             f"{wt['raw']['targeted']}) | {_pct(wt['shown']['rate'])} ({wt['shown']['wrong']}/"
             f"{wt['shown']['targeted']}) |",
             f"| recall | {_pct(rec['raw']['rate'])} ({rec['raw']['matched']}/"
             f"{rec['raw']['expected']}) | {_pct(rec['shown']['rate'])} "
             f"({rec['shown']['matched']}/{rec['shown']['expected']}) |", "",
             f"Removed before storage (counted from run_trace, not labellable): verifier "
             f"{fab['dropped_by_verifier']}, writer schema {fab['dropped_by_writer_schema']}. "
             f"Raw upper bound (all verifier drops counted as fabrications): "
             f"{_pct(fab['raw_upper_bound_rate'])}.", "",
             "## Other metrics", "",
             f"- \"Uncertain\" honesty (report only): {_pct(hon['rate'])} "
             f"({hon['marked_uncertain']}/{hon['claims_without_document']} standards claims "
             f"without a library document marked uncertain); expected uncertain claims "
             f"covered {hon['expected_uncertain']['covered']}/"
             f"{hon['expected_uncertain']['expected']}; standards claims on actions "
             f"{hon['standards_actions']}.",
             f"- Latency: gate p50 {_num(lat['gate_p50_s'], ' s')}, p90 "
             f"{_num(lat['gate_p90_s'], ' s')} (n {lat['gate_n']}; sources {lat['sources']}); "
             f"worker time p50 {_num(lat['worker']['p50_s'], ' s')}, p90 "
             f"{_num(lat['worker']['p90_s'], ' s')}.",
             f"- Cost: tokens {cost['tokens_total']}; cost total "
             f"{_num(cost['cost_total'])} {cost['currency'] or ''}, mean per pack "
             f"{_num(cost['cost_mean'])} (priced {cost['packs_priced']}, unpriced "
             f"{cost['packs_unpriced']}, no tokens {cost['packs_without_tokens']}; one "
             f"blended rate per pack).",
             ]
    if "agreement" in m:
        a = m["agreement"]
        lines.append(f"- Inter-annotator agreement (ceiling, n {a['n']}): supported_by_refs "
                     f"{_pct(a['supported_by_refs']['percent'])}, kappa "
                     f"{_num(a['supported_by_refs']['kappa'])}; is_standards_claim "
                     f"{_pct(a['is_standards_claim']['percent'])}, kappa "
                     f"{_num(a['is_standards_claim']['kappa'])}.")
    if wt["wrong"]:
        lines += ["", "## Wrong targets", "", "| Pack | Element | Reason | Shown |",
                  "|---|---|---|---|"]
        lines += [f"| {w['pack_id']} | {w['element']} | {w['reason']} | {w['shown']} |"
                  for w in wt["wrong"]]
    if rec["missed"]:
        lines += ["", "## Missed expected actions (shown)", "",
                  ", ".join(f"{x['gold_id']}/{x['expected_id']}" for x in rec["missed"])]
    if rep.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in rep["warnings"]]
    return "\n".join(lines) + "\n"
