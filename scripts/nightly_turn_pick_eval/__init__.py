"""N0 measurement harness for the nightly turn pick (J2); pre-registration
docs/superpowers/specs/2026-11-10-nightly-turn-pick-eval-preregistration.md.

Reads Track C's built set and labels read-only. Everything that holds an item ref, a turn
index or a model answer lands under the gitignored runs/ directory next to this file; only
results/summary.json (counts, rates, verdicts) is meant to be committed.
"""
