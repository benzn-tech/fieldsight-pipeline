# Track C, slice 2: the model points at the line each item came from (design)

**Date:** 2026-11-10 · **Status:** design, direction approved by the owner 2026-11-10 ("行，按你的推进");
pending review · **Branch:** `docs/item-quote-locator`
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
`[HH:MM:SS]` stamp printed on the transcript line it came from. This is my reading of §9.2, for three
reasons:

1. **Ceiling.** A topic has 1–2 quotes; an item whose source line is not one of them cannot be linked
   by an index. A line locator can reach any line.
2. **Proven behaviour.** The model already copies these stamps accurately for topic evidence: on the
   2026-08-10 calibration, 40 of 42 quotes resolved within 1.0 s of the copied stamp (max 6.1 s).
3. **Same cost.** Measured on the 59 Task 10 sessions: one `"said_at": "HH:MM:SS"` per child adds a
   median 2.4% (p90 4.1%) of the topics JSON characters. That is in line with the ~3% the owner was
   told. Copying a quote per item (~20%) stays out.

## 3. Goals, non-goals, anti-goals

**Goals**
1. A gated prompt block and one child field `said_at`, on the **final tier only**.
2. A pure resolver that turns `said_at` into one turn, or refuses.
3. The resolver is a new `item_support` path, `locator`, with its own method and rule version, writing
   through the slice 1 pipeline (`support` object → item writer → `event_links`, `claim_type`).
4. A pre-registered measurement (§6) that must pass before any flag is on.

