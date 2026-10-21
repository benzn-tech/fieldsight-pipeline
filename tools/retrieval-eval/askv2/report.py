"""Per-run summary, noise floor, extrapolation, caps (plan T10; review S8/S11).

Counts, never percentages alone (review S11): with 39 answerable questions one question
is 2.6 points. A cross-family claim takes the per-question majority over repeats and
must exceed the same-config flip count, which the result header prints beside it.
"""
import statistics
from collections import Counter

from askv2.funnel import STAGES


class CapExceeded(Exception):
    pass


def _answerable_scored(turns):
    return [t for t in turns if t["expect"] == "answerable" and t["funnel"]["status"] == "scored"]


def _tokens(turns):
    out = {"prompt": 0, "completion": 0, "reasoning": 0}
    for t in turns:
        for j in t.get("judge") or []:
            for k in out:
                out[k] += (j.get("usage") or {}).get(k) or 0
    return out


def summarise(turns):
    """One repeat of one config."""
    scored = _answerable_scored(turns)
    controls = [t for t in turns if t["expect"] != "answerable"
                and t["funnel"]["status"] != "invalid"]
    ms = [t["sql"]["ms_total"] for t in turns]
    return {
        "answerable_scored": len(scored),
        "stages": {s: "%d/%d" % (sum(bool(t["funnel"]["stages"][s]) for t in scored), len(scored))
                   for s in STAGES},
        "lost_at": dict(Counter(t["funnel"]["lost_at"] for t in scored
                                if t["funnel"]["lost_at"])),
        "status": dict(Counter(t["funnel"]["status"] for t in turns)),
        "fallback_reasons": dict(Counter(t["funnel"]["fallback"] for t in turns
                                         if t["funnel"]["fallback"])),
        "controls": {"n": len(controls),
                     "answered_with_citations": sum(
                         bool((t["funnel"]["control"] or {}).get("answered_with_citations"))
                         for t in controls)},
        "judge_calls": sum(len(t.get("judge") or []) for t in turns),
        "tokens": _tokens(turns),
        "sql": {"statements": sum(t["sql"]["statements"] for t in turns),
                "turn_ms_p50": statistics.median(ms) if ms else None,
                "stmt_ms_max": max((t["sql"]["ms_max"] or 0 for t in turns), default=None),
                "largest_response": max((t["sql"]["largest_response"] for t in turns),
                                        default=0)},
    }


def flips(runs):
    """Questions scored in every repeat whose (seen, cited) differs between repeats:
    the same-config noise a family difference must exceed (spec §5)."""
    if len(runs) < 2:
        return []
    by_q = {}
    for turns in runs:
        for t in _answerable_scored(turns):
            st = t["funnel"]["stages"]
            by_q.setdefault(t["id"], []).append((st["seen"], st["cited"]))
    return sorted(q for q, v in by_q.items() if len(v) == len(runs) and len(set(v)) > 1)


def majority(runs, stage):
    """{question: True/False by strict majority over its scored repeats, None on a tie}."""
    votes = {}
    for turns in runs:
        for t in _answerable_scored(turns):
            votes.setdefault(t["id"], []).append(bool(t["funnel"]["stages"][stage]))
    out = {}
    for q, v in votes.items():
        yes = sum(v)
        out[q] = True if yes * 2 > len(v) else False if yes * 2 < len(v) else None
    return out


class Caps:
    """Hard caps (review S8): the run aborts, results so far are still written."""

    def __init__(self, max_judge_calls, max_tokens):
        self.max_judge_calls, self.max_tokens = max_judge_calls, max_tokens

    def check(self, turns):
        calls = sum(len(t.get("judge") or []) for t in turns)
        tok = _tokens(turns)
        total = tok["prompt"] + tok["completion"]
        if self.max_judge_calls is not None and calls > self.max_judge_calls:
            raise CapExceeded("judge calls %d > %d" % (calls, self.max_judge_calls))
        if self.max_tokens is not None and total > self.max_tokens:
            raise CapExceeded("tokens (prompt+completion) %d > %d" % (total, self.max_tokens))


def extrapolate(turns, n_turns):
    """Per-turn means of the pilot, times the full run's turn count."""
    n = len(turns) or 1
    tok = _tokens(turns)
    calls = sum(len(t.get("judge") or []) for t in turns)
    return {"turns": n_turns,
            "judge_calls": round(calls * n_turns / n),
            "prompt_tokens": round(tok["prompt"] * n_turns / n),
            "completion_tokens": round(tok["completion"] * n_turns / n),
            "reasoning_tokens": round(tok["reasoning"] * n_turns / n),
            "sql_statements": round(sum(t["sql"]["statements"] for t in turns) * n_turns / n)}


def diff(a, b):
    """Per-question majority changes between two result files. Refused across
    different judge model chains (spec §9 risk 3)."""
    ca, cb = a["fingerprint"].get("model_chain"), b["fingerprint"].get("model_chain")
    if ca != cb:
        raise SystemExit("model chains differ (%s vs %s): not comparable" % (ca, cb))
    out = {}
    for stage in ("seen", "cited"):
        ma = majority([r["turns"] for r in a["runs"]], stage)
        mb = majority([r["turns"] for r in b["runs"]], stage)
        out[stage] = {q: [ma.get(q), mb.get(q)] for q in sorted(set(ma) | set(mb))
                      if ma.get(q) != mb.get(q)}
    return out
