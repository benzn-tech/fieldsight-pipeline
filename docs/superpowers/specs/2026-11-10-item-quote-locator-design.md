# Track C, slice 2: the model points at the line each item came from (design)

**Date:** 2026-11-10 · **Status:** design, direction approved by the owner 2026-11-10 ("行，按你的推进");
revised after review (§9) · **Branch:** `docs/item-quote-locator`
**Inputs:** slice 1 design `specs/2026-11-08-track-c-item-history-and-evidence-chain-design.md` (§3, §5.3,
§7, §9.2); its pre-registration `specs/2026-11-08-item-support-eval-preregistration.md` and the Task 10
result (PR #1261); claim provenance Phase A (`specs/2026-08-08-claim-provenance-design.md`).

---

## 1. Why

Slice 1 links each final item to the transcript turn it came from with a lexical rule. Task 10 measured
it on 59 TEST sessions and it failed its pre-registered fit:

- precision was fine (best point 0.987: 79 links, 1 wrong);
- coverage was about 26%, under the owner's 60% line (§9.2 of slice 1);
- `inferred` precision was 0: all 32 "nothing close" items were English item text over Chinese
  transcripts. The turn said it; word overlap could not see it.

The owner's §9.2 ruling: below 60%, the next step is to measure a prompt option in which the model
itself says where each item came from. On 2026-11-10 the owner approved going ahead, with one product
condition (§7): customers must not see citations everywhere.

Why it is worth a prompt change: a pointer does not depend on language, so it fixes exactly the failure
Task 10 found. It also makes `claim_type` computable for most items, and gives later features (a
"听原话" button, Ask citing the source) a stored locator instead of a recomputation.

## 2. Which option, and why it is not the "topic-quote index"

Slice 1 §3 named the "topic-quote index": each item points at one of its topic's 1–2 `evidence`
quotes. **This design measures a line locator instead:** each final child item carries `said_at`, the
`[HH:MM:SS]` stamp printed on the transcript line where the item was first raised. This is my reading of
§9.2, for three reasons:

1. **Ceiling.** A topic has 1–2 quotes; an item whose source line is not one of them cannot be linked
   by an index. A line locator can reach any line.
2. **Known behaviour.** The model already copies these stamps for topic evidence: on the 2026-08-10
   calibration, 40 of 42 quotes resolved within 1.0 s of the copied stamp (max 6.1 s). That was a
   windowed match. This design uses exact string equality, which is stricter, so a few copies that were
   close but not exact will become `no_line`. That is a coverage cost, accepted for precision.
3. **Same cost.** Measured on the 59 Task 10 sessions: one `"said_at": "HH:MM:SS"` per child adds a
   median 2.4% (p90 4.1%) of the topics JSON characters, in line with the ~3% the owner was told.
   Copying a quote per item (~20%) stays out.

## 3. Goals, non-goals, anti-goals

**Goals**
1. A gated prompt block and one child field `said_at`, on the **final single-session tier only**.
2. A pure resolver that turns `said_at` into one turn the model was shown, or refuses.
3. The resolver becomes the `item_support` path `locator`, with its own method and rule version,
   writing through the slice 1 pipeline (`support` object → item writer → `event_links`, `claim_type`).
4. A pre-registered measurement (§6) that must pass before any flag is on.

**Non-goals**
- No quote text in children (the +20% path). Topic `evidence` is unchanged.
- Not item numbers. `said_at` is a transcript line locator, internal only. It is unrelated to the
  per-project item number (#12) and to external references (Aconex, Procore, inspection numbers). Those
  belong to the "主动过清单" design and its external-reference field.
- No UI, no report wording, no Ask change (§7 records the owner's display principles for later).
- Live and group tiers: unchanged prompt, no locator.
- **No combination with the lexical paths in this slice.** Their constants are `None` and Task 10
  failed them, so combination logic would be code with no measurement behind it. With the locator
  enabled, the lexical paths do not run. If they are ever calibrated, how to combine them is a new
  design with its own measurement.

**Anti-goals**
- **A link to the wrong line.** The evidence chain would lie. Every refusal rule below exists for
  precision.
- **A link with no check behind it.** The locator cannot be verified by text, because cross-language
  items are the whole point. Its guard is structural: the stamp must name exactly one line the model
  was shown, inside the topic. On top of that sits the gold-labelled precision bar (§6, bar 3). Without
  that bar passing, no flag goes on.
- **Changing what gets extracted.** A prompt edit can shift which items the model admits. This has
  happened before: a prior block made the thinking model re-emit old items. §6 bar 1 blocks admission
  drift beyond run-to-run noise.
- **Costing status suggestions.** An action item can now carry two stamps: `said_at`, where it was
  raised, and `status_suggestion.at`, where its status was reported. A model that copies one into the
  other silently loses suggestions. §4.1 contrasts them, and §6 reports suggestion counts.

## 4. Design

### 4.1 Prompt (the only surface other lines also edit)

- New flag `EMIT_ITEM_LOCATOR`, default `false` everywhere. It is wired like `EMIT_ITEM_SUPPORT`:
  template parameter `EmitItemLocator`, both deploy workflows passing
  `vars.*_EMIT_ITEM_LOCATOR || 'false'`, and an env var on `ExtractSessionFunction`. Use a workflow
  variable, not a changed default, because SAM keeps old parameter values.
- `locator: bool` is threaded through `_instructions_block(locator=False)` and
  `build_extraction_prompt(..., locator=False)`. The single-session call site passes
  `final and EMIT_ITEM_LOCATOR`. The group prompt (which reuses `_instructions_block`) always passes
  `False`.
- **Byte-identical test:** with `locator=False`, the live prompt, the final prompt and the group prompt
  all equal today's.
- With `locator=True`:
  - the schema block adds `"said_at": "HH:MM:SS of the line where this item was FIRST raised, or
    null"` to each of the four child kinds;
  - a numbered instruction says to copy the stamp printed on the one line where the item was first
    raised, exactly as printed, hour included. Use null when no single line raises it (it combines
    several lines, or is your own conclusion). It is a stamp, not a quote: never copy words into it.
    It is not `status_suggestion.at`, which marks where a status was reported, usually later. The new
    rule states that contrast; `status_suggestions.INSTRUCTION` itself is not edited (the status
    line's request, 2026-11-10).
  - The existing evidence rule "Do NOT put evidence inside action_items or findings" stays. The new
    wording must not read as contradicting it.
- Independent of `EMIT_EVIDENCE`: stamps are printed whether or not evidence is requested.
- **The wording is frozen before the first measured call** (§6). Its sha256 goes in every run file.
- **Cross-line interface** (status line and jev TrackB both edit this prompt and were consulted
  2026-11-10, no objection): one gated block plus one child field, final single-session only, off =
  byte-identical. Nothing else in the prompt moves.

### 4.2 Resolver (`item_locator.py`, pure, no AWS, no DB)

**Inputs:** the final result, the turns, and the set of turn indices the model was actually shown.
`render_transcript` / `elide_middle` drop middle lines when the transcript exceeds
`TRANSCRIPT_TEXT_LIMIT`, so the shown set must come back from rendering. Today rendering returns only a
count. A stamp that falls in the elided gap would name a real turn the model never saw, so it must
not link.

For each final child with a `said_at`:

1. Parse it with the slice 1 time parser and normalise to `%H:%M:%S` (the parser accepts `9:05:03`).
   Anything else → `unparseable`. Null or absent → `no_locator`.
2. Candidates are the **shown** turns whose printed `abs_start_str` equals the normalised stamp.
   - Zero shown matches, but a match exists among the elided turns → `not_shown`.
   - Zero matches at all → `no_line`.
   - More than one → `ambiguous_stamp`. Two diarised speakers starting in the same second, in one file
     or across files, cannot be told apart. The rate is reported as a coverage cost.
3. The turn must lie inside the topic's `time_range`, widened by one minute each side, using
   `_window_turns`' parsing and midnight-shift rule. Outside → `outside_topic`. This check is a
   coverage choice more than a guard: it only catches a stamp copied from another topic's span. If the
   topic's `time_range` does not parse, the check is skipped and the support object records
   `range_checked: false` (reported).
4. Otherwise `linked`, with the slice 1 locator `{source_stem, start_sec, end_sec, speaker_label,
   quote_status}`, `method = rule_version = "locator_v1"`, and score null.

**Strip.** After resolution, `said_at` is popped in place from the child's top level, on all four child
kinds. The strip runs on every final artifact, regardless of flags and on every code path, including
the continuity fallback path (`lambda_extract_session` ~L2308, where `normalise_children`/`resolve`
raise). It never touches `status_suggestion` or its `at`, never rebuilds or reorders child lists, and
runs **after** the continuity resolve and `_run_linker`. `assign_refs` numbers children by position, so
reordering would misalign links. The group path strips `said_at` too, defensively. Refusals carry their
reason and no locator.

### 4.3 Wiring into `item_support` and the writer

- `PATHS` gains `"locator"`. With `EMIT_ITEM_LOCATOR` on, `attach_support` runs the locator path alone;
  lexical constants stay `None` and are not consulted. It no longer raises on `None` constants when only
  the locator path runs. With the locator off, slice 1 behaviour is unchanged (raises on `None`).
- New statuses: `no_locator`, `unparseable`, `not_shown`, `no_line`, `ambiguous_stamp`,
  `outside_topic`. They are added to `attach_support`'s counts, `claim_type_store.SUPPORT_STATUSES` and
  the item writer's `_SUPPORT_METRIC`, so refusals show on the dashboard instead of being dropped (or
  raising `KeyError`).
- The item writer passes `support["method"]` to `event_links.insert_supported_by`. Today it falls back
  to the default `lexical_v1`. No migration is needed, because `method` is text and `score` is nullable.

### 4.4 `claim_type` rule v2

`claim_type.derive` gains the locator statuses, as rule `claim_v2`:
- `linked`: as v1. Recorder identified → `observed`; someone else → `stated`; unidentified → `NULL`.
- `no_locator` (the model said no single line raises it): `inferred` **only if §6 bar 6 passes**,
  otherwise `NULL`. This is a code constant set from the measurement result.
- Every refusal → `NULL`. `none_close` cannot occur under v2, because the lexical paths do not run.
- The voiceprint writer's recompute and the backfill reuse `derive` unchanged in shape.

### 4.5 Flags

`EMIT_ITEM_LOCATOR` changes the prompt and runs the resolver. `EMIT_ITEM_SUPPORT` (slice 1) still gates
attaching and writing. With the locator on and support off, the model emits `said_at`, the extractor
strips it, and nothing is stored. That combination is legal, but nobody should leave it on.

## 5. Interfaces and ownership

| Party | Gets | Owns | Tests it asked for |
|---|---|---|---|
| Status line | fuller `find_items.origin` (its reconcile #1270 does not read origin) | `status_suggestions` and its instruction | the strip leaves `status_suggestion` byte-identical; suggestion counts reported in §6 |
| jev TrackB | nothing changes for it; the linker sees `{ref, kind, text}` only | continuity, the linker | strip on the fallback path; locator + `CONTINUITY_LINKER` on → links land on the same children |
| Architecture line (nightly agent) | `method='locator_v1'` rows beside future `nightly_*` repairs | unchanged | — |

## 6. Measurement (pre-registered; frozen when the first result row exists)

**Sessions.** The Task 10 manifest (digest `38e50e20…2eec2e3`, 59 sessions) and its frozen split, fit 30
and held-out 29. Eligibility is the same, and the known duplicates stay in, as in Task 10.

**Arms.** A = today's final prompt (locator off). B = locator on. Both use the deployed TEST LLM
configuration (`load_deployed_llm_env("fieldsight-test-extract-session")`) with final-tier thinking,
`speaker_names=None` and `continuity_block=""`, and each runs twice (A1, A2, B1, B2). The stored finals
were made with names and, sometimes, a prior block, so they may differ from both arms. That is why A is
run fresh.

**Side set (reported only, status line's request).** These six TEST sessions carry known status
reports: Ben_Lin_test2 2026-11-06 `sidc415ad…`, `sidf39bb7…`, `sid926509…`, `sidda4287…`, and
2026-11-07 `sida8cf83…`, `sidbdfd62…`. They run A/B × 2 with the deployed status-suggestion configuration:
`continuity_block` is built as the deployed final builds it, with `status_suggestions.INSTRUCTION`.
Report `status_suggestions` offered / kept / `dropped_by.weak` for each arm. If kept drops under B, the
result goes to the status line before any flag.

**Prompt frozen.** The locator wording is fixed before call 1, and its sha256 is recorded in every run
file. There is no iteration on fit results. A wording change after any result row is a dated addendum
and a complete new run.

**Staged spend.** The fit half runs first: 30 × 4 = 120 extraction calls, plus the side set,
6 × 4 = 24. The held-out half (29 × 4 = 116) runs only if the fit half passes its gate (below).
**260 is the ceiling, not a commitment.** Before the first call, the owner gets the exact count.

**Gold.** A blind agent labeller sees each B1 item's text and its topic's turns, **without**
`said_at`, and labels `turn` / `several` / `none`, as in Task 10. A link is correct only if the
linked turn is gold's turn or one of gold's `several` turns. There is no adjacency or shared-token
allowance: that rule is vacuously true across languages.

**Owner sample.** 40 held-out B1 items, stratified: 20 `linked`, 12 `no_locator`, 8 from the refusal
statuses. If a stratum is short, the remainder is filled from `linked`. The seed is fixed before the
run. The owner labels blind, using the Task 10 format.

**Fit gate (decides whether to spend on held-out only).** Bars 1, 3 and 4 on the fit half. Bar 3 needs
≥ 59 fit links; below that the verdict is `insufficient`, and the work stops with no held-out spend.

**Bars (judged on fit + held-out pooled, since nothing is tuned between them; also reported per half):**

1. **Admission unchanged.** Per session, match items across runs one to one, same kind, by
   `s = max(overlap(a, b), overlap(b, a)) ≥ 0.5`. Matching is greedy by `s` descending, ties broken by
   kind, then item position. The noise floor is the A1↔A2 matched share (matched pairs ÷
   mean(|A1|, |A2|) items). Both A1↔B1 and A2↔B2 must be ≥ (A1↔A2 share − 5 points). Also, B's mean
   items per session (mean of B1, B2) must be within ±10% of A's (mean of A1, A2).
2. **Locator follow-through.** Reported, not gated: the share of B1 items with a non-null `said_at`
   that resolve to `linked`, and the histogram of refusal statuses.
3. **Link precision.** Wrong links ÷ linked items, by gold: the one-sided 95% Clopper–Pearson upper
   bound must be ≤ 5% (≥ 59 links).
4. **Twin hard negatives.** The labeller flags any link whose turn names a different unit, level,
   block, room or direction than the item. Owner-confirmed flags must be 0. Answers are JSON booleans
   only.
5. **Coverage.** Linked ÷ B1 final items ≥ 60% (the owner's line), using Task 10's denominator rules.
6. **`inferred` precision** (decides §4.4). Among `no_locator` items, the share that gold says a single
   turn supports must have a CP upper bound ≤ 10%. This needs ≥ 29 such items: with 0 supported out
   of 29 the bound is 9.8%, and below 29 even 0 supported fails. With fewer than 29 items the bar is
   reported only and `inferred` stays off.
7. **Stability.** On items matched B1↔B2 (bar 1's matching) that both runs linked, both links point to
   the same turn: ≥ 90%.
8. **Agent–owner agreement** on the owner sample ≥ 85% (the continuity bar). Required before any flag
   is on, TEST included.

**Reported, not gated:**
- coverage by kind and by language pair (item script vs turn script);
- the `ambiguous_stamp`, `not_shown` and `range_checked: false` rates;
- the share of links whose `said_at` equals one of the topic's `evidence.at` stamps, which would
  expose a model that copies the quote stamp onto every child;
- output-token delta B vs A, from the calls' usage fields;
- the latency delta.

**Feasibility.** Pooled B1 items are about 550. At 60% coverage that is about 330 links, where a CP
upper bound of 5% allows about 10 wrong. The fit half alone has about 300 items, so at the 26%
coverage floor seen in Task 10 it would already have about 80 links.

**Outcome.** Pass: TEST gets `EMIT_ITEM_LOCATOR=true` and `EMIT_ITEM_SUPPORT=true`; prod stays the
owner's call. Fail: both flags stay off, the result is recorded as in Task 10, and nothing is tuned
after the fact.

## 7. Display principles (owner, 2026-11-10; for later slices, not built here)

- No citations by default. Lists, the Tasks page and report bodies show no markers or footnotes.
- Only on demand, in an item's detail view: one "听原话" entry. It plays from the linked line and shows
  that line's transcript beside it. One entry per item, even if several lines are linked.
- No `claim_type` tag (already ruled 2026-11-08).
- Flagging items with no source line for managers is a later decision, not this slice.

## 8. Left to the builder

- Module and function names.
- The exact prompt wording within §4.1's constraints, frozen before the first call.
- The harness layout: extend `scripts/item_support_eval/` and reuse `scripts/continuity_eval`'s LLM-env
  loader.
- How rendering reports the shown turn indices, as long as it is exact under `elide_middle`.

## 9. Review record (2026-11-10)

An independent review raised 13 findings, all adopted:
- the elided-turn hole (`not_shown`);
- the group prompt leak (`locator` threaded through `_instructions_block`);
- the lexical-combination cut;
- the stratified owner sample and the bar 6 minimum n;
- the frozen prompt with pooling;
- the strict correct-link rule;
- pinned arm inputs;
- the new statuses in the metric maps;
- the unparseable `time_range`;
- stamp normalisation;
- the exact-match coverage note and the copied-evidence diagnostic;
- the bar 1 definition;
- the fit-gate minimum.

The status line and jev TrackB each added tests (§5) and the side set (§6).
