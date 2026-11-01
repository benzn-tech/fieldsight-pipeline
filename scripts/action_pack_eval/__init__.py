"""Evaluation harness for action packs (spec 2026-11-01 background action agent §8, §9,
§14 S16). Scores recorded packs against an owner-supplied gold set and human labels; every
number is counted in code from files, never graded by a model. Runs on TEST only; real gold,
labels, packs and reports stay under a gitignored `action_pack_eval_runs/` directory. See
README.md and SCHEMA.md in this folder."""