**Non-goals**
- No quote text in children (the +20% path). Topic `evidence` is unchanged.
- Not item numbers. `said_at` is a transcript line locator, internal only. It is unrelated to the
  per-project item number (#12) and to external references (Aconex, Procore, inspection numbers). Those
  belong to the "主动过清单" design and its external-reference field.
- No UI, no report wording, no Ask change (§7 records the owner's display principles for later).
- Live and group tiers: unchanged prompt, no locator. Group extractions have no shared clock.
- Not calibrating the lexical paths. They stay as built, refusing while their constants are `None`.

**Anti-goals**
- **A link to the wrong line.** The evidence chain would lie. Every refusal rule below exists for
  precision.
- **A link with no check behind it.** The locator cannot be verified by text: cross-language items are
  the whole point. Its guard is structural (the stamp must name exactly one printed line inside the
  topic) plus the gold-labelled precision bar (§6, bar 3). Without that bar passing, no flag goes on.
- **Changing what gets extracted.** A prompt edit can shift which items the model admits (seen before:
  a prior block made the thinking model re-emit old items). §6 bar 1 blocks any admission drift beyond
  run-to-run noise.

## 4. Design

### 4.1 Prompt (the only surface another line owns)

- New flag `EMIT_ITEM_LOCATOR`, default `false` everywhere, wired like `EMIT_ITEM_SUPPORT`: template
  parameter `EmitItemLocator`, both deploy workflows passing `vars.*_EMIT_ITEM_LOCATOR || 'false'`, env
  in `ExtractSessionFunction`. Use a workflow variable, not a changed default (SAM keeps old parameter
  values).
- `build_extraction_prompt` gains a `locator: bool` argument. The call site passes
  `final and EMIT_ITEM_LOCATOR`. With `locator=False` the prompt is **byte-identical** to today's, on
  every tier. A test asserts that.
- With `locator=True`:
  - the schema block adds `"said_at": "HH:MM:SS of the line this item was said in, or null"` to each of
    the four child kinds;
  - a numbered instruction says: copy the stamp printed on the one line where this item was said,
    exactly as printed, hour included. Use null when no single line says it (the item combines several
    lines, or is your own conclusion). It is not a quote: never copy words into it.
  - The existing evidence rule "Do NOT put evidence inside action_items or findings" stays. The new
    wording must not read as contradicting it: `said_at` is a stamp, not evidence.
- Independent of `EMIT_EVIDENCE`: stamps are printed whether or not evidence is requested.
- **Cross-line interface:** one gated block plus one child field, final tier only, off = byte-identical.
  Agreed with the line that owns the extraction prompt before merge. Nothing else in the prompt moves.

### 4.2 Resolver (`item_locator.py`, pure, no AWS, no DB)

For each final child with a `said_at`:

1. Parse `HH:MM:SS`. Anything else → refuse, `unparseable`. Null or absent → `no_locator`.
2. Candidate turns are the turns the model saw (post-filter, the same list `verify_evidence` uses)
   whose printed `abs_start_str` equals the stamp exactly. Zero → `no_line`. More than one →
   `ambiguous_stamp` (refuse; two lines printed with the same second cannot be told apart).
3. The turn must lie inside the topic's `time_range`, widened by one minute each side because
   `time_range` has minute resolution. Outside → `outside_topic`.
4. Otherwise `linked`, with the slice 1 locator `{source_stem, start_sec, end_sec, speaker_label,
   quote_status}` and `method = rule_version = "locator_v1"`, score null.

Then the raw `said_at` is removed from the child; the `support` object carries the result. Refusals
carry their reason and no locator.

### 4.3 Combining with the lexical paths

- `PATHS` gains `"locator"`, tried **first**.
- Lexical paths run only when their constants are calibrated. Today they are `None`, so
  `attach_support` must no longer refuse to run as a whole: with `locator` enabled and lexical
  constants `None`, the locator runs alone and the lexical paths are skipped (not errors).
- If a lexical path is calibrated later and both link the same item to **different** turns, the item
  gets `status = "disagree"` and no link. Same turn → one link, method `locator_v1`.
- The item writer passes `support["method"]` to `event_links.insert_supported_by` (today it falls back
  to the default `lexical_v1`). No migration: `method` is text.

### 4.4 `claim_type` rule v2

`claim_type.derive` gains the locator statuses, rule `claim_v2`:
- `linked` by the locator: as v1. Recorder identified → `observed`; someone else → `stated`;
  unidentified → `NULL`.
- `no_locator` (the model said no single line says it): `inferred` **only if §6 bar 6 passes**.
  Otherwise `NULL`, and `inferred` is never emitted. The decision is a constant in code, fixed by the
  measurement result, not a runtime guess.
- Every refusal (`unparseable`, `no_line`, `ambiguous_stamp`, `outside_topic`, `disagree`) → `NULL`.
- The voiceprint writer's recompute and the backfill reuse `derive`, unchanged in shape.

### 4.5 Flags, in order of switching on (TEST first; prod is the owner's)

`EMIT_ITEM_LOCATOR` changes the prompt and runs the resolver. `EMIT_ITEM_SUPPORT` (slice 1) still gates
attaching and writing. Locator on with support off: the model emits `said_at`, the extractor strips it,
nothing is stored. That combination is legal, but nobody should leave it on.

## 5. Interfaces and ownership

| Party | Gets | Owns |
|---|---|---|
| Extraction prompt line | one gated block, one child field, final only | the rest of the prompt; agrees the wording before merge |
| Status line, jev TrackB | `find_items.origin` is filled for more items; no API change | unchanged |
| Architecture line (nightly agent) | `method='locator_v1'` rows next to future `nightly_*` repairs | unchanged |

## 6. Measurement (pre-registered; frozen when the first result row exists)

**Sessions.** The Task 10 manifest (digest `38e50e20…2eec2e3`, 59 sessions) and its frozen split, fit 30
and held-out 29. Same eligibility; the known duplicates stay in, as in Task 10.

**Arms.** A = today's final prompt (locator off). B = locator on. Same deployed TEST configuration for
both (`load_deployed_llm_env("fieldsight-test-extract-session")`, final-tier thinking), each run twice
(A1, A2, B1, B2). Fresh A runs, because the deployed model may differ from when the stored finals were
made.

**Staged spend.** Fit half first: 30 × 4 = **120** extraction calls. The held-out half (29 × 4 = **116**)
runs only if the fit half passes bars 1, 3 and 4. **236 is the ceiling, not a commitment.** Before
the first call, the owner gets the exact count.

**Gold.** A blind agent labeller sees each B item's text and its topic's turns, **without** `said_at`,
and labels `turn` / `several` / `none`, as in Task 10. A link is correct if gold's turn (or one of
`several`) is the linked turn, or an adjacent turn by the same speaker carrying the same claim.
Labelling uses B1 items. Owner sample: 40 B1 links from the held-out half.

**Bars.** Bars 1, 3, 4 gate going to held-out on the fit half. All bars are judged on held-out.

1. **Admission unchanged.** Per session, match items across runs by kind with `item_text_match.overlap ≥
   0.5`, one to one. The noise floor is the A1↔A2 matched share. B must stay within it: the A1↔B1 and
   A2↔B2 matched shares are each ≥ (A1↔A2 share − 5 points), and B's mean items per session lies within
   ±10% of A's.
2. **Locator follow-through.** The share of B items with a non-null `said_at` that resolve to `linked`.
   Reported, not gated. `ambiguous_stamp` and `outside_topic` are counted separately.
3. **Link precision.** Wrong links ÷ linked items: the one-sided 95% Clopper–Pearson upper bound
   ≤ 5%, which needs ≥ 59 links.
4. **Twin hard negatives.** The labeller flags any link whose turn names a different unit, level,
   block, room or direction than the item. Owner-confirmed flags must be 0.
5. **Coverage.** Linked ÷ B1 final items ≥ 60% (the owner's line), with the same denominator rules as
   Task 10.
6. **`inferred` precision** (decides §4.4). Among `no_locator` items, the share that gold says a single
   turn supports: CP upper bound ≤ 10%. With fewer than 20 such items, reported only, and `inferred`
   stays off.
7. **Stability.** On items matched B1↔B2 (bar 1's matching), both runs linked to the same turn, or to
   adjacent same-speaker turns: ≥ 90% of the items both runs linked.
8. **Agent–owner agreement** on the owner sample ≥ 85% (the continuity bar). Required before any flag
   is on, TEST included.

**Reported, not gated:** coverage by kind and by language pair (item script vs turn script); the
refusal histogram; output-token delta B vs A from the calls' usage fields; latency delta.

**Outcome.** Pass: TEST gets `EMIT_ITEM_LOCATOR=true` and `EMIT_ITEM_SUPPORT=true`; prod stays the
owner's call. Fail: both flags stay off, the result is recorded as in Task 10, and nothing is tuned
after the fact.

## 7. Display principles (owner, 2026-11-10; for later slices, not built here)

- By default no citations anywhere: lists, the Tasks page and report bodies show no markers or
  footnotes.
- On demand only: in an item's detail view, one "听原话" entry that plays from the linked line, with
  that line's transcript beside it. One entry per item, even if several lines are linked.
- No `claim_type` tag (already ruled 2026-11-08).
- Flagging items with no source line for managers is a later decision, not this slice.

## 8. Left to the builder

Module and function names; the exact prompt wording within §4.1's constraints; the harness layout
(extend `scripts/item_support_eval/` and reuse `scripts/continuity_eval`'s LLM-env loader); how bar 1's
one-to-one matching breaks ties, provided it is deterministic and written down before the first run.
