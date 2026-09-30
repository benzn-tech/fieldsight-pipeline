# The extractor says which item continues which

**Date:** 2026-09-30 · **Revision:** 3 (after the final review and its re-check; changes listed in §10)
**Status:** design, awaiting owner review. No code yet.
**Follows:** Track B (`plans/2026-09-24-track-b-stable-identity-and-decision-records.md`; PRs #971, #972).

## 1. Problem, with the measurement that found it

Track B gives every extracted child item — action item, finding, decision, question — a `stable_id`. When a session is re-extracted, the item writer carries that id, plus any human edit (a tick, an answered question), from the previous pass to the new one. It decides which new item is "the same" by comparing texts: an identical normalised text matches, otherwise the texts must be ≥ 0.90 similar (`difflib.SequenceMatcher`), one-to-one, with no near-tie. The floor is deliberately high because "a wrong carry-forward is worse than a lost one."

The first real live → final pair on TEST (2026-09-29, `extractions/Ben_UCPK2/2026-09-23/sid7312b54d…`) reworded both action items:

| live pass | final pass |
|---|---|
| Platform initial login using temporary password then change to own password per PDF | Platform login via temporary password at… |
| Elevation onboarding session to attend tomorrow at eleven o'clock | Onboarding meeting to be attended tomorrow… |

A person sees the same two jobs; the text matcher carried 0 of 2. The plan's own example ("Check the scaffolding before Monday" → "Scaffolding to be checked before Monday") scores 0.68. Because the model rewords by default, a tick made during recording will usually vanish from the user's view when the next pass lands. The tick is not deleted — it stays on the superseded row and is counted in `OrphanedHumanEdits` — but the user sees it disappear.

Lowering the floor does not fix this. Between 0.6 and 0.9, different items on the same topic ("book the crane for Tuesday" / "book the hoist for Tuesday") score as high as rewordings of the same item, so text similarity cannot tell the two cases apart.

## 2. The idea

The model writing the new list is the only party that knows which new item rewords which old one. So the extraction call is shown the items currently published for the session and states the correspondence itself: for each new item, "this continues A3 — which starts '<the first words of A3>'", or nothing. Code outside the model checks every claim before trusting it.

The repo has measured this principle once already. Reconciling two model-written documents after the fact (by time, by text) failed; asking the generator to state the correspondence worked 19/19 (memory: *ask-the-writer-do-not-reconcile-two-documents*). In that precedent the model copied a **verifiable verbatim string**, not a bare label. This design keeps that property with the echo in D4.

## 3. Decisions (taken by the controller on the owner's delegation, 2026-09-30)

- **D1. Two identities, each with one owner.**
  - `stable_id` stays owned by the database, exactly as Track B built it. Carry-forward moves it.
  - A new, separate `item_id` (UUID4) is the **extractor's lineage id**. `extract_session` assigns one to every child item in code, and the model never sees or writes it.
  - The item writer stores `item_id` in a new nullable `item_id uuid` column on the four child tables. Migration 0075 has no default, so there is no table rewrite (compare Track B R20).
  - Keeping the two apart is what makes the chain unbreakable. If the DB's `stable_id` doubled as the extractor's id, the first carry done by text would change the row's `stable_id` without the artifact knowing, and every later claim would point at an id no row holds (final review C1).
- **D2. Continuity is resolved in the extractor; identity is moved in the writer.**
  - When a claim passes the guards, the new item **inherits the prior item's `item_id`** before the extraction is written to S3. So the artifact records the lineage.
  - The writer pairs new rows with retired rows that have the same `item_id` (pass 0 of carry-forward), then calls Track B's existing `carry_identity`. That moves `stable_id`, `carried_from`, and the human edits exactly as today.
  - Prior items without an `item_id` (artifacts written before this change or with the flag off) are **not offered** to the model. Their successors are matched by Track B's text passes, and the next pass then has ids to offer.
- **D3. Every pass of `extract_session` does this — live and final.** Live passes re-extract every few minutes during recording, so a tick can be lost at any live → live step, not only at live → final. The prior list is the extraction currently published at the session's key.
  - Live: the published extraction is read after the gather. This is a new read, separate from the throttle's early read (don't reuse that variable).
  - Final: gains the same read, also after the gather. An `UNKNOWN` read sends no prior block — it must not skip the final pass, unlike live.
