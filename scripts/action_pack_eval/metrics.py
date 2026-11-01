"""The §8 metrics, each counted in code from files (spec §8.5: no model-graded number).

Each `score_*` takes the gold entries, a recorded run (runs.py) and the human label rows
(annotations.py) and returns a small dict with a `verdict` of pass | fail | insufficient
(or report_only for the metrics spec §8.3 sets no bar for). Matching rules are in SCHEMA.md
and restated in each docstring.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import Counter

from scripts.action_pack_eval import annotations as ann_mod
from scripts.action_pack_eval import gold as gold_mod
from scripts.action_pack_eval import paths
from scripts.action_pack_eval import runs as runs_mod

PASS, FAIL, INSUFFICIENT, REPORT_ONLY = "pass", "fail", "insufficient", "report_only"
_EPS = 1e-12

# Spec §8.3 (the builder's call; the owner may tighten). latency_min_n: below this many
# finished packs a p90 is not a measurement (memory: n <= 8 cannot see a tail); 20 = the gold
# set's minimum size.
DEFAULT_THRESHOLDS = {"fabrication_shown_max": 0.05, "wrong_target_max": 0,
                      "recall_min": 0.60, "latency_p90_max_s": 300, "latency_min_n": 20}
FINISHED_STATES = ("ready", "failed")


def load_thresholds(path=None) -> dict:
    out = dict(DEFAULT_THRESHOLDS)
    if path is None:
        return out
    data = paths.read_json(path)
    unknown = sorted(set(data) - set(DEFAULT_THRESHOLDS) - {"note"})
    if unknown:
        raise SystemExit(f"{path}: unknown threshold keys {unknown}; known: "
                         f"{sorted(DEFAULT_THRESHOLDS)}")
    for k in DEFAULT_THRESHOLDS:
        if k in data:
            v = data[k]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise SystemExit(f"{path}: {k} must be a non-negative number")
            out[k] = v
    return out


def _rate(num, den):
    return num / den if den else None


# ---------------------------------------------------------------- fabrication

def _fab_counts(elements, idx):
    labelled = no = unclear = 0
    for pack, el in elements:
        lab = idx.get((str(pack["id"]), el["element"]))
        if lab is None:
            continue
        labelled += 1
        no += lab.get("supported_by_refs") == "no"
        unclear += lab.get("supported_by_refs") == "unclear"
    return {"elements": len(elements), "labelled": labelled,
            "unlabelled": len(elements) - labelled, "no": no, "unclear": unclear,
            "rate": _rate(no, labelled), "rate_unclear_as_no": _rate(no + unclear, labelled)}


def verifier_drops(run_trace) -> int:
    """Elements the verifier removed before storage (never labellable): conclusion lines it
    labelled unsupported plus every code-check drop (`drop_*`)."""
    v = (run_trace or {}).get("verify") or {}
    if not isinstance(v, dict):
        return 0
    return sum(n for k, n in v.items() if isinstance(n, int) and not isinstance(n, bool)
               and (k == "conclusion_unsupported" or k.startswith("drop_")))


def score_fabrication(run, labels, *, max_rate=DEFAULT_THRESHOLDS["fabrication_shown_max"]):
    """Share of elements a person judged NOT supported by their cited refs ("no").

    raw   -- every stored element: the writer's output as far as it is stored, including
             actions the verifier labelled unsupported (spec §14 S16).
    shown -- what a reader sees: raw minus unsupported actions (the post-verifier rate; the
             one the §8.3 gate reads).
    Conclusion lines the verifier rejected, and code-check drops, are not stored, so they
    are counted from run_trace.verify (`dropped_by_verifier`), never labelled;
    `raw_upper_bound_rate` treats every one of them as a fabrication.
    `unclear` stays in the denominator only; `rate_unclear_as_no` is the strict reading.
    Verdict: insufficient while any shown element is unlabelled or nothing is shown."""
    idx = ann_mod.label_index(labels)
    elements = [(pack, el) for _, pack, el in runs_mod.iter_elements(run)]
    raw = _fab_counts(elements, idx)
    shown = _fab_counts([(p, e) for p, e in elements if e["shown"]], idx)
    dropped = sum(verifier_drops(r["pack"].get("run_trace"))
                  for r in run.get("packs") or [] if r.get("pack"))
    write_dropped = 0
    for r in run.get("packs") or []:
        wd = ((r.get("pack") or {}).get("run_trace") or {}).get("write_dropped")
        if isinstance(wd, dict):
            write_dropped += sum(v for v in wd.values() if isinstance(v, int))
    if shown["labelled"] == 0 or shown["unlabelled"]:
        verdict = INSUFFICIENT
    else:
        verdict = PASS if shown["rate"] <= max_rate + _EPS else FAIL
    return {"raw": raw, "shown": shown, "dropped_by_verifier": dropped,
            "dropped_by_writer_schema": write_dropped,
            "raw_upper_bound_rate": _rate(raw["no"] + dropped, raw["labelled"] + dropped),
            "max_rate": max_rate, "verdict": verdict}


# ---------------------------------------------------------------- targets

def target_matches(expected, action) -> bool:
    """A close/update expectation names its item by stable id (preferred) or task number."""
    if expected.get("target_stable_id"):
        return str(action.get("target_stable_id") or "") == str(expected["target_stable_id"])
    if expected.get("target_task_no") is not None:
        return action.get("target_task_no") == expected["target_task_no"]
    return False


def touches_forbidden(entry, action) -> bool:
    for x in entry.get("must_not_touch") or []:
        if isinstance(x, str) and str(action.get("target_stable_id") or "") == x:
            return True
        if isinstance(x, int) and action.get("target_task_no") == x:
            return True
    return False


def _expected(entry, eid):
    return next((x for x in entry.get("expected_actions") or [] if x["id"] == eid), None)


def score_wrong_target(gold, run, labels, *, max_wrong=DEFAULT_THRESHOLDS["wrong_target_max"]):
    """Close/update actions on the wrong item. Wrong when the target is on the gold entry's
    must_not_touch list, or the annotator matched the action to a close/update expectation
    (`matches_expected`) whose target differs. Rate = wrong / targeted actions; the gate
    counts SHOWN wrong ones (spec §8.3: 0); raw adds the stored unsupported ones.
    Insufficient when no close/update was shown."""
    gold_by = gold_mod.by_id(gold)
    idx = ann_mod.label_index(labels)
    counts = {"shown": [0, 0], "raw": [0, 0]}
    wrong = []
    for gid, pack, el in runs_mod.iter_elements(run):
        if el["kind"] != "action" or el["type"] not in gold_mod.TARGETED:
            continue
        entry = gold_by.get(gid) or {}
        reason = None
        if touches_forbidden(entry, el):
            reason = "must_not_touch"
        else:
            lab = idx.get((str(pack["id"]), el["element"])) or {}
            exp = _expected(entry, lab.get("matches_expected"))
            if exp is not None and exp["type"] in gold_mod.TARGETED \
                    and not target_matches(exp, el):
                reason = "mismatched_expectation"
        for scope in (("raw", "shown") if el["shown"] else ("raw",)):
            counts[scope][0] += 1
            counts[scope][1] += reason is not None
        if reason:
            wrong.append({"pack_id": str(pack["id"]), "element": el["element"],
                          "reason": reason, "shown": el["shown"]})
    out = {s: {"targeted": t, "wrong": w, "rate": _rate(w, t)} for s, (t, w) in counts.items()}
    shown_t, shown_w = counts["shown"]
    verdict = INSUFFICIENT if shown_t == 0 else (PASS if shown_w <= max_wrong else FAIL)
    return {**out, "wrong": wrong, "max_wrong": max_wrong, "verdict": verdict}


# ---------------------------------------------------------------- recall

def _action_matches(expected, el, label) -> bool:
    if el["kind"] != "action" or el["type"] != expected["type"]:
        return False
    if expected["type"] in gold_mod.TARGETED:
        return target_matches(expected, el)
    return (label or {}).get("matches_expected") == expected["id"]


def score_recall(gold, run, labels, *, min_rate=DEFAULT_THRESHOLDS["recall_min"]):
    """Expected actions the run proposed. close/update match by type + target (automatic);
    create/draft/note match only by the annotator's `matches_expected` on an action of the
    same type. An instruction with no pack (or a failed one) misses all its expectations.
    The gate reads SHOWN matches; raw also counts stored unsupported actions."""
    idx = ann_mod.label_index(labels)
    packs_for = {}
    for gid, pack, el in runs_mod.iter_elements(run):
        packs_for.setdefault(gid, []).append((pack, el))
    total = shown = raw = 0
    missed = []
    for entry in gold:
        for exp in entry.get("expected_actions") or []:
            total += 1
            hits = [el["shown"] for pack, el in packs_for.get(entry["id"], [])
                    if _action_matches(exp, el, idx.get((str(pack["id"]), el["element"])))]
            raw += bool(hits)
            shown += any(hits)
            if not any(hits):
                missed.append({"gold_id": entry["id"], "expected_id": exp["id"]})
    verdict = INSUFFICIENT if total == 0 else (
        PASS if shown / total >= min_rate - _EPS else FAIL)
    return {"shown": {"expected": total, "matched": shown, "rate": _rate(shown, total)},
            "raw": {"expected": total, "matched": raw, "rate": _rate(raw, total)},
            "missed": missed, "min_rate": min_rate, "verdict": verdict}


# ---------------------------------------------------------------- "uncertain" honesty

def _cites_document(pack, refs) -> bool:
    by_ref = {s.get("ref"): s for s in pack.get("sources") or []}
    for r in refs:
        s = by_ref.get(r)
        if s is not None and s.get("provenance") == "documents":
            return True
        if s is None and isinstance(r, str) and r.startswith("D"):
            return True
    return False


def score_uncertain_honesty(gold, run, labels):
    """Of the SHOWN conclusion lines a person marked as a standards claim and that cite no
    project-library document (no source with provenance `documents`), the share the pack
    marked `uncertain`. Actions carry no confidence, so standards claims on actions are
    counted (`standards_actions`) but not scored. `expected_uncertain`: the gold's expected
    uncertain claims a person tied (`expected_uncertain_id`) to a shown line marked
    uncertain. Spec §8.3 sets no bar: report_only."""
    idx = ann_mod.label_index(labels)
    claims = honest = actions = 0
    covered = set()
    for gid, pack, el in runs_mod.iter_elements(run):
        lab = idx.get((str(pack["id"]), el["element"])) or {}
        if not el["shown"] or not lab.get("is_standards_claim"):
            continue
        if el["kind"] == "action":
            actions += 1
            continue
        if el.get("confidence") == "uncertain" and lab.get("expected_uncertain_id"):
            covered.add((gid, lab["expected_uncertain_id"]))
        if _cites_document(pack, el["refs"]):
            continue
        claims += 1
        honest += el.get("confidence") == "uncertain"
    want = {(e["id"], u["id"]) for e in gold for u in e.get("expected_uncertain") or []}
    return {"claims_without_document": claims, "marked_uncertain": honest,
            "rate": _rate(honest, claims), "standards_actions": actions,
            "expected_uncertain": {"expected": len(want), "covered": len(want & covered)},
            "verdict": REPORT_ONLY}


# ---------------------------------------------------------------- latency

def _ts(v):
    if not isinstance(v, str):
        return None
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def nearest_rank(values, p):
    """The nearest-rank percentile (no interpolation): sorted[ceil(p*n) - 1]."""
    if not values:
        return None
    s = sorted(values)
    return float(s[max(0, math.ceil(p * len(s)) - 1)])


def _dist(values):
    return {"n": len(values), "p50_s": nearest_rank(values, 0.5),
            "p90_s": nearest_rank(values, 0.9)}


def score_latency(run, *, max_p90_s=DEFAULT_THRESHOLDS["latency_p90_max_s"],
                  min_n=DEFAULT_THRESHOLDS["latency_min_n"]):
    """Seconds per finished (ready/failed) pack. end_to_end = updated_at - created_at (the
    reader's wait incl. queue; read before anyone applies or archives); worker =
    run_trace.ms.total (the agent's own time). The gate takes end_to_end and falls back to
    worker per pack; `sources` says which was used. An instruction whose pack never
    finished inside submit's max wait (error "timeout") enters the gate as its client-side
    elapsed time, a censored value (the real latency is at least that), so the slowest
    instructions cannot drop out of the tail. Insufficient below min_n values."""
    e2e, worker, gate = [], [], []
    sources = Counter({"timestamps": 0, "run_trace": 0, "timeout": 0, "none": 0})
    for row in run.get("packs") or []:
        waited = row.get("client_elapsed_s")
        if row.get("error") == "timeout":
            if isinstance(waited, (int, float)) and not isinstance(waited, bool):
                gate.append(float(waited))
                sources["timeout"] += 1
            else:
                sources["none"] += 1
            continue
        pack = row.get("pack") or {}
        if pack.get("state") not in FINISHED_STATES:
            continue
        a, b = _ts(pack.get("created_at")), _ts(pack.get("updated_at"))
        e = (b - a).total_seconds() if a and b and b >= a else None
        ms = ((pack.get("run_trace") or {}).get("ms") or {}).get("total")
        w = ms / 1000 if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None
        if e is not None:
            e2e.append(e)
        if w is not None:
            worker.append(w)
        if e is not None:
            gate.append(e)
            sources["timestamps"] += 1
        elif w is not None:
            gate.append(w)
            sources["run_trace"] += 1
        else:
            sources["none"] += 1
    g = _dist(gate)
    verdict = INSUFFICIENT if g["n"] < max(1, min_n) else (
        PASS if g["p90_s"] <= max_p90_s else FAIL)
    return {"end_to_end": _dist(e2e), "worker": _dist(worker), "sources": dict(sources),
            "gate_n": g["n"], "gate_p50_s": g["p50_s"], "gate_p90_s": g["p90_s"],
            "max_p90_s": max_p90_s, "min_n": min_n, "verdict": verdict}


# ---------------------------------------------------------------- cost

def load_prices(path):
    """{"currency": str?, "per_1k_tokens": {model id: rate}, "default": rate?}. No rate is
    ever hardcoded here; a model missing from the file (and no default) is unpriced."""
    if path is None:
        return None
    data = paths.read_json(path)
    rates = data.get("per_1k_tokens")
    ok = isinstance(rates, dict) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
        for v in rates.values())
    d = data.get("default")
    if not ok or (d is not None and (not isinstance(d, (int, float)) or d < 0)):
        raise SystemExit(f"{path}: per_1k_tokens must map model ids to non-negative numbers")
    return data


def _rate_for(models, prices):
    if not prices:
        return None
    table = prices.get("per_1k_tokens") or {}
    for k in ("writer_served", "writer"):
        m = (models or {}).get(k)
        if m in table:
            return table[m]
    return prices.get("default")


def score_cost(run, prices):
    """Tokens (run_trace.tokens: ONE total over planner, writer and verifier, with the
    agent's chars/4 estimate where a vendor gave no usage) x a rate per 1k tokens from the
    price file, looked up by models.writer_served, then models.writer, then the file's
    `default`. One blended rate per pack is an approximation: the trace has no per-model
    split (README)."""
    tokens_total = priced_n = unpriced = without = 0
    cost_total = 0.0
    for row in run.get("packs") or []:
        pack = row.get("pack")
        if not pack:
            continue
        t = (pack.get("run_trace") or {}).get("tokens")
        if not isinstance(t, int) or isinstance(t, bool):
            without += 1
            continue
        tokens_total += t
        rate = _rate_for(pack.get("models"), prices)
        if rate is None:
            unpriced += 1
            continue
        priced_n += 1
        cost_total += t / 1000 * rate
    return {"tokens_total": tokens_total,
            "tokens_mean": _rate(tokens_total, priced_n + unpriced),
            "cost_total": cost_total if priced_n else None,
            "cost_mean": _rate(cost_total, priced_n) if priced_n else None,
            "currency": (prices or {}).get("currency"), "packs_priced": priced_n,
            "packs_unpriced": unpriced, "packs_without_tokens": without,
            "verdict": REPORT_ONLY}


# ---------------------------------------------------------------- agreement (the ceiling)

def percent_agreement(pairs):
    pairs = list(pairs)
    return _rate(sum(a == b for a, b in pairs), len(pairs))


def cohen_kappa(pairs):
    """Cohen's kappa for two raters; None when undefined (no pairs, or chance agreement 1)."""
    pairs = list(pairs)
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / (n * n)
    if abs(1 - pe) < _EPS:
        return None
    return (po - pe) / (1 - pe)


def agreement(first, second):
    """First vs second annotator on the elements both labelled (the blind subset)."""
    a, b = ann_mod.label_index(first), ann_mod.label_index(second)
    common = [k for k in a if k in b]
    out = {"n": len(common),
           "first_annotators": sorted({str(a[k].get("annotator")) for k in common}),
           "second_annotators": sorted({str(b[k].get("annotator")) for k in common})}
    for field in ("supported_by_refs", "is_standards_claim"):
        pairs = [(a[k].get(field), b[k].get(field)) for k in common]
        out[field] = {"percent": percent_agreement(pairs), "kappa": cohen_kappa(pairs)}
    return out
