"""How often does the corroboration step say the web agrees when it does not?

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §5.2

Read-only. Calls the Anthropic API and nothing else -- no database, no S3, no
customer content. Every case below is written for this file: a question and an
answer I made up, about entities whose existence is public.

## Why this is a labelled set and not a sample of real traffic

The spec left the precision question to "the first week of TEST data". That was
wrong, and this file is the correction: **real traffic has no ground truth.** An
answer that comes back `corroborated` in production looks identical whether the
web agreed or the model merely thought it did, and nobody is going to
hand-adjudicate a week of them. Precision is only measurable against cases where
someone already knows the answer, which means writing them.

## The error that matters is not the error rate

Two of the sixteen mistakes this can make are ordinary and one is not:

- a true claim called `not_found` costs the reader a card
- a checkable claim called `no_checkable_claim` costs the reader a card
- **a false claim called `corroborated` hands the reader a wrong statement with
  a source stapled to it**, which is worse than showing nothing at all and is
  precisely the trust inflation §1 exists to prevent

So the summary at the bottom counts that one separately, and a run with a clean
overall score and one FALSE->corroborated is a failing run.

## Variance

Each case runs three times, because this repository has already been burned once
by ranking two prompts on a single sample and getting the order backwards. A
case that disagrees with itself across runs is reported as UNSTABLE, which is a
finding about the model rather than about the case.

Usage:
    ANTHROPIC_API_KEY=... python scripts/measure_corroboration_precision.py [--runs N]
"""
import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

os.environ.setdefault("ENABLE_EXTERNAL_CORROBORATION", "true")

import corroboration  # noqa: E402

# (label, question, answer, expected_state, note)
#
# `expected` is what a careful human would assign knowing the world. Where a
# second state would also be defensible it is named in `also_ok` -- pretending
# every case has exactly one right answer would manufacture failures and hide
# the one that matters.
CASES = [
    # --- true, checkable: the web should agree -------------------------------
    ("true/nzs3604", "what standard governs the timber framing",
     "The timber framing follows NZS 3604, the New Zealand standard for "
     "light timber frame buildings.",
     "corroborated", {"not_found"}),
    ("true/worksafe", "who regulates site safety",
     "WorkSafe New Zealand is the country's workplace health and safety regulator.",
     "corroborated", {"not_found"}),
    ("true/fletcher", "who supplied the plasterboard",
     "Fletcher Building is a large New Zealand construction and building "
     "materials company.",
     "corroborated", {"not_found"}),
    ("true/as1170", "what governs the wind loading",
     "Wind loading is covered by AS/NZS 1170, the structural design actions "
     "standard used in Australia and New Zealand.",
     "corroborated", {"not_found"}),

    # --- false, checkable: the web should disagree ---------------------------
    ("false/nzs3604-country", "what standard governs the timber framing",
     "The timber framing follows NZS 3604, which is the Australian standard "
     "for concrete structures.",
     "conflicts", set()),
    ("false/worksafe-role", "who regulates site safety",
     "WorkSafe New Zealand is the government agency that issues building "
     "consents and approves resource applications.",
     "conflicts", set()),
    ("false/fletcher-sector", "who supplied the plasterboard",
     "Fletcher Building is a Japanese semiconductor manufacturer.",
     "conflicts", set()),
    ("false/iso9001", "what quality standard do we work to",
     "We work to ISO 9001, which is the international standard for food "
     "safety management in commercial kitchens.",
     "conflicts", set()),

    # --- the entity is real, the claim is about our own job ------------------
    # This is the category §5.2 calls the most likely way the feature becomes
    # dishonest: confirming the company exists and rendering it as if the
    # ANSWER were verified.
    ("ours/naylor-schedule", "what did the contractor say about the programme",
     "Naylor Love Construction said the slab pour would move to Thursday.",
     "no_checkable_claim", set()),
    ("ours/fletcher-delivery", "when is the plasterboard arriving",
     "Fletcher Building are delivering the plasterboard to the site on Monday.",
     "no_checkable_claim", set()),
    ("ours/worksafe-visit", "is there an inspection coming",
     "WorkSafe New Zealand are visiting the site next week.",
     "no_checkable_claim", set()),
    ("ours/nzs3604-copy", "where is the standard",
     "The site office has a printed copy of NZS 3604 on the shelf.",
     "no_checkable_claim", set()),

    # --- checkable in principle, unlikely to be findable ---------------------
    ("obscure/private-firm", "who did the scaffolding",
     "Kea Rise Scaffolding Limited holds a current scaffolding certificate of "
     "competence for suspended work platforms.",
     "not_found", {"no_checkable_claim", "conflicts"}),
    ("obscure/standard-clause", "what does the standard say about bracing",
     "NZS 3604 requires exactly 47 bracing units per storey in every dwelling.",
     "conflicts", {"not_found"}),

    # --- nothing external at all: the gate should empty this out -------------
    ("none/pure-internal", "what did the team decide",
     "The team agreed to move the handrail order forward and chase it Monday.",
     "EMPTY", set()),
    ("none/commercial", "what was agreed about the variation",
     "We agreed the variation would be priced at forty thousand dollars "
     "before the claim goes in, and John Smith will sign it off.",
     "EMPTY", set()),
]


