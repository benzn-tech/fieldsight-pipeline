# Nightly turn pick (J2) N0 measurement harness

Pre-registration (binding): `docs/superpowers/specs/2026-11-10-nightly-turn-pick-eval-preregistration.md`.

Reads Track C's built set and labels read-only (defaults:
`C:\Users\camil\wt\track-c-eval\scripts\item_support_eval\{data,labels}`; they hold customer
text). Every write is refused outside `scripts/nightly_turn_pick_eval/runs/<name>/`
(gitignored), except that `score.py --publish` writes `results/summary.json`
(counts, rates and verdicts only, which is checked before the write). Nothing is printed
except counts.

Run from the repo root with `PYTHONPATH=<repo>;<repo>/src` and
`C:\Users\camil\fspy\Scripts\python.exe`. Paid calls go through the Claude API
(`ANTHROPIC_API_KEY`, `anthropic` SDK), never OpenRouter.

1. **Count** (no call, no client):
   `python -m scripts.nightly_turn_pick_eval.run --split fit --reps 3 --out scripts/nightly_turn_pick_eval/runs/fit_v0 --dry-run`
   and the same with `--split heldout`. The owner approves the count before any paid run.
2. **Fit v0**: the same command without `--dry-run`. If it is interrupted, re-run it with
   `--resume`: only (session, rep) pairs with no line in `log.jsonl` are called, and a
   recorded failed call is never called again.
   Score it (as often as needed): `python -m scripts.nightly_turn_pick_eval.score --run .../runs/fit_v0`.
3. **Wording revisions** (at most 2): change `INSTRUCTION`, log the new hash in `WORDING.md`
   first, then run 1 rep on fit (`--reps 1`). Bar 4 is not judged for 1 rep.
4. **Held-out**: commit the wording, then
   `run --split heldout --reps 3 --out .../runs/heldout --fit-run .../runs/<last fit run>`.
   This refuses unless `PROMPT_HASH` is the last `WORDING.md` entry, matches the fit run, and
   every other recorded hash matches the fit run's.
5. **Score once**: `score --run .../runs/heldout` writes `summary.json` (provisional),
   `flags.owner.jsonl` (bar 3(a)) and `adjudicate.owner.jsonl` (bar 5's adjudication), ids
   and turn indexes only. A second scoring is refused.
6. **Owner**:
   - First, the 45 blind answers to Track C's `labels/todo.owner.jsonl` in
     `labels/done.owner.jsonl`. Do this **before** opening `adjudicate.owner.jsonl`, which shows
     the agent's gold.
   - Then `runs/heldout/done.adjudicate.owner.jsonl` (Track C gold rows:
     `{"item_ref", "label", "turns"}`).
   - Then `runs/heldout/done.flags.owner.jsonl` (`{"flag_id", "confirmed": true|false}`; only a
     JSON boolean counts as an answer).

   Use Track C's view to open the texts.
7. **Final**: `score --run .../runs/heldout --addendum YYYY-MM-DD` (once ever; refused while
   any owner file is incomplete), then `score --run .../runs/heldout --publish` copies the
   newest summary to `results/summary.json`. A fit summary is published only with `--fit-ok`.

Run files: `meta.json` (model, `PROMPT_HASH`, parameters, input hashes, calls, failed calls,
tokens), `log.jsonl` (one line per (session, rep)), `rep<k>.jsonl` (one row per item:
`item_ref, session, kind, lang, raw_pick, turn_index, outcome, guard, cross_language,
script_unknown`) and `sessions.jsonl` (per session per rep: status, failure, stop_reason,
unknown aliases, tokens). `raw_pick` is the answered turn label, null when the model answered
null or left the item out, `__failed__` for a failed call. When an item is answered more than
once, the answers are joined with `|`.
