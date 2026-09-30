# The extractor says which item continues which

**Date:** 2026-09-30
**Status:** design, awaiting owner review. No code yet.
**Follows:** Track B (`plans/2026-09-24-track-b-stable-identity-and-decision-records.md`, merged as PR #971 / #972).

## 1. Problem, with the measurement that found it

Track B gave every extracted child item (action item, finding, decision, question) a `stable_id` and carries it and any human edit (a tick, an answered question) from the previous pass to the new one. The carrying is done after the fact, in the item writer, by comparing texts. The rule is: the same normalised text is a match; otherwise the texts must be at least 0.90 similar (`difflib.SequenceMatcher`), one-to-one, with no near-tie. That floor is deliberately high because "a wrong carry-forward is worse than a lost one".

On the first real live → final pair on TEST (2026-09-29, `extractions/Ben_UCPK2/2026-09-23/sid7312b54d…`), the final pass reworded both action items:

| live pass | final pass |
|---|---|
| Platform initial login using temporary password then change to own password per PDF | Platform login via temporary password at… |
| Elevation onboarding session to attend tomorrow at eleven o'clock | Onboarding meeting to be attended tomorrow… |

A person sees the same two jobs. The text matcher carried 0 of 2. The plan's own example ("Check the scaffolding before Monday" → "Scaffolding to be checked before Monday") scores 0.68. The model rewords by default, so the text matcher will rarely carry anything. In practice, a tick made during recording will usually vanish when the final pass lands. It is not deleted: it stays on the superseded row and is counted in `OrphanedHumanEdits`. But the user sees their tick disappear.

Lowering the floor is not the answer. Between 0.6 and 0.9, genuinely different items on the same topic ("book the crane for Tuesday" vs "book the hoist for Tuesday") score as high as rewordings of the same item. Text similarity cannot tell these apart at any threshold.

## 2. The idea

The model that writes the new list is the only party that knows which new item is a rewording of which old one. So the extraction call is shown the items currently published for the session, and it states the correspondence itself: each new item either says "this continues item A3" or says nothing. Code outside the model checks every claim before it is trusted.

This repo has already measured this principle. When two model-written documents had to be reconciled, reconciling them afterwards (by time or by text) failed. Asking the generator to state the correspondence worked 19/19. Memory: *ask-the-writer-do-not-reconcile-two-documents*.

## 3. Decisions (taken by the controller on the owner's delegation, 2026-09-30)

- **D1. Identity is assigned by code, not by the model.** After parsing, `extract_session` gives every child item a code-generated `item_id` (UUID4). The item writer inserts that UUID as the row's `stable_id`. The model never sees or writes a UUID. In the prompt, prior items carry short aliases (`A1`, `F2`, `D1`, `Q3`), and code maps alias → UUID. Long identifiers in a prompt get copied wrongly; short aliases scoped to one call do not.
- **D2. Continuity is resolved in the extractor, not the writer.** When the model says a new item continues `A3`, code sets that new item's `item_id` to A3's UUID before the extraction is written to S3. The writer inserts rows with the stable_ids they already carry. It then only has to copy human edits between the two rows that share a stable_id (the old row is superseded, the new one is live). This keeps the writer's job mechanical and makes the S3 artifact the record of what was claimed.
- **D3. Every pass of `extract_session` does this, live and final.** A tick can be made at any point during recording, and live passes re-extract every few minutes, so each live → live step loses ticks exactly as live → final does. The prior list is whatever is currently published at the session's key (`read_existing_extraction`, already used by the live throttle).
- **D4. Guards live outside the model.** A claim is accepted only if all of the following hold. Otherwise it is dropped, and the item keeps a fresh id (it is then just a new item).
  - the alias exists in the prior list that was sent;
  - it names the same kind (an action item can only continue an action item);
  - no other new item claims the same alias (if two do, both claims are dropped — a tie is a miss, not a guess, same rule as Track B);
  - it passes a plausibility veto on the normalised texts (§6.3). The veto catches a claim that is plainly about something else, such as a hallucinated or shifted alias. It is not a similarity threshold for accepting a match.
  - **Prompt-level guards do not count as guards.** A prompt-level guard can be bypassed by the prompt (memory: *user-text-in-the-instruction-region*). Only the code checks above count.
- **D5. The text matcher stays as the fallback, not the primary.** The order in the writer is:
  1. rows already sharing a stable_id (the model's claim was accepted);
  2. exact content-hash;
  3. fuzzy ≥ 0.90.

  The fallback still serves extractions written before this change, flag-off stacks, and items the model left unlabelled.
- **D6. Every claim becomes a decision record.** One `decision_records` row per claim:
  - `kind='item_continuity'`, `provider`/`model` = the extraction's LLM;
  - `subject_stable_id` = the prior item's stable_id when the alias exists, else the new item's own `item_id`; `object_ref` = the claimed alias;
  - written by the item writer from the extraction's `continuity.claims` list, because the extractor runs outside the VPC and cannot reach Aurora;
  - `auto_outcome` = `accepted`, or `rejected` with the failing guard recorded in `output`.

  This is Track B's rule ("every gated AI verdict is recorded"), and it makes the eval in §7 repeatable on real traffic.
- **D7. Scope.** Covers `extract_session` (live and final tiers).

  Out of scope for this change, with the Track B text matcher staying in place:
  - `extract_group` (group merge of several devices' sessions);
  - the nightly report ingest.

  Both are follow-ups once §7's numbers exist. Group merge is the natural next step, because its member extractions will already carry item_ids.
- **D8. Behind a flag, off by default.** `DECLARE_CONTINUITY` covers both the prompt block and the id assignment. It is wired in three parts: template parameter, workflow variable, and function env (memory: *fieldsight-unwired-toggle-trap*). TEST is switched on only after §7 passes. Prod stays the owner's call.

## 4. Data flow (flag on)

```
extract_session(pass N)
  prev = read_existing_extraction(out_key)            # already published pass N-1, or None
  prior = [(alias, kind, text, item_id) for every child in prev]   # A1.., F1.., D1.., Q1..
  prompt = today's prompt + PRIOR ITEMS block (data region, delimited)   # only if prior
  result = LLM(prompt)                                 # children may carry "continues": "A3"
  for each child in result:
      claim = child.pop("continues", None)
      if claim passes D4 guards: child.item_id = prior[claim].item_id ; record accepted
      else: child.item_id = uuid4() ; record rejected (if a claim was made)
  write extraction (every child now has item_id)       # S3 artifact = the claim record

lambda_item_writer (unchanged order: supersede -> insert -> carry-forward savepoint)
  insert children with stable_id = item_id (validated UUID, unique within the extraction; else DB default)
  carry-forward: pass 0 = rows of this pass whose stable_id equals a retired row's stable_id
                 -> copy human edits (same columns Track B copies), carried_from = old id, how='declared'
                 then Track B's exact / fuzzy passes over what is left
  OrphanedHumanEdits unchanged (it now counts what neither the model nor the text could carry)
  decision_records for the claims (from the extraction's claim list)
```

## 5. Contract changes

- **Extraction JSON (additive):**
  - every child gains `item_id` (UUID string);
  - the extraction gains `continuity`: `{"prior_count": n, "claims": [{"alias", "new_path", "outcome", "reason"}]}` for the decision records and for audit;
  - readers that ignore unknown keys are unaffected. The implementation plan lists every reader of the extraction JSON (Track B R15 did this once: only the item writer parses the full body).
- **Model output schema:** children gain an optional `"continues": "<alias> or null"`. It appears only when a PRIOR ITEMS block was sent. It is stripped by code before the extraction is written, so the S3 artifact never carries a raw model claim as if it were fact.
- **Writer:** accepts `item_id` → `stable_id`. It rejects non-UUIDs and duplicates within one extraction (duplicates fall back to the DB default and are logged at WARNING). No schema migration: `stable_id` already exists on all four tables (migration 0073) with a default.
- **Prompt:**
  - the PRIOR ITEMS block sits in the data region, fenced and labelled as data. Its items are model output derived from user speech, and the repo has measured that text placed in the instruction region can override house rules;
  - the block carries alias, kind and text only — no status, no owner, no tick. That keeps the model from treating "already done" as a reason to keep or drop an item;
  - the instruction adds one sentence: continuity is labelled only when the new item is the same piece of work, and the previous list is not a reason to include an item.

## 6. Failure modes and how each is caught

1. **The model anchors on the prior list** and keeps items the transcript no longer supports, or drops new ones. Adding a block to the prompt is exactly the kind of change that shifts admission (memory: *prompt-register-and-admission-are-coupled*). This is caught by §7's admission comparison against a noise floor, not by a guard.
2. **Shifted or hallucinated alias.** The model claims A3 when it means A4, or claims an alias that was never sent. Existence and kind checks catch the second. The plausibility veto and the one-to-one rule catch most of the first. What remains is measured as the wrong-carry rate in §7.
3. **Plausibility veto.** Reject a claim when the normalised texts share no content token of four or more letters AND their SequenceMatcher ratio is below a floor.

   The floor is set from §7's gold set before switching on: the highest value at which no gold-correct pair is vetoed. It is written into the flag's rollout note. Normalisation is the same as Track B's (`content_hash.normalize` plus CJK spacing), and for CJK text a shared character bigram counts as a shared token.
4. **The published extraction cannot be read** (UNKNOWN). Send no prior block. Every item gets a fresh id and the text fallback applies. Log at WARNING: this is today's behaviour, not a failure.
5. **Duplicate or malformed `item_id` reaching the writer** (hand-edited artifact, older code). Fall back to the DB default and log at WARNING. Never fail the pass.
6. **Prompt size.** The prior list is small (tens of items). Its size is measured in §7 and capped: past a cap (proposed 80 items), the block is not sent and the fallback applies.

## 7. Measurement before switching on (pre-registered)

Written before any result, per CLAUDE.md "Measure before you change a prompt, and run the same config twice".

- **Eval set.** TEST sessions with transcripts, at least 15 sessions. For each session, run a live-mode extraction on roughly the first 60% of its transcript segments. Then run final-mode on all segments, in four configurations:
  - baseline (no block), twice;
  - with the block, twice.

  Every run uses the deployed model and settings. Outputs stay gitignored; only counts are committed.
- **Gold labels.** For each session, the owner — or a blind second annotator who never sees the model's claims (memory: *second-annotator-gives-the-ceiling*) — pairs live items with final items: "same piece of work" or "not".
- **Primary metric: wrong carries.** A wrong carry is an accepted claim that gold says is a different item.
  - **Bar:** 0 wrong carries in at least 60 gold-positive pairs across both with-block runs.
  - 0/60 bounds the true rate below about 5% (rule of three). The spec says so plainly rather than calling it "zero".
- **Carry recall.** Share of gold-positive pairs carried, by exact + claim + fuzzy combined. Compare with the text-only baseline, whose current evidence is 0/2.
  - **Bar:** at least 80%.
- **Admission drift.**
  - Measure the item count per session, and item-set overlap against the baseline, for the two with-block runs.
  - The difference from baseline must sit inside the spread between the two baseline runs (the noise floor).
  - If it does not, the block changes WHAT is extracted, and the change does not ship as designed.
- **Cost.** Median added prompt tokens and latency per pass. This is recorded, not gated, unless latency pushes the final pass past the stop-email budget (memory: *email-generation-3min-budget*).
- **Decision rule.** All three bars pass → switch on for TEST, watch `OrphanedHumanEdits` and the `item_continuity` records for a week, then ask the owner about prod. Any bar fails → the flag stays off, the numbers are committed, and the spec is revisited. The fuzzy floor is not lowered as a workaround.

## 8. Testing (implementation)

- **Unit.** Alias assignment and mapping; each guard (missing alias, wrong kind, double claim, veto); stripping of `continues`; item_id assignment on every child; flag off = byte-identical prompt and no item_ids.
- **Seam.** Drive `extract_session` output straight into the real `write_extraction_items` against real Postgres. A ticked live item must survive a reworded final item that the model (stubbed at the LLM boundary only) labels as a continuation. The same test must also show that a claim the veto rejects does NOT carry the tick.
- **Integration.** `decision_records` rows for accepted and rejected claims. Writer handling of duplicate or malformed item_ids.
- **Live on TEST after switch-on.** Re-run the 2026-09-23 session from Task 8, with one item ticked between passes.

## 9. Out of scope

- `extract_group` and ingest continuity (D7).
- Any change to the 0.90 fuzzy floor.
- Showing the user a "we could not match this tick" list (it exists as data: superseded rows plus `OrphanedHumanEdits`).
- Continuity for topics themselves: topics are containers and are not re-keyed (Track B decision).