def state_of(out, label):
    """The single state this run produced, or a marker for the shapes that are
    not one state: nothing came back, or several entities did."""
    if out["timed_out"]:
        return "TIMEOUT"
    cards = out["corroborations"]
    if not cards:
        return "EMPTY"
    if len(cards) > 1:
        # More than one entity survived. Score the run on the entity the case is
        # about -- the first card -- and say so, rather than silently picking.
        return cards[0]["state"] + f"(+{len(cards) - 1})"
    return cards[0]["state"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set")

    results = collections.OrderedDict()
    dangerous = []
    latencies = []

    for label, q, a, expected, also_ok in CASES:
        seen = []
        for _ in range(args.runs):
            t0 = time.time()
            out = corroboration.corroborate(q, a)
            latencies.append(time.time() - t0)
            seen.append(state_of(out, label))
            # A false claim rendered as agreement is the one error worth
            # naming individually.
            if expected == "conflicts" and seen[-1].startswith("corroborated"):
                dangerous.append((label, out["corroborations"][0].get("summary", "")))
        results[label] = (expected, also_ok, seen)
        base = [s.split("(")[0] for s in seen]
        stable = len(set(base)) == 1
        ok = all(b == expected or b in also_ok for b in base)
        mark = "OK  " if ok else ("~   " if any(b == expected or b in also_ok
                                                for b in base) else "MISS")
        print(f"{mark} {label:26} expected={expected:19} got={seen} "
              f"{'' if stable else 'UNSTABLE'}")

    print("\n" + "=" * 74)
    total = sum(len(v[2]) for v in results.values())
    hits = sum(1 for exp, alt, seen in results.values()
               for s in seen if s.split("(")[0] == exp or s.split("(")[0] in alt)
    print(f"runs: {total}   accepted: {hits}/{total}")
    print(f"latency: mean {sum(latencies)/len(latencies):.2f}s  "
          f"max {max(latencies):.2f}s  (hard stop {corroboration.HARD_STOP_SECONDS}s)")

    unstable = [k for k, (e, a, seen) in results.items()
                if len({s.split('(')[0] for s in seen}) > 1]
    if unstable:
        print(f"unstable across runs: {', '.join(unstable)}")

    print("\nDANGEROUS (a false claim reported as agreement): "
          f"{len(dangerous)}")
    for label, summary in dangerous:
        print(f"  {label}: {summary[:160]}")
    if dangerous:
        print("\nA run with any of these is a failing run regardless of the "
              "overall score.")


if __name__ == "__main__":
    main()