- **D4. Guards live outside the model.** A claim has the shape `"continues": {"id": "A3", "starts": "<first 3–6 words of A3, copied>"}`. It is accepted only if all of the following hold; otherwise it is dropped and the item gets a fresh `item_id`.
  1. **Existence:** the alias was in the prior list that was sent.
  2. **Kind:** an action item can only continue an action item (likewise findings, decisions, questions).
  3. **Echo, unique among siblings:** `starts`, tokenised, must be a prefix of the claimed prior item's tokens **and of no other prior item of the same kind**.
     - **Tokeniser:** Track B's normalisation (`content_hash.normalize`, punctuation dropped, CJK spacing removed); Latin runs split on whitespace; every CJK character is one token. Mixed text uses both rules.
     - **Length:** the echo must have at least `min(4, tokens in the prior item)` tokens. A two-word item ("Call electrician") can be echoed whole.
     - **Why unique:** the extraction prompt makes items lead with their subject ("the FIRST 2-4 WORDS must carry the real SUBJECT", `lambda_extract_session.py` ~1070). So siblings ("Door delivery level 3…", "Door delivery level 4…") share their opening words. An echo that is a prefix of two same-kind items identifies neither and is rejected. The prompt asks the model to copy enough words to tell the item apart.
     - This catches the shifted alias — the model meaning A4 but writing A3 while copying A4's words — which a word-overlap veto cannot (final review I1, N1).
  4. **One-to-one:** if two new items claim the same alias, both claims are dropped. A tie is a miss, not a guess (Track B's rule).
  5. **Exact beats claim:** if a prior item's normalised text exactly equals a new item's of the same kind, that new item inherits the id automatically, before claims are read.
     - The exact pass is one-to-one; duplicate texts on either side resolve nothing and fall to the claims.
     - Any other claim on that prior item is dropped, and so is the matched new item's own claim (final review I2, N6).

  Prompt-level instructions are not counted as guards. Text in the prompt can override prompt rules (memory: *user-text-in-the-instruction-region*); only these code checks count.
- **D5. The text matcher stays as the writer's fallback.** The writer's order:
  1. pass 0: same `item_id` (claims plus exact matches, already resolved in the extractor);
  2. exact content hash;
  3. fuzzy ≥ 0.90, over the retired rows still unmatched.

  The fallback serves pre-change artifacts, flag-off stacks, and items the model left unlabelled. The 0.90 floor is unchanged.
- **D6. Every claim becomes a decision record** (Track B's rule that every gated AI verdict is recorded).
  - The item writer writes them from the artifact's `continuity.claims`, since the extractor runs outside the VPC and cannot reach Aurora.
  - Column mapping:

    | column | value |
    |---|---|
    | `kind` | `item_continuity` |
    | `subject_type` | the kind of whichever row supplied `subject_stable_id`, so the visibility predicate looks in the right table (always one of `action_item`, `finding`, `decision`, `question`; final review N4) |
    | `subject_stable_id` | the DB `stable_id` the writer finds on the retired row with that `prior_item_id`; for a rejected or unresolvable claim, the new row's `stable_id` after carry-forward |
    | `object_ref` | the alias |
    | `provider` / `model` | the extraction's `llm_provider` / `llm_model` |
    | `question_set` | hash of the continuity instruction text (Track B R13) |
    | `input_key` / `input_hash` | the extraction key / `extracted_at` (a stretch of `input_hash`'s meaning — the plan records this) |
    | `output` | `{prior_item_id, new_item_id, outcome, guard}` — ids and enums only, never text |
    | `score` / `threshold` | NULL |
    | `auto_outcome` | `accepted`, or `rejected` with the failing guard in `output.guard` |

  - A record is skipped if one with the same (`kind`, `input_key`, `input_hash`, `object_ref`, `output->>'new_item_id'`) already exists, so writer re-deliveries do not duplicate. The two claims of a double claim stay two records (final review N5).
  - Records exist for the artifacts the writer actually processes. An artifact overwritten before the writer read it has no records; that is counted, not hidden (D9).
- **D7. Scope: `extract_session` only.**
  - `extract_group` reads transcripts, not member extractions, so member `item_id`s never reach its prompt. Group merge needs its own prior block and is a follow-up.
  - The nightly report ingest is also a follow-up.
  - Both keep Track B's text matcher.
  - The continuity block is a **parameter** of the prompt builder. `EXTRACTION_SCHEMA` and `_instructions_block()` are shared with the group prompt, so a test pins the group prompt byte-identical under both flag states, and `extract_group` defensively strips any `continues` it receives (final review I4).
- **D8. Behind a flag, off by default.**
  - `DECLARE_CONTINUITY` covers the prior block, the claim handling, and the `item_id`s.
  - Wired in three places: template parameter, workflow variable, function env (memory: *fieldsight-unwired-toggle-trap*).
  - With the flag off, the prompt is byte-identical to today's and no `item_id` is emitted.
  - TEST is switched on only after §7 passes. Prod is the owner's call, after a run under prod's model and temperature.
- **D9. Stale priors are recorded, not prevented.**
  - A wider pass can publish between this pass's read and its write. Then the claims point at the older list.
  - The lineage mostly survives this: the in-between pass's items carry the same `item_id`s if it also ran with continuity.
  - The artifact stores `continuity.prior_extracted_at`. At write time the extractor re-reads the published extraction (live already does this; final gains it) and sets `continuity.prior_stale` if it changed.
  - `prior_stale` and "artifact overwritten before the writer read it" are both counted during the TEST week.
  - Neither lambda can see the second one alone. The extractor logs one structured line per write (key, `extracted_at`), and the writer logs the `extracted_at` it processed. The count is the difference, computed by a Logs Insights query the plan writes down (final review N7).

## 4. Data flow (flag on)

```
extract_session(pass N)
  turns = gather(...)
  prev  = read_existing_extraction(out_key)       # new read, after the gather; UNKNOWN/None -> no block
  prior = children of prev that HAVE an item_id  -> aliases A1.. F1.. D1.. Q1..  (cap per kind, §6.6)
  prompt = build_extraction_prompt(..., continuity_block=render(prior))   # block before "## Instructions"
  result = LLM(prompt)
  normalise decisions/questions given as plain strings into dicts
  exact pass:  new child whose normalised text == a prior item's -> inherits that item_id
  claims:      pop "continues" from every child; apply D4 guards; accepted -> inherit item_id
  everyone else -> fresh uuid4 item_id
  re-read published extraction; set continuity.prior_stale
  write extraction  (children carry item_id; "continues" never reaches S3; continuity.claims recorded)

lambda_item_writer (order unchanged: supersede -> inserts -> carry-forward savepoint -> ...)
  inserts store item_id on all four child tables
  carry-forward pass 0: retired rows x new rows with equal, NON-NULL item_id (one-to-one; a duplicate id on
                        either side drops those pairs) -> carry_identity(...) as today; how='item_id'
  then Track B exact / fuzzy passes over the rest; OrphanedHumanEdits as today
  EMF: carried count per method (item_id / exact / fuzzy)
  decision_records for continuity.claims (D6)
```

## 5. Contract changes

- **Schema:** migration 0075 adds nullable `item_id uuid` to `action_items`, `findings`, `topic_decisions`, `topic_questions`. No default, no index: pass 0 reads the retired rows by topic id, as Track B already does.
- **Extraction JSON (additive):**
  - every child gains `item_id` (UUID string);
  - the extraction gains `continuity`: `{prior_count, prior_extracted_at, prior_stale, claims: [{alias, prior_item_id, new_item_id, outcome, guard}]}`;
  - no text in `claims`;
  - the plan lists every reader of the extraction JSON and confirms each ignores unknown keys (Track B R15 found only the item writer parses the full body).
- **Model output schema:** children gain optional `"continues": {"id", "starts"}`, present only when a prior block was sent. Code strips it before the write.
- **Insert paths (the plan must list and change every one):**
  - the action-item mapping that rebuilds dicts with a fixed key set (`lambda_ingest._map_action_items`, reused by the writer — the same whitelist trap as the frontend api layer);
  - the action-item insert inside `topics.upsert_topic`;
  - `findings.insert_findings`, `topic_decisions.insert_decisions`, `topic_questions.insert_questions`.

  A malformed or duplicate `item_id` within one extraction: every copy is stored as NULL, logged at WARNING, and matched by text.
- **Prompt:**
  - The prior block sits before `## Instructions`, in its own fence. Each prior item is one JSON line: `{"alias", "kind", "text"}`.
  - Fence sentinels (the transcript's `"""` and the block's own marker) are stripped from item texts, because those texts are model output derived from speech (final review I8).
  - Alias, kind and text only — no status, owner or tick. The model must not treat "already done" as a reason to keep or drop an item.
  - The instruction adds two sentences: label continuity only when the new item is the same piece of work, copying its first words; and the previous list is not a reason to include an item.
- **Text field per kind:** action item `action`, finding `observation`, decision `decision`, question `question` — the same fields Track B's carry-forward compares.

## 6. Failure modes

1. **Anchoring.** The model keeps items the transcript no longer supports, drops new ones, or moves items between kinds. Adding a block shifts admission (memory: *prompt-register-and-admission-are-coupled*), and no guard catches that. §7 measures it.
2. **Shifted alias.** Caught by the echo (D4.3); what slips through is measured as wrong carries in §7.
3. **Hallucinated alias.** Caught by existence; its rate is measured.
4. **UNKNOWN published extraction.** No block, fresh ids, text fallback, WARNING. This is today's behaviour; the pass is never skipped.
5. **Bad `item_id` reaching the writer** (hand-edited artifact, older code). NULL plus WARNING; the pass never fails.
6. **Prompt size.**
   - Cap: 40 prior items per kind. Items past the cap are not offered and fall back to text.
   - Plan task: measure prod's per-session item-count distribution (read-only counts) and report it before the flag goes on. The cap matters most in long sessions, where ticks matter most.
   - The latency budget is in §7.
7. **Stale prior.** D9.

## 7. Measurement before switching on (pre-registered)

Written before any result, per CLAUDE.md "Measure before you change a prompt, and run the same config twice".

**Harness**
- Uses the real `build_extraction_prompt` and the deployed model and settings.
- **Void condition:** any with-block run whose prompt lacks the block, or whose `prior_count` is 0, is void.
- Outputs stay gitignored; only counts are committed.

**Shapes**
- **(a) live → final:** a live extraction on about 95% of a session's segments, then final on 100%. This is production's shape.
- **(b) chained live → live → final:** 40% → 60% → 80% → 100%, each pass fed the previous one's output. This is what D3 is for.
- **(c) optional stress case:** a 60% prefix → final.

**Runs**
- Each shape is run **3 times per arm**: baseline (no block) and with-block (memory: *one-before-after-diff-proves-nothing*).

**Sessions**
- At least 15 TEST sessions.
- **Hard-negative group:** pairs of same-topic, same-kind items that gold says are different work. It needs at least 30 such pairs; with fewer, the result is "insufficient", not "pass".
- **Prod sessions may be used (owner, 2026-09-30).** TEST's mostly short owner recordings are unlikely to supply 30 hard negatives. Conditions:
  - read-only S3 GET of prod transcripts and extractions; no prod database access and no prod lambda triggered;
  - the eval's extraction calls use the same model provider prod already uses, so no new third party receives the text;
  - outputs and labels stay local and gitignored; only counts are committed.
- **Who labels prod sessions** is an open owner decision: the blind agent annotator (a Claude agent reading customer conversation text — a new place that data goes) or the owner. The harness takes this as a setting (`prod_labeller = agent | owner`). TEST sessions are agent-labelled either way. The owner has accepted the adjudication workload below.

**Gold**
- Labelling is an **assignment per (prior list, new list)**, not per pair: for each new item, pick its counterpart in the prior list, or "none". "None" is the default.
- **Expected volume:** 15 sessions × 4 pass steps (1 in shape a, 3 in shape b) × 6 runs × about 10 items ≈ 3,600 assignment decisions. Too many for one person, so:
  - a blind **agent annotator** labels everything (memory: *second-annotator-gives-the-ceiling*). It never sees the model's claims or the extraction prompt's continuity block.
  - the **owner** adjudicates every accepted claim the agent calls "different", plus a random 10% of all assignments. Agent–owner agreement on that sample is reported; below 90%, the owner labels another 10% and the agent's labels are not used alone.
- Labelling is done blind to the model's claims (final review N3).
- Pre-registered scoring:
  - a claim onto an item that **merged** the prior item with another counts as correct;
  - a claim onto one half of a **split** counts as correct;
  - anything else gold calls different is wrong.
- The same labelling pass judges item-set overlap for the admission check. Text matching cannot judge overlap, because rewording is the problem being solved.

**Metrics and bars**

| metric | definition | bar |
|---|---|---|
| wrong carries | accepted claims that gold says are different work, over **accepted claims** | **0 overall, and 0 within the hard-negative group**; reported per session, since items within a session are correlated, so no "<5%" rate is claimed |
| carry recall | gold-positive pairs carried by item_id + exact + fuzzy, vs **text-only recall on the same pairs** | ≥ 80% |
| admission drift | per session and kind: the mean \|count difference\| over the 9 with-block × baseline run pairs, averaged over sessions | ≤ the **largest** within-arm pairwise difference, averaged the same way over both arms' 3+3 pairs. A tolerance, not a "must beat the noise" test: with no real effect the two quantities are equal in expectation (final review N2) |
| transcript support | share of **block-only** items (with-block, absent from all baselines) the gold pass judges unsupported by the transcript, vs the share of **baseline-only** items judged unsupported | block-only unsupported share ≤ baseline-only unsupported share + 5 percentage points; both counts reported |
| added final-pass latency | p90 over at least 30 final passes | ≤ 20 s, which keeps the stop-email budget (memory: *email-generation-3min-budget*) |

**Also reported, not gated**
- how often the model labels a gold-positive pair at all;
- claim precision before the guards;
- false rejects per guard (existence, kind, echo, one-to-one, exact-beats-claim);
- hallucinated-alias rate;
- how often the per-kind cap is hit;
- added prompt tokens.

**Decision rule**
- All bars pass: switch on for TEST, watch the per-method EMF counts, `OrphanedHumanEdits`, `prior_stale` and the `item_continuity` records for a week, then ask the owner about prod, with a prod-config run first.
- Any bar fails: the flag stays off, the numbers are committed, and the spec is revisited. The fuzzy floor is not lowered as a workaround.

## 8. Testing (implementation)

- **Unit:**
  - alias assignment and cap;
  - every guard (existence, kind, echo for Latin and CJK, one-to-one, exact-beats-claim);
  - `continues` is stripped;
  - `item_id` on every child, including string-form decisions and questions;
  - flag off gives a byte-identical prompt and no `item_id`s;
  - the group prompt is byte-identical under both flag states, and `extract_group` strips `continues`;
  - fence sentinels in a prior text are neutralised.
- **Seam (real Postgres):**
  - `extract_session`'s output, with the LLM stubbed at the boundary only, goes through the real mapping and insert paths into the real `write_extraction_items`;
  - a ticked item survives a reworded next pass that the model claims correctly;
  - a claim failing the echo does NOT carry the tick;
  - a chain of three passes, with one text-carried step in the middle, still carries on the third pass. This is the C1 regression.
- **Integration:**
  - `item_id` round-trips on all four tables;
  - pass 0 pairing, with duplicate ids dropped;
  - `decision_records` for accepted and rejected claims, with the dedupe on re-delivery.
- **Live on TEST after switch-on:**
  - a **fresh** recording (the owner records it; a re-driven live pass on an already-final session stands down and only tests final → final);
  - the owner ticks one item in the app during recording;
  - check after final: same `stable_id`, still ticked, a declared carry in the EMF counts, and an accepted `item_continuity` record.

## 9. Out of scope

- `extract_group` and ingest continuity (D7).
- Any change to the 0.90 fuzzy floor.
- A user-facing "this tick could not be matched" list. The data already exists: superseded rows plus `OrphanedHumanEdits`.
- Continuity for topics: topics are containers and are not re-keyed (Track B decision).

## 10. Revision 2 — what the final review changed

- **C1:** a separate extractor lineage id (`item_id` column, migration 0075) instead of reusing `stable_id`. Pass 0 pairs rows on `item_id` and then moves `stable_id` through `carry_identity`. D6 resolves `subject_stable_id` from the DB.
- **I1:** the claim carries a copied echo of the prior text. This replaces the word-overlap veto, which sibling items defeat.
- **I2:** exact matches are resolved before claims and win over them.
- **I3:** every insert and mapping path is named; string-form decisions and questions are normalised to dicts.
- **I4:** the block is a builder parameter; the group prompt is pinned byte-identical; `extract_group` strips `continues`.
- **I5 / D9:** `prior_extracted_at` and `prior_stale`, a re-read at write time, dedupe of records, and D6 scoped to processed artifacts.
- **I6:** every NOT NULL column of the decision record is specified, with ids and no text.
- **I7:** §7 rewritten:
  - precision over accepted claims, and a hard-negative group with a minimum size;
  - 3 runs per arm and production-shaped evals (95% prefix, chained passes);
  - gold labels every pair blindly, with merge and split scoring defined;
  - a transcript-support check for block-only items;
  - the missing metrics added;
  - a void condition, a prod-config gate, and a numeric latency bar.
- **I8:** each prior item is a JSON line in its own fence, with sentinels stripped, placed before the instructions.
- **I9:** the live test uses a fresh recording, and the owner ticks.
- **Re-check of revision 2 (same reviewer):**
  - N1: the echo must be a prefix of exactly one same-kind prior item, with a defined tokeniser and a `min(4, n)` length.
  - N2: the admission bar is a pre-registered tolerance, and transcript support is compared against baseline-only items.
  - N3: gold is an assignment per list pair, labelled by a blind agent and adjudicated by the owner, with the volume stated.
  - N4: `subject_type` follows the row that supplied `subject_stable_id`.
  - N5: the dedupe key includes `new_item_id`.
  - N6: pass 0 excludes NULLs; the exact pass is one-to-one and same-kind, and drops the matched item's own claim.
  - N7: the overwritten-artifact count comes from paired log lines.
- **Minors:**
  - M1: the cap is per kind, with a prod distribution measured.
  - M2: D7's reason corrected.
  - M3: per-method EMF.
  - M4: the final pass reads after the gather, and UNKNOWN does not skip it.
  - M5: text field per kind named.
  - M6: duplicate-id rules on both sides.
  - M7: D3 reworded.
