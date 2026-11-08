# Item continuity: link in a second call, extract without the prior list

**Date:** 2026-11-08 · **Revision:** 2 (after Fable review, 2026-11-08: 1 critical / 7 important, folded in; §9)
**Amends:** `2026-09-30-extractor-declares-item-continuity-design.md` (D3, §4, §5 "Prompt") and
`2026-10-27-action-status-suggestions-design.md` §4.1 "Where the instruction lives", final pass only.
**Owner decision (2026-11-08):** keep `PROD_DECLARE_CONTINUITY` and `PROD_STATUS_SUGGESTIONS` on
while this is built. Review questions the owner delegated were decided by the controller (§9).

## 1. Problem, with the measurement that found it

The pre-registered prod-config measurement (11-08; 13 prod sessions, 540 steps, 1768 blind agent
labels) passed every bar except **admission drift: 0.743 vs tolerance 0.616**. Wrong carries
0/209, hard-negative wrong carries 0/534, carry recall 0.988.

The failure is the anchoring the original spec named as failure mode 1. On the one long session in
the sample (sid6237, 28 segments), passes that see the prior block re-emit it:

| sid6237 final pass, total items | with block (11-06) | no block |
|---|---|---|
| shape a (0.95 → 1.0) | 25 / 32 / 19 (each equal to its live step's count) | 55 / 51 / 60 |
| shape b (0.4 → 1.0) | 30 / 19 / 31 | 39 / 48 (third chain errored) |

The items lost are real: 98.9% of baseline-only items are supported by the transcript.

Live passes run without thinking; the **final pass runs with thinking** (`enable_thinking=final`)
and, fresh, finds about twice the items. With the block, the thinking final keeps the shallow live
list instead of re-extracting. The same freeze is in the 10-01 TEST-config run (17 → 17), which
passed the bar by 0.009.

Two prompt-level fixes were measured on sid6237 (prod config, 3 reps each):
- Wording ("the list is not a draft of your answer; extract the whole transcript"), block still
  after the transcript: **no effect**.
- Block moved before the transcript, plus that wording (PR #1257, merged to develop): live → live
  growth restored (shape b finals 36 / 38 / 42); **the thinking final still freezes** (shape a
  33 → 33, 25 → 25).

## 2. The change

**The final pass's extraction call never sees the prior list.** Its prompt is byte-identical to the
flag-off prompt, so the final's admission cannot depend on the live list.

**A second call, the linker, states continuity for the final pass.** It runs after the final
extraction, only when there is a prior list, and returns per new item the same `continues` claim
the extractor used to emit, plus the optional `status_suggestion`. Code writes those fields onto the
children and runs the existing pipeline **unchanged**: `resolve` (exact pass, D4 guards,
one-to-one) → `status_suggestions.filter_suggestions` (C3, evidence check) → artifact → item writer.

Live passes keep today's single call with the block before the transcript (#1257). Reason: the
measured harm of #1257's remaining defect is on the final; a live linker would double input tokens
on every live pass. Live may move to the linker later on its own measurement (§6 reports nothing
about live; a later decision).

Review confirmed every D4 guard in `item_continuity.resolve` is a pure check on (claim, prior list,
child text) and the C3 checks key on `item_id` after `resolve`; neither depends on which call
emitted the claim.

## 3. Decisions

- **L1. Flag.** `CONTINUITY_LINKER`, default off, wired in three places (template parameter,
  workflow variable, function env; memory: *fieldsight-unwired-toggle-trap*). Effective only when
  `DECLARE_CONTINUITY` is on and `final=True`. Off = today. Rollback = off.
- **L2. Final pass only** (above).
- **L3. Linker prompt**, parts in this order:
  1. `LINKER_INSTRUCTION` (a module constant in `item_continuity`, text fixed by the plan before any
     record is written; `LINKER_QUESTION_SET` = `"item_continuity_linker:" + sha256[:16]` of it);
     plus `status_suggestions.INSTRUCTION` when `STATUS_SUGGESTIONS` is on. That instruction's
     phrase "an action item that continues an earlier item" is reworded by the plan to "a new item
     you link to an earlier item" in a linker-specific variant, pinned by a test;
  2. the prior list exactly as `render_block` renders it (aliases, kind, text, fence, scrub);
  3. the new items, in their own fence, one JSON line each `{"ref", "kind", "text"}`;
  4. the transcript — **the identical text the extraction saw**: `build_extraction_prompt` returns
     its rendered `transcript_text` (additive return field), rendered with the same
     `speaker_names`, and the linker reuses it.
- **L4. Refs.** Assigned after `normalise_children`, in `item_continuity._children` order
  (topic-major, then `KINDS` order, dict children only), `N1, N2, …` across all kinds. Kept as a
  side list `ref → child object`; never written onto the child. Children already matched by the
  exact pass are still listed (the linker may still attach a status suggestion to them; their
  `continues` claim is then rejected by `exact_matched_item` as today). All of this runs inside the
  existing `try` at the continuity step, so a malformed child list costs only continuity.
- **L5. Linker output.** `{"links": [{"ref", "continues": {"id", "starts"}, "status_suggestion"}]}`.
  Unknown ref, duplicate ref, or malformed entry → dropped before `resolve`, counted in
  `continuity.linker.dropped`. `status_suggestion` on a non-action child → dropped (as today).
- **L6. Model, mode, budget.** Deployed extraction model, **thinking off**, caller tag
  `continuity_link_final`, `max_tokens = LINKER_MAX_TOKENS` (2000; output is one short line per
  linked item). **Deadline guard:** `lambda_handler` passes `context.get_remaining_time_in_millis()`
  into `extract_session` (new keyword, default None = no cap); the linker calls `call_llm(...,
  deadline=min(remaining_s − 60, LINKER_CAP_S))` with `LINKER_CAP_S = 120`. If less than 30 s would
  remain, the linker is skipped (`status: "skipped_budget"`). The linker can therefore never cause
  the runtime kill that re-runs the paid extraction (`Timeout: 900`, chain budget 840 per call).
- **L7. Linker failure never costs the extraction.** Error, timeout, skip, or unparseable JSON: no
  model claims. `resolve(topics, prior)` still runs, so exact-text matches still inherit ids; all
  other children get fresh ids and the writer's text fallback. A linker failure does **not** set
  `continuity_error` (that remains "the topics did not fit the shape"); suggestions then simply
  have none to filter. Artifact: `continuity.linker = {"status": "ok"|"error"|"skipped_budget",
  "ms", "dropped", "model"}`. WARNING log, counts only.
- **L8. Records.** `continuity.claims` and the decision records (D6) keep their shape;
  `question_set` is `LINKER_QUESTION_SET` on linker passes, so the two mechanisms stay apart.
- **L9. Scope unchanged.** `extract_session` only; group prompt untouched; cross-day out of scope
  (it will reuse the linker with a cross-day prior list — Track C `find_items`, precomputed to S3).

## 4. Data flow (final pass, all three flags on)

```
extract_session(..., final=True, remaining_ms=…)
  turns = gather(...); prev = read_existing_extraction(out_key); prior = prior_items(prev)
  prompt, stats, transcript_text = build_extraction_prompt(..., continuity_block="")   # == flag-off prompt
  topics = LLM(prompt, thinking=True)
  try:
      normalise_children(topics)
      if prior:
          refs = assign_refs(topics)                                     # L4
          links = LLM(linker_prompt(prior, refs, transcript_text), thinking=False, deadline=…)   # L6
          attach continues / status_suggestion by ref                    # L5
      claims = resolve(topics, prior) if prior else assign_fresh_ids(topics)   # unchanged
  filter_suggestions(...)                                                # unchanged
  write extraction (+ continuity.linker)
```

## 5. Cost and latency

One extra non-thinking call per final pass that has a prior list; input ≈ the extraction's input
plus two short lists; output small. §6 measures linker p90 latency and input tokens per call and
reports a per-month estimate from prod's final-pass count.

## 6. Measurement (pre-registered, before TEST flag-on)

One new run of `scripts/continuity_eval` on the same 13 prod sessions under prod config
(`--sessions-file prod-config-2026-11-06/sessions.json --config-env prod --status-instruction`),
arms **baseline** (re-run now, not reused, so both arms share the same day's served model) and
**linker** (live steps exactly as deployed — block before the transcript; final step extraction
with no block + linker). The harness records `info["model"]` / served model per step. Blind agent
labels, owner adjudication of claim disagreements plus a random 10%, `finalize.py`.

The linker arm's void rule: a step is void if its prior list is empty or the linker call did not
return `ok`. A harness assertion: the linker arm's final extraction prompt is byte-equal to the
baseline arm's prompt for the same step (this, not the drift bar, is the "by construction" check).

Bars — the original §7 bars with unchanged thresholds, plus:

| bar | threshold | note |
|---|---|---|
| wrong carries | 0 | now a non-thinking call matching after the fact; the bar to watch |
| carry recall | ≥ 0.80 | same |
| admission drift | ≤ tolerance | a fail means a harness or vendor problem (prompts are byte-equal), not the design; rerun before concluding |
| status suggestions `done` precision | ≥ 0.9 with ≥ 30 labelled positives, else "insufficient" | the 10-27 §5 gate, re-run on the linker arm |
| linker latency, final passes | p90 ≤ 20 s | the linker call timed alone |
| linker status ≠ ok | ≤ 2% of final passes with a prior | |
| tokens | reported | input/output per linker call + monthly estimate |

**Gates.** TEST flag-on of `CONTINUITY_LINKER` needs every bar except the status-suggestion bar
(TEST is where status-suggestion positives accumulate). Proposing it for prod needs all bars,
including `done` precision, because `PROD_STATUS_SUGGESTIONS` is on.

## 7. What changes for other lines (interface only)

- Status suggestions: field shape, C1–C3 and the evidence filter unchanged. On final passes with
  the linker on, the instruction is in the linker prompt (reworded per L3). No action for the
  owner of `STATUS_SUGGESTIONS`; this line re-runs its §5 gate.
- Item writer, org-api, UI: no change (artifact contract unchanged except additive `continuity.linker`).

## 8. Not doing

- No linker on live passes (later decision on its own measurement).
- No change to the D4 guards, the cap (40 per kind), or the writer's carry order.
- No thinking on the linker unless §6 shows thinking-off misses claims (a new decision).

## 9. Review log (Fable, 2026-11-08)

- C1 Lambda budget → L6 deadline guard + skip.
- I1 drift bar tautological → byte-equal prompt assertion; drift fail means rerun; served model recorded.
- I2 regime change → wrong carries / recall named as the bars to watch; `done` precision gate added.
- I3 latency not comparable → linker timed alone.
- I4 error gating → L7.
- I5 cost/ordering on live → final-only (L2); tokens reported.
- I6 transcript identity → L3.4.
- Minor: refs (L4), linker void rule (§6), missing constants (L3.1, L6).
- Owner questions, decided by the controller under the owner's standing delegation: (1) final-only
  by default; (2) `done` precision gates prod, not TEST; (3) baseline re-run, not reused.
