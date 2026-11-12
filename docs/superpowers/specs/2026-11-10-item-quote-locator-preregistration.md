# Item quote locator measurement: pre-registration

Date: 2026-11-10. Spec: `2026-11-10-item-quote-locator-design.md` §6. Harness: `scripts/item_support_eval/`
(`arms.py`, `label.py`, `score.py`, `store.py`). Code under test: `src/item_locator.py`, plus the
locator prompt in `src/lambda_extract_session.py`.

This document is committed before the first measured call, and is frozen when the first result row
exists. Any later change goes in a dated addendum at the end. The addendum says whether it was
written before or after the result it might affect. It holds counts, hashes and rules only, and no
customer text.

## 1. Frozen identities

**Prompt.** The freeze covers the instruction block only: `prompt_sha` = sha256 (hex) of
`lambda_extract_session._instructions_block(locator)`, as `arms.prompt_sha` computes it. The
transcript half of the prompt changes with every session, so it is not hashed. Every run file
records the sha it was sent with, and `score locator` refuses an arm whose runs carry more than one.
The block depends only on `EMIT_EVIDENCE`. Both values are given here; the deployed TEST value is
`true`, which Task 9 passes as `--emit-evidence` after reading the deployed config.

| EMIT_EVIDENCE | Arm A (locator off) | Arm B (locator on) |
|---|---|---|
| true (deployed) | `e7e917e63e710419fc662f4bc93fda575f8602ceee548f03c2d012f698e6beea` | `d8acf3ff12c62bdfebf8a7d4e5e9c28e086220d21ce1e30487ee0a3560362155` |
| false | `9897bc04758647826dfbbb4bf1cf87bdb5fc94663147083c5b8e28be263e7e3a` | `3ee235112e9663750f8bfe78b2d1b8defdc04d0e55cc4488c7cb8acb8414d722` |

How the shas were computed: `arms.prompt_sha(False|True)` inside `arms.prompt_settings(emit)`, at
the commit that adds this document.

**Sessions.** The Task 10 manifest, `data/manifest.json`, has sha256
`38e50e20da1202fccdd29a9a819e08c51e6b644a514d513f380cb8b861eec2e3`. It holds 59 sessions, split
fit 30 / held-out 29. Run files and label files record this digest; scoring refuses any other.

**Owner-sample seed.** `store.LOCATOR_OWNER_SEED = "item-locator-owner-2026-11-10"`. Each stratum
draws with `random.Random(f"{seed}:{stratum}")` from its sorted item_refs.

**Planned calls** (one extraction call per run file, counted by `arms.py count`; `run` refuses
without `--confirm-count` equal to that number):

| Stage | Sessions × runs | Calls |
|---|---|---|
| fit | 30 × 4 | 120 |
| side | 6 × 4 | 24 |
| held-out (only if the fit gate passes) | 29 × 4 | 116 |
| **ceiling** | | **260** |

These are confirmed calls. `llm_utils` HTTP retries and its model fallback chain can add provider
requests beyond them, so the spend reported to the owner is the confirmed calls plus any retries.

## 2. Spec §6, verbatim

The text below is copied unchanged from the design spec. The rulings in §3 refine it. Where §3 is
more specific, §3 governs.

