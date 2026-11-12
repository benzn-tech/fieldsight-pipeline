# Item quote locator: re-test design (2026-11-12)

Follows `2026-11-10-item-quote-locator-design.md` and its pre-registration
`2026-11-10-item-quote-locator-preregistration.md`. This document changes the locator
question and the measurement procedure. A separate re-test pre-registration is committed
after the tuning step and before any held-out call (section 6).

## 1. Why

The pre-registered fit gate failed on link precision (counts only):

| | |
|---|---|
| B1 items / linked | 340 / 321 (coverage 94.4%) |
| Wrong links vs blind agent gold | 24 (CP upper 10.4%, bar 5%) |
| Wrong link points to an EARLIER turn than gold | 22 / 24 (12 exactly one turn earlier) |
| Refusals | ambiguous_stamp 17, outside_topic 2 |
| Stability B1/B2 | 92.4% |

The locator asked for the line where the item was "FIRST raised". The gold asks which turn
says the item. The two questions differ, and 22/24 errors are the predicted shape of that
mismatch. The fit result stands as recorded. Nothing below re-scores it.

A deviation was found as well: the eval copied no `LLM_REASONING_EFFORT` from the deployed
function, so both arms ran at the provider default instead of TEST's `high`.

## 2. Owner decisions (2026-11-11/12)

1. Option B. Change the locator question and keep the gold as it is. The question becomes
   "the turn that states this item". This is the gold's question and the question J2 asks
   (`src/nightly_turn_pick.py`), so locator and J2 results stay comparable.
2. Playback context belongs to the UI. The stored link points at the stating turn, and the web
   player starts one turn earlier. No "earlier turn is also fine" tolerance goes into the gold.
3. The eval runs at `LLM_REASONING_EFFORT=high`, as deployed.
4. Tuning uses only the fit half. Stop and report if the fit B1 wrong-link count is above 9.
   With about 320 links, the 5% CP bar is out of reach above that. This design tightens the
   continue rule (section 4, step 2) because the verdict is now held-out only (Fable review,
   finding 1).
5. Any "both turns acceptable" rule must be in the committed re-test pre-registration before
   held-out, and a blind reviewer judges it. This design adds none. The existing gold label
   `several` (more than one turn each says the item) stays the only multi-turn case, and the
   blind labeller decides it.
6. Budget: 170 calls, cap 200.
   - Tune: 30 B1 calls on the fit half, plus at most one more wording at 30.
   - Side set: 24.
   - Held-out: 116.

   The fit A1, A2 and B2 arms are NOT re-run. Bars 1 (admission) and 7 (stability) are judged
   on held-out only. Spend so far is 144, so the programme total becomes 314, cap 344, against
   the old 260 ceiling (owner approved).
7. Flags `EMIT_ITEM_LOCATOR` and `EMIT_ITEM_SUPPORT` stay off. They stay off until the
   held-out bars pass and the owner has blind-labelled the 40-item sample.

## 3. Changes

### C1. Locator wording (`src/lambda_extract_session.py`, locator=True prompts only)

- **Schema line.** `"said_at": "HH:MM:SS of the ONE line that states this item, or null"`.
- **Rule 8 says:**
  - `said_at` is the `[HH:MM:SS]` printed on the ONE line that states the item, meaning the
    line that says what the item says;
  - copy it exactly as printed, the hour included;
  - if the item is stated on more than one line, use the line that states it most fully; if
    those are equal, use the earliest;
  - use null when no single line states it (it combines several lines, or is your own
    conclusion);
  - it is a stamp, not evidence and not a quote;
  - it is not the `at` of a status_suggestion, which marks where a status was reported,
    usually later. This clause is kept verbatim from the current rule.
- **Unchanged:**
  - with the flag off, every prompt is byte-identical (tests assert this);
  - the group prompt never asks for `said_at`;
  - the strip still runs.
- **Recorded:** the new B instruction-block sha goes in the re-test pre-registration. The A sha
  is unchanged.

### C2. Same-second tie-break (`src/item_locator.py`)

- **What the data shows.** 16 of the 17 `ambiguous_stamp` refusals are two adjacent turns
  that start in the same second. One of the two is a diarization fragment of 0.0–0.3 s. The
  17th is one speaker split across two source files, both pieces starting in the same second.
- **New rule.** When more than one shown turn matches the stamp:
  - if exactly one of them has `end_sec - start_sec >= 0.5`, link that turn;
  - otherwise refuse with `ambiguous_stamp`, as before.
- **Duration.** A missing or non-numeric `start_sec`/`end_sec` counts as duration 0.
- **Version.** `RULE_VERSION` becomes `locator_v2`. No src module whitelists method strings:
  `claim_type_store` only defaults the method, and `KNOWN_PATHS` holds path names. The
  hard-coded `locator_v1` strings are in tests only, and those tests are updated. The
  real-Postgres write/read-back test is the check. No rows exist yet because the flags are off.
- **What this rule costs.** A genuine turn under 0.5 s next to a long fragment refuses (coverage
  cost, safe).
