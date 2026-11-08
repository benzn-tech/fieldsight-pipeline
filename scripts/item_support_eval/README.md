# item_support measurement harness

Pre-registration: `docs/superpowers/specs/2026-11-08-item-support-eval-preregistration.md`
(frozen at the first result row; later changes go in dated addenda there).

Everything under `data/`, `runs/` and `labels/` holds folder names, item text or turn text and
is gitignored; every write is refused outside those three directories. The only committed
output is `results/summary.json` (counts, rates, verdicts).

Run from the repo root with `PYTHONPATH=<repo>;<repo>/src` and the `fieldsight-deployer`
profile (boto3 only; no aws CLI loops). Nothing writes to S3 or a database.

1. **Build** the TEST set (finals with evidence since 2026-08-01, amendment A12, seeded split):
   `python -m scripts.item_support_eval.build`
   Check `data/manifest.json`: `sessions` >= 40, and `resolved_quotes` close to
   `verified_quotes` (turns are rebuilt from today's transcripts).
2. **Gold, agent:** `python -m scripts.item_support_eval.label export` writes
   `labels/todo.agent.jsonl` (every item, both halves) and `labels/todo.owner.jsonl` (the 45-item
   held-out sample). Give the agent `label instruction` plus `todo.agent.jsonl`; it writes
   `labels/done.agent.jsonl`. The view is blind: no score, status or path.
3. **Fit:** `python -m scripts.item_support_eval.run grid` then
   `python -m scripts.item_support_eval.score fit` -> `runs/fit/choice.json`. Commit the chosen
   constants into `src/item_support.py` with the fit numbers in the commit body.
4. **Held-out, twice:** `run point --floor F --margin M --nothing-close N --paths quote,window
   --out scripts/item_support_eval/runs/heldout_a` and again to `runs/heldout_b`.
5. **Owner:** the owner answers `labels/todo.owner.jsonl` blind into `labels/done.owner.jsonl`.
   Then `label adjudicate --run .../runs/heldout_a` writes the agent-vs-heuristic disagreements
   in the sample (`labels/adjudicate.owner.jsonl` -> `done.adjudicate.owner.jsonl`), and
   `label flags --run .../runs/heldout_a` writes bar 2's twin flags (`labels/flags.owner.jsonl`
   -> `done.flags.owner.jsonl`, `{"flag_id", "confirmed"}`).
6. **Score:** `python -m scripts.item_support_eval.score summary --run-a .../heldout_a
   --run-b .../heldout_b` -> `results/summary.json`. If bar 6 keeps one path only
   (`rerun_needed: true`), rerun step 4 with `--paths <that path>`, redo step 5's flags, and
   score again with `--both-paths-run` pointing at the two-path run.

Optional inputs for the reported numbers: `--latency-json` (a `find_items` TEST invoke) and
`--claim-type-json` (a TEST in-VPC count); without them the summary says "not measured".