> ## 6. Measurement (pre-registered; frozen when the first result row exists)
>
> **Sessions.** The Task 10 manifest (digest `38e50e20…2eec2e3`, 59 sessions) and its frozen split, fit 30
> and held-out 29. Eligibility is the same, and the known duplicates stay in, as in Task 10.
>
> **Arms.** A = today's final prompt (locator off). B = locator on. Both use the deployed TEST LLM
> configuration (`load_deployed_llm_env("fieldsight-test-extract-session")`) with final-tier thinking,
> `speaker_names=None` and `continuity_block=""`, and each runs twice (A1, A2, B1, B2). The stored finals
> were made with names and, sometimes, a prior block, so they may differ from both arms. That is why A is
> run fresh.
>
> **Side set (reported only, status line's request).** These six TEST sessions carry known status
> reports: Ben_Lin_test2 2026-11-06 `sidc415ad…`, `sidf39bb7…`, `sid926509…`, `sidda4287…`, and
> 2026-11-07 `sida8cf83…`, `sidbdfd62…`. They run A/B × 2 with the deployed status-suggestion configuration:
> `continuity_block` is built as the deployed final builds it, with `status_suggestions.INSTRUCTION`.
> Report `status_suggestions` offered / kept / `dropped_by.weak` for each arm. If kept drops under B, the
> result goes to the status line before any flag.
>
> **Prompt frozen.** The locator wording is fixed before call 1, and its sha256 is recorded in every run
> file. There is no iteration on fit results. A wording change after any result row is a dated addendum
> and a complete new run.
>
> **Staged spend.** The fit half runs first: 30 × 4 = 120 extraction calls, plus the side set,
> 6 × 4 = 24. The held-out half (29 × 4 = 116) runs only if the fit half passes its gate (below).
> **260 is the ceiling, not a commitment.** Before the first call, the owner gets the exact count.
>
> **Gold.** A blind agent labeller sees each B1 item's text and its topic's turns, **without**
> `said_at`, and labels `turn` / `several` / `none`, as in Task 10. A link is correct only if the
> linked turn is gold's turn or one of gold's `several` turns. There is no adjacency or shared-token
> allowance: that rule is vacuously true across languages.
>
> **Owner sample.** 40 held-out B1 items, stratified: 20 `linked`, 12 `no_locator`, 8 from the refusal
> statuses. If a stratum is short, the remainder is filled from `linked`. The seed is fixed before the
> run. The owner labels blind, using the Task 10 format.
>
> **Fit gate (decides whether to spend on held-out only).** Bars 1, 3 and 4 on the fit half. Bar 3 needs
> ≥ 59 fit links; below that the verdict is `insufficient`, and the work stops with no held-out spend.
>
> **Bars (judged on fit + held-out pooled, since nothing is tuned between them; also reported per half):**
>
> 1. **Admission unchanged.** Per session, match items across runs one to one, same kind, by
>    `s = max(overlap(a, b), overlap(b, a)) ≥ 0.5`. Matching is greedy by `s` descending, ties broken by
>    kind, then item position. The noise floor is the A1↔A2 matched share (matched pairs ÷
>    mean(|A1|, |A2|) items). Both A1↔B1 and A2↔B2 must be ≥ (A1↔A2 share − 5 points). Also, B's mean
>    items per session (mean of B1, B2) must be within ±10% of A's (mean of A1, A2).
> 2. **Locator follow-through.** Reported, not gated: the share of B1 items with a non-null `said_at`
>    that resolve to `linked`, and the histogram of refusal statuses.
> 3. **Link precision.** Wrong links ÷ linked items, by gold: the one-sided 95% Clopper–Pearson upper
>    bound must be ≤ 5% (≥ 59 links).
> 4. **Twin hard negatives.** The labeller flags any link whose turn names a different unit, level,
>    block, room or direction than the item. Owner-confirmed flags must be 0. Answers are JSON booleans
>    only.
> 5. **Coverage.** Linked ÷ B1 final items ≥ 60% (the owner's line), using Task 10's denominator rules.
> 6. **`inferred` precision** (decides §4.4). Among `no_locator` items, the share that gold says a single
>    turn supports must have a CP upper bound ≤ 10%. This needs ≥ 29 such items: with 0 supported out
>    of 29 the bound is 9.8%, and below 29 even 0 supported fails. With fewer than 29 items the bar is
>    reported only and `inferred` stays off.
> 7. **Stability.** On items matched B1↔B2 (bar 1's matching) that both runs linked, both links point to
>    the same turn: ≥ 90%.
> 8. **Agent–owner agreement** on the owner sample ≥ 85% (the continuity bar). Required before any flag
>    is on, TEST included.
>
> **Reported, not gated:**
> - coverage by kind and by language pair (item script vs turn script);
> - the `ambiguous_stamp`, `not_shown` and `range_checked: false` rates;
> - the share of links whose `said_at` equals one of the topic's `evidence.at` stamps, which would
>   expose a model that copies the quote stamp onto every child;
> - output-token delta B vs A, from the calls' usage fields;
> - the latency delta.
>
> **Feasibility.** Pooled B1 items are about 550. At 60% coverage that is about 330 links, where a CP
> upper bound of 5% allows about 10 wrong. The fit half alone has about 300 items, so at the 26%
> coverage floor seen in Task 10 it would already have about 80 links.
>
> **Outcome.** Pass: TEST gets `EMIT_ITEM_LOCATOR=true` and `EMIT_ITEM_SUPPORT=true`; prod stays the
> owner's call. Fail: both flags stay off, the result is recorded as in Task 10, and nothing is tuned
> after the fact.

## 3. Controller rulings that refine §2 (all made before any result)

1. **Bar 6 decides `inferred_on` only.** It never enters the overall verdict. Below 29 `no_locator`
   items it is `reported_only` and `inferred` stays off. At 29 or more, a pass sets `inferred_on`;
   a fail or `insufficient` leaves it off. The locator flags are unaffected either way.
2. **Bar 1 share is pooled.** Each run pair (A1↔A2, A1↔B1, A2↔B2) gives one share:
   Σ matched pairs ÷ Σ mean(|X|, |Y|) over the scored sessions. Matching itself is per session.
   The ±10% check compares B's mean items per session (B1, B2) with A's (A1, A2). Children are
   normalised (`item_continuity.normalise_children`) in every run, A and B alike, before matching
   or counting.
3. **Exclusions.** A session is excluded from the paired bars (1, 7) and from the B1 bars (2, 3, 5,
   6, and the diagnostics) if any of the following holds:
   - any of its four run files is missing;
   - any of them errored;
   - any of them reports no answering model while the A1 runs do (`model_unknown`);
   - any of them was answered by a model other than the primary (a fallback).

   Each count is reported. Bar 4 reads every non-errored B1 link, since more links can only add
   flags.
4. **The primary model is computed once over all runs on disk.** It is the most common answering
   model across the A1 runs of every stage present, and it is recorded in the summary. The fit
   gate, both halves and the owner sample use this one exclusion set.
5. **Side set.** Its prior is the published final's own items, because S3 keeps only the latest
   extraction. Both arms share that prior, so only the B − A delta in kept suggestions is read.
   Absolute counts are not compared with production. If mean kept under B is below A, the result
   goes to the status line before any flag.
6. **Twin flags are released only after every non-empty todo is answered.**
   - `label locator-flags` writes the flag file, which shows the linked turns, only when every
     item_ref of `todo.locator.agent.jsonl` is in `done.locator.agent.jsonl`.
   - If `todo.owner.locator.jsonl` is non-empty, every one of its item_refs must also be in
     `done.owner.locator.jsonl`.
   - At the fit stage there is no owner todo, because the owner sample is held-out only. The
     agent set alone releases the fit flags, so bar 4 can be judged at the fit gate.
7. **Correct link** means the linked turn index is in gold `turns` (label `turn` or `several`).
   There is no adjacency or shared-token allowance. The turn index is
   `run.linked_turn_index(support, turns)`.
8. **Gold is a separate label set** (`labels/*.locator.*`), never mixed with Task 10's labels.
   - Blind rows carry no `said_at`, no support or status, and no arm information.
   - Each todo row records the sha of the B1 run file it was made from; scoring refuses labels
     whose B1 run file has changed.
   - Owner answers win over the agent's for bars 3 and 6. Bar 8 compares the raw answers.
9. **Owner sample strata.** linked / no_locator / refusal (`item_locator` refusal statuses).
   Another status (e.g. a missing support object) is in no stratum and is counted apart. Short
   strata are filled from linked.
10. **Verdict order** is fail > insufficient > pending_owner > pass.
    - Bar 8 is `pending_owner` while any owner answer is owed, and the overall verdict is then
      never pass.
    - An owner answer with no agent answer is `missing_agent`, verdict `insufficient`.
    - The overall verdict is `insufficient` if either half has no eligible session.
11. **Fit gate.** Bars 1, 3 and 4 on the fit half. Fewer than 59 fit links (gold present or not)
    gives `insufficient`, whatever the other bars say.

## 4. Known limits, reported with the result

- **Bar 4 candidates come from the `twin_flags` regex** (place classes and values). The labeller
  does not produce them. A twin the regex cannot read (e.g. a place named in words it does not
  parse) is not flagged.
- **The fit-stage primary may differ from the final one.** At the fit stage only fit runs are on
  disk; once held-out runs exist the mode is taken over all of them. If the two differ, the fit
  gate's exclusions differ from the final summary's. Both summaries record the primary.
- **Main-set `n_segments` is approximated** by the count of distinct source files among the stored
  turns. It can undercount the extractor's `len(source_filenames)`, and it is the same for A and B.
- **`said_at` = evidence `at` is readable only beside its base.** The summary reports how many
  topic evidence stamps carry seconds, next to the share of links whose stamp equals one.
- **Stray status suggestions** on B finals are counted from `status_suggestions_stripped`.
  Main-stage runs record that count before stripping.

## Addenda

(none)

### Result (2026-11-11, recorded 2026-11-12)

Counts only. The fit gate failed on bar 3, and held-out (116 calls) was not run.
- Spend: fit 120 + side 24 = 144 calls. All 30 sessions were answered by the primary model,
  with 0 errors and 0 fallback.
- Bar 3, link precision: 24 wrong / 321 links, CP upper 10.4% (bar 5%). Fail.
- Coverage 94.4% (en→en 99.6%, en→zh 82.7%). Stability 92.4%. Admission passed (A 10.78 / B
  11.03 items per session).
- Refusals: ambiguous_stamp 17, outside_topic 2. Output tokens +3.6%, latency +1.27 s. Side
  set: no drop in kept suggestions.
- Bar 4: 13 twin flags, all of the direction class. They were not answered before the verdict.
- **Cause.** 22 of the 24 wrong links point to an earlier turn than gold (12 exactly one turn
  earlier). The prompt asked for the line where the item was FIRST raised, while the gold asks
  which turn says it. This is a design flaw in the pre-registration, and nothing was rescored.
- **Deviation.** The eval loader did not copy `LLM_REASONING_EFFORT`, so both arms ran at the
  default effort.
- **Follow-up.** The re-test is
  `docs/superpowers/specs/2026-11-12-item-quote-locator-retest-design.md`; its result is in
  that document's section 8.