- **Data on the 59 sessions.** There are 325 same-second groups:
  - 270 have exactly one turn >= 0.5 s, and would link;
  - 27 have none, and refuse;
  - 28 have two or more, and refuse.

  Before freezing, the re-test pre-registration reports the duration histogram of the turns
  the fit gold names.
- **Scorer index fix.** `scripts/item_support_eval/run.py` `linked_turn_index` matches
  `(source_stem, start_sec)` and returns the FIRST match. 9 groups have two turns with
  identical `(stem, start_sec)`. The fix matches on `end_sec` as well. A unit test covers two
  turns that share `start_sec`. The fix applies to every reader that rebuilds a turn from a
  support object.

### C3. Eval reads the deployed reasoning effort (`scripts/item_support_eval/arms.py`)

- After `load_deployed_llm_env`, `arms.py` also copies these keys from the deployed
  function's configuration into `os.environ`:
  - `LLM_REASONING_EFFORT`, `LLM_FALLBACK_MODELS`;
  - `LLM_REASONING_HEADROOM`, `LLM_ANSWER_TOKEN_CEILING`, `LLM_HTTP_TIMEOUT`;
  - `LLM_CHAIN_BUDGET_SECONDS`, `QWEN_ENABLE_THINKING`.

  It then reloads `llm_utils`, the same reason `load_deployed_llm_env` reloads.
- **Empty vs unset.** A key that is set but empty on the function stays set and empty. An empty
  `LLM_FALLBACK_MODELS` means no fallback. A key missing on the function is removed from
  `os.environ`.
- **Extractor env.** `TRANSCRIPT_TEXT_LIMIT`, `EVIDENCE_*` and `STATUS_EVIDENCE_FLOOR_TOKENS`
  are compared with the deployed function before `lambda_extract_session` is imported.
  `run` refuses on a mismatch.
- **Run files record:**
  - `reasoning_effort`;
  - `fallback_models`;
  - the full copied key/value map, without API keys.
- `run` refuses to start when the deployed function has no `LLM_REASONING_EFFORT`, unless
  `--allow-default-effort` is passed.
- `scripts/jev_eval/baseline.py` is not edited: it is shared with another line.

### C4. Run namespace and arm subset (`arms.py`, `store.py`)

- **New namespace.** Re-test runs go under `runs/locator_v2/<wording>/`, where `<wording>` is
  `w1` or `w2` (`--wording`, required for the v2 namespace). Labels follow the same split:
  `labels/v2/<wording>/`. The first test's `runs/locator/` is left untouched.
- **Arm subset.** `run --arms B1` (comma list from A1, A2, B1, B2; default all four) restricts
  the arms. `count` honours the same flag. `--confirm-count` must equal the restricted count.

### C5. Scoring stages (`scripts/item_support_eval/score.py`)

**`score locator --stage tune`** reads the fit half, B1 only, from `locator_v2`, and reports:

- bar 3: wrong links, links, CP upper;
- bar 4: twin flags;
- coverage;
- refusal histogram;
- the decision `continue` / `ask_owner` / `stop`, by the rule in section 4, step 2;
- `admission_early`, reported only: the A1-B1 matched share and items-per-session delta. It
  uses the first test's fit A1/A2 runs, whose A sha is unchanged and whose reasoning effort is
  recorded. It costs 0 calls, and the effort difference is labelled.
- the share of links whose `said_at` equals the child's own `status_suggestion.at`.

It has no stability bar, because only B1 exists. Tune eligibility is B1-only. A session is
excluded when:
- its B1 run errored;
- its answering model is None;
- its answering model differs from the deployed primary recorded in the run file.

`primary_model` is never computed from absent A1 runs.

**`score locator --stage heldout`** reads the held-out half from `locator_v2` and runs:

- bars 1, 3, 4, 5, 7. Bar 7 is gated strictly, as before. It is also reported
  "several-aware": both picks fall inside gold's `several` turns.
- bar 6, reported only (held-out alone will almost surely have fewer than 29 `no_locator`
  items);
- the `status_suggestion.at` diagnostic;
- bar 8, owner agreement;
- the side set from `locator_v2`.

The verdict order is unchanged.

The existing `fit` and `all` stages keep working on `runs/locator/`, so the first result is
reproducible.

### C6. Labels

- **Gold instruction.** `BLIND_LABEL_INSTRUCTION` is unchanged.
- **Separate files.** Re-test label files are separate: `labels/v2/`, the same file names
  inside.
- **Owner sample.** The held-out owner sample stays 40 items: 20 linked, 12 no_locator,
  8 refusal, with the same seed rule.
- **Collision check.** Owner labelling is published as a blind page, and only after the
  nightly line's Task 10 page is done (one owner batch at a time).

## 4. Order of work

1. One PR to develop with C1–C6 and tests. This includes a real-Postgres integration test
   that a `locator_v2` support row writes and reads back. Merge when CI is green. The TEST
   deploy keeps the flags off.
