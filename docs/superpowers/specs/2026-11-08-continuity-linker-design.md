# Item continuity: link in a second call, extract without the prior list

**Date:** 2026-11-08 · **Status:** design, Fable review pending.
**Amends:** `2026-09-30-extractor-declares-item-continuity-design.md` (D3, §4, §5 "Prompt") and
`2026-10-27-action-status-suggestions-design.md` §4.1 "Where the instruction lives".
**Owner decision (2026-11-08):** keep `PROD_DECLARE_CONTINUITY` and `PROD_STATUS_SUGGESTIONS` on
while this is built.

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

The mechanism is specific to how passes are run. Live passes run without thinking; the **final
pass runs with thinking** (`enable_thinking=final`) and, fresh, finds about twice the items. With
the block, the thinking final keeps the shallow live list instead of re-extracting. The same freeze
is in the 10-01 TEST-config run (17 → 17), which passed the bar by 0.009.

Two prompt-level fixes were measured on sid6237 (prod config, 3 reps each):
- Wording ("the list is not a draft of your answer; extract the whole transcript"), block still
  after the transcript: **no effect** (shape b stayed frozen for four steps).
- Block moved before the transcript, plus that wording (PR #1257): shape b finals 36 / 38 / 42.
  Shape a still frozen (33 → 33, 25 → 25; the third chain's live step errored, so its final ran
  with no block and found 52).

So no prompt arrangement tried so far keeps the final pass's admission independent of the prior
list. Prompt text is not a guarantee anyway (memory: *prompt-instruction-present-is-not-obeyed*).

## 2. The change

**The extraction call never sees the prior list.** Its prompt is byte-identical to the flag-off
prompt on every pass, so admission cannot drift.

**A second call, the linker, states continuity.** It runs after extraction, only when there is a
prior list. It sees the prior items, the new items and the transcript, and returns, per new item,
the same `continues` claim the extractor used to emit, plus the optional `status_suggestion`. Code
writes those fields onto the children and then runs the existing pipeline **unchanged**:
`normalise_children` → `resolve` (exact pass, D4 guards, one-to-one) → `status_suggestions.filter_suggestions`
(C3, evidence check) → artifact → item writer.

The idea in the original spec (§2) is unchanged: the model states the correspondence, with a
verifiable echo, and code outside the model checks it. Only *which call* states it changes.

## 3. Decisions

- **L1. Flag.** `CONTINUITY_LINKER`, default off, wired in the three places (template parameter,
  workflow variable, function env; memory: *fieldsight-unwired-toggle-trap*). It only has an effect
  when `DECLARE_CONTINUITY` is on. Off = today's single call with the block (PR #1257 placement).
  Rollback is turning it off.
- **L2. Every pass, live and final.** Anchoring was also measured on live → live steps (shape b
  before #1257). One code path.
- **L3. Linker input.** One prompt:
  - the prior list exactly as `render_block` renders it today (aliases `A1`, `F1`, `D1`, `Q1`,
    kind, text; same fence, same scrub);
  - the new items as one JSON line each `{"ref", "kind", "text"}`, with refs `N1, N2, …` assigned
    in document order across all kinds (one sequence, so a ref can't be mistaken for a prior alias);
  - the transcript, rendered as the extraction saw it (`render_transcript`, same truncation);
  - `CONTINUITY_INSTRUCTION` minus the extraction-only sentences, plus
    `status_suggestions.INSTRUCTION` when `STATUS_SUGGESTIONS` is on (verbatim: same contract).
- **L4. Linker output.** `{"links": [{"ref": "N3", "continues": {"id": "A2", "starts": "…"},
  "status_suggestion": {…}}]}`. Refs not listed get nothing. Code attaches `continues` and
  `status_suggestion` to the child with that ref. An unknown ref, a duplicate ref, or a malformed
  entry is dropped before `resolve` and counted (`continuity.linker.dropped`). A `status_suggestion`
  on a non-action ref is dropped (as today).
- **L5. Model and mode.** The deployed extraction model, **thinking off** on every pass (a matching
  task over a short list; latency). Same caller-tag pattern: `continuity_link_live` /
  `continuity_link_final`.
- **L6. Failure never costs the extraction.** Linker call error, timeout, unparseable JSON: no
  claims, fresh ids, text fallback in the writer — the same outcome as an UNKNOWN prior today.
  The artifact records `continuity.linker = {"status": "ok" | "error", "ms": …, "dropped": n}`.
  WARNING log, counts only.
- **L7. Records unchanged.** `continuity.claims` and the item writer's decision records (D6) keep
  their shape. `question_set` hashes the linker instruction text, so records from the two
  mechanisms stay distinguishable.
- **L8. Scope unchanged.** `extract_session` only; group prompt untouched; cross-day out of scope
  (it will reuse the linker with a cross-day prior list — Track C `find_items`, precomputed to S3).

## 4. Data flow (both flags on)

```
extract_session(pass N)
  turns = gather(...); prev = read_existing_extraction(out_key); prior = prior_items(prev)
  prompt = build_extraction_prompt(..., continuity_block="")     # == flag-off prompt
  topics = LLM(prompt, thinking=final)
  normalise_children(topics)
  if prior:
      links = LLM(linker_prompt(prior, new_items(topics), transcript), thinking=False)
      attach continues / status_suggestion by ref (L4)
  claims = resolve(topics, prior) if prior else assign_fresh_ids(topics)   # unchanged
  filter_suggestions(...)                                                  # unchanged
  write extraction (+ continuity.linker)
```

## 5. Cost and latency

One extra call per pass that has a prior list; thinking off; output is small (one line per linked
item). Input is roughly the extraction's input. Measured in §6, not estimated here. The live pass
must stay inside its existing timeout budget; if the measured live p90 says it cannot, the linker
runs on the final pass only and live keeps #1257 — a decision the plan records with the number.

## 6. Measurement (pre-registered, before TEST flag-on)

Re-run `scripts/continuity_eval` on the same 13 prod sessions under prod config
(`--sessions-file prod-config-2026-11-06/sessions.json --config-env prod --status-instruction`),
with the with-block arm replaced by a linker arm; the baseline arm is the 11-06 baseline (identical
prompt, so reused). Blind agent labels, owner adjudication of claim disagreements plus a random
10%, `finalize.py`. Bars as the original §7, unchanged thresholds, plus:

| bar | threshold |
|---|---|
| admission drift | ≤ tolerance (expected equal in expectation by construction) |
| added final-pass latency (extraction + linker − baseline extraction) | p90 ≤ 20 s |
| added live-pass latency | p90 reported; decides L2 vs final-only (§5) |
| linker error rate | ≤ 2% of passes with a prior |

The harness gains a `linker` arm; `with_block` stays runnable for comparison.

## 7. What changes for other lines (interface only)

- Status suggestions: field shape, C1–C3 and the evidence filter are unchanged. The instruction
  text moves from the extraction prompt to the linker prompt. Owner of `STATUS_SUGGESTIONS`: no
  action needed; the eval in their §5 is re-run on the linker arm by this line.
- Item writer, org-api, UI: no change (artifact contract unchanged except the additive
  `continuity.linker`).

## 8. Not doing

- No change to the D4 guards, the cap (40 per kind), or the writer's carry order.
- No thinking on the linker unless §6 shows thinking-off misses claims (would be a new decision).
- No cross-day linking here.
