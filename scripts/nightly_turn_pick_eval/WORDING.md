# J2 prompt wording log

Pre-registration §5: the fit half fixes the wording of `nightly_turn_pick.INSTRUCTION` only,
with at most 2 wording revisions (going back to an earlier wording counts as one). Each
entry is logged here **before** it is run, with the new `PROMPT_HASH`, the reason and the fit
numbers that prompted it. The wording may not name sessions, sites, people or any text from
the set.

`run.py` refuses a real run unless the current `PROMPT_HASH` is the last `prompt_hash:` line
below, and a held-out run also needs its `--fit-run` to have used that same hash. Keep exactly
one `prompt_hash: <12 hex>` line per entry.

## v0 (baseline, not a revision)

prompt_hash: 46491db1afea

- Reason: the Task 1 wording (`src/nightly_turn_pick.py` at commit 2f53a3fc).
- Fit numbers: none yet (first fit run).