2. Tune: run 30 B1 calls on the fit half → blind agent labels (`labels/v2/`) → `score --stage
   tune`.
   - **Continue rule.** Held-out at about 300 links allows at most about 8 wrong for the 5% CP
     bar, and about 5 if coverage falls to 60%.
     - `continue` when wrong <= 5 and coverage >= 60%.
     - `ask_owner` when wrong is 6-9 and coverage >= 60%. Held-out would likely fail; the owner
       decides whether to spend 116 calls.
     - `stop` when wrong > 9 or coverage < 60%. This is the owner's stop line.
   - **Second wording.** On `stop` or `ask_owner`, if it is in budget, change only the rule-8
     sentence once (w2) and re-run 30 into `runs/locator_v2/w2/`. Both wordings are recorded.
     The held-out wording is the one with fewer wrong links; a tie goes to w1. If neither
     reaches `continue`, report to the owner.
   - **Attribution.** The tune step changes wording and reasoning effort together. A drop in
     wrong links is not attributed to the wording alone.
3. Write the re-test pre-registration (section 6) and merge it as its own docs PR.
4. Run the side set (24) and held-out (116) → blind agent labels → owner sample → `score
   --stage heldout`.
5. Record the counts in the re-test pre-registration's result section. Tell the
   architecture, status and TrackB lines.

## 5. Non-goals

- No change to the gold instruction, the bars, or the 5% threshold.
- No adjacency tolerance.
- No re-scoring of the first fit result.
- No prod change. No flag flips before the owner sample.

## 6. What the re-test pre-registration must freeze

- The B instruction-block sha (the wording chosen at tuning).
- The `locator_v2` tie-break rule.
- `reasoning_effort=high`.
- The held-out manifest digest and owner seed.
- The wording chosen for held-out, w1 or w2, by the selection rule.
- The expected held-out link count, and the wrong count that keeps CP upper <= 5% at that count.
- The bars as in the 2026-11-10 pre-registration §2, with three exceptions:
  - the fit gate is replaced by the tune stop rule (wrong ≤ 9 on fit B1);
  - bars 1 and 7 are on held-out only;
  - pooling is held-out only, because fit is now a tuning set.
- The call counts.

## 7. Review record

Fable reviewed this design on 2026-11-12. All nine findings are adopted:
1. The tune rule is sized to held-out n, as `continue` / `ask_owner` / `stop`.
2. The scorer index matches on `end_sec`, and the data counts are added.
3. Tune eligibility is B1-only.
4. Each wording gets its own namespace, and the selection rule is stated.
5. The status_suggestion clause is kept, and the `at` diagnostic is added.
6. Bar 7 is also reported several-aware. Note that J2 has no tie-break and may answer null
   where the locator picks a line, so comparability is approximate.
7. More LLM keys are mirrored, empty and unset are kept apart, and an extractor env mismatch
   refuses the run.
8. An early admission signal from the fit A arms is reported at 0 calls, and bar 6 is reported
   only.
9. The method-whitelist claim is corrected.

## 8. Result (2026-11-12)

Counts only. Neither wording reached `continue`, so held-out, the side set and the owner
sample were not run. The locator does not ship: `EMIT_ITEM_LOCATOR` stays off.

| Tune (fit half, B1, 30 sessions, effort high) | w1 | w2 |
|---|---|---|
| Items / linked | 337 / 318 | 317 / 309 |
| Wrong links | 17 | 25 |
| CP upper (bar 5%) | 7.9% | 11.1% |
| Coverage | 94.4% | 97.5% |
| ambiguous_stamp / outside_topic / no_locator | 4 / 1 / 14 | 2 / 6 / 0 |
| Wrong links one turn early | 3 | 13 |
| Errors / fallback | 0 / 0 | 0 / 0 |
| Decision | stop | stop |

- **w1** asks for the line that states the item. It removed the first test's one-sided error:
  10 of 17 wrong links point earlier and 7 point later. 11 of 17 are within two turns, 16 of 17
  are on Chinese turns, and 15 of 17 sit in three sessions.
- **w1 disagreements, adjudicated.** A blind A/B judge saw both candidate turns in random order.
  It picked gold 12 times (9 with high confidence), both 4 times (the item is split across
  adjacent turns), neither once, and the locator 0 times. This was a diagnostic and nothing was
  rescored. Even with the 4 "both" counted correct, 13 wrong is above the stop line.
- **w2** changed one rule-8 sentence: a combined item takes the line that states its core. It
  linked more items, but the one-turn-early error came back and English-turn errors rose. The
  selection rule would pick w1, but it did not reach `continue`.
- **The ≥ 0.5 s tie-break** cut ambiguous_stamp refusals from 17 (first test) to 4 and 2.
- **Spend.** 60 calls of the 170 approved. Programme total: 204.
- **Recorded deviation.** `admission_early` came back null: the first test's fit A runs were not
  present in the re-test worktree.
- **Experiment code.** The w2 wording was measured from a local experiment branch and was not
  merged. Develop carries w1.
