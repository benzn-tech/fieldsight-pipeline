"""Noise floor first (spec §8.4; memory: one before/after diff proves nothing).

`compare(baseline_reports, variant_reports)` takes report.json files. The baseline group is
two or more runs of the SAME gold set under the SAME config (config_label): their per-metric
spread (max - min) is the floor. A variant's delta against the baseline mean is called
better/worse only when |delta| exceeds the floor; otherwise `within_noise`. Fewer than two
baseline runs, mixed gold sets, or mixed baseline configs are refused outright.

A floor from two runs is itself noisy: it is a minimum bar, not a confidence interval.
"""
from __future__ import annotations

HEADLINE = {  # metric -> True when higher is better
    "fabrication_shown_rate": False, "fabrication_raw_rate": False,
    "wrong_target_rate": False, "recall": True, "uncertain_honesty": True,
    "latency_p50_s": False, "latency_p90_s": False, "cost_mean": False,
}
_EPS = 1e-12


class CompareRefused(Exception):
    pass


def _values(reports, metric):
    return [(r.get("headline") or {}).get(metric) for r in reports]


def compare(baseline, variants) -> dict:
    if len(baseline) < 2:
        raise CompareRefused("the noise floor needs at least two baseline runs of the same "
                             "gold set and config (spec §8.4)")
    everything = list(baseline) + list(variants)
    for field, what in (("run_id", "a run id"), ("pack_ids_sha256", "a pack-id hash"),
                        ("submitted_inputs_sha256", "a submitted-inputs hash")):
        if any(not r.get(field) for r in everything):
            raise CompareRefused(f"every report needs {what} ({field}); re-score the runs "
                                 "with the current harness")
    run_ids = [r["run_id"] for r in everything]
    if len(set(run_ids)) != len(run_ids):
        raise CompareRefused(f"the same run is passed more than once: {sorted(run_ids)}; a "
                             "floor from one run measured twice is 0 by construction")
    packs = [r["pack_ids_sha256"] for r in everything]
    if len(set(packs)) != len(packs):
        raise CompareRefused("two reports scored the same packs (equal pack_ids_sha256): "
                             "a copied report is not a second run")
    shas = {r.get("gold_sha256") for r in list(baseline) + list(variants)}
    if len(shas) != 1 or None in shas:
        raise CompareRefused(f"runs are scored against different gold sets: {sorted(map(str, shas))}")
    inputs = {r.get("submitted_inputs_sha256") for r in list(baseline) + list(variants)}
    if len(inputs) != 1:
        raise CompareRefused("runs were submitted from different instructions (inputs "
                             f"sha256 {sorted(map(str, inputs))}); the floor needs identical "
                             "inputs")
    configs = {r.get("config_label") for r in baseline}
    if len(configs) != 1:
        raise CompareRefused(f"baseline runs have different config labels {sorted(map(str, configs))}; "
                             "the floor is measured on identical configs")
    floor, mean = {}, {}
    for metric in HEADLINE:
        vals = _values(baseline, metric)
        if any(v is None for v in vals):
            floor[metric] = mean[metric] = None
            continue
        floor[metric] = max(vals) - min(vals)
        mean[metric] = sum(vals) / len(vals)
    out_variants = []
    for v in variants:
        rows = {}
        for metric, higher in HEADLINE.items():
            val = (v.get("headline") or {}).get(metric)
            if val is None or mean[metric] is None:
                rows[metric] = {"value": val, "baseline_mean": mean[metric],
                                "floor": floor[metric], "delta": None, "verdict": "n/a"}
                continue
            delta = val - mean[metric]
            if abs(delta) <= floor[metric] + _EPS:
                verdict = "within_noise"
            else:
                verdict = "better" if (delta > 0) == higher else "worse"
            rows[metric] = {"value": val, "baseline_mean": mean[metric],
                            "floor": floor[metric], "delta": delta, "verdict": verdict}
        out_variants.append({"run_id": v.get("run_id"), "config_label": v.get("config_label"),
                             "metrics": rows})
    return {"gold_sha256": shas.pop(), "baseline_config": configs.pop(),
            "baseline_runs": [r.get("run_id") for r in baseline],
            "floor": floor, "baseline_mean": mean, "variants": out_variants}


def _fmt(v):
    return "n/a" if v is None else f"{v:.4g}"


def render_markdown(cmp) -> str:
    lines = ["# Action pack eval: compare", "",
             f"Baseline `{cmp['baseline_config']}` runs {cmp['baseline_runs']} · gold sha256 "
             f"`{cmp['gold_sha256']}`", "",
             "Floor = max - min across the baseline runs. A delta inside the floor is "
             "`within_noise`, never better or worse.", "",
             "| Metric | Baseline mean | Floor |", "|---|---|---|"]
    lines += [f"| {k} | {_fmt(cmp['baseline_mean'][k])} | {_fmt(cmp['floor'][k])} |"
              for k in HEADLINE]
    for v in cmp["variants"]:
        lines += ["", f"## Variant {v['run_id']} (`{v['config_label']}`)", "",
                  "| Metric | Value | Delta | Verdict |", "|---|---|---|---|"]
        lines += [f"| {k} | {_fmt(r['value'])} | {_fmt(r['delta'])} | {r['verdict']} |"
                  for k, r in v["metrics"].items()]
    return "\n".join(lines) + "\n"
