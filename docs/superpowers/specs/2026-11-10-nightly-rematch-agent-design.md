# Nightly re-match agent: what the per-session hooks miss, repaired overnight

Status: design, rev 2 (2026-11-10). Architecture research line. Not built, no flag, no migration.
Rev 2 folds in a Fable builder check of rev 1 (8 questions, 6 grounding errors; §12). Each answer
is recorded as my call under the owner's standing delegation.
Owner ruling this builds on: Track C spec `2026-11-08-track-c-item-history-and-evidence-chain-design.md`
§9.4 (relayed by jev Track C, 2026-11-08). The nightly re-match agent is the site decision loop's
first model-backed scheduled-scan handler, with its own design, owned by this line. Its constraints:
- silent writes only in the store-only layer (`event_links` repairs and `claim_type`), tagged
  `method='nightly_*'`, reversible;
- anything user-visible or state-changing (status, `continues`, `resolves`) is a proposal for a
  person;
- each judgement kind has its own `decision_records` kind and bars.
Parent design: `2026-11-05-site-decision-loop-design.md` rev 5 (hub, runs, brakes, cards).
Baseline: origin/develop 11b13981 (2026-11-10).

## 1. What changed between the ruling and this design

When the owner ruled (2026-11-08), "re-match the day's items against site history with a model"
was one idea. Two days later most of it has an owner and a design, and both run **per session,
right after its final extraction**, not at night:

| Work | Owner line | Design | Trigger |
|---|---|---|---|
| Today's item continues an earlier item (`continues` edge) | jev TrackB | `2026-11-09-cross-day-continuity-design.md` | each final, 28-day window |
| A later recording reports an earlier item's status (suggestion / pick card) | status line | `2026-11-09-cross-session-status-reconcile-design.md` | each final, R0 = same recorder same day |
| Item → transcript turn (`supported_by`) by word overlap | Track C | Track C §5.3 | each final, flag off |

The owner's 2026-11-08 split also says `continues` edges are written **only** by TrackB and status
suggestions only by the status line. A nightly agent that re-implements either one would be a
second matcher with different guards, writing into another line's table. **That is ruled out.**

Two facts measured since then define what is actually left for a nightly pass:
1. **Per-session hooks look backwards once, at write time.** Reconcile §8 counts an *ordering
   miss* ("the assigning session's final landed after the reporting session's final, so it was
   never re-examined"). An offline device that uploads Monday's recording on Wednesday is never
   looked at again by the sessions that came before it in upload order.
2. **Track C's word-overlap linker failed its pre-registered bars** (Task 10, 2026-11-09, plan
   lines 429-430). On the fit half (30 sessions, 306 items) the best point linked 79 items (about
   26%, against the owner's 60% line) with 1 wrong. No grid point met the `inferred` constraint:
   at least 32 items stayed "nothing close" while the blind gold found a supporting turn for every
   one, and at the 0.10 floor all 32 were English item text over Chinese transcripts. The held-out
   half was never scored. `EMIT_ITEM_SUPPORT` stays off, so prod has **no** `supported_by` links
   and no `observed` / `stated` `claim_type`. How much of the other ~227 unlinked items is a
   language problem, and how much is ties or weak overlap, is not known; J2's measurement reports
   it (§7).

## 2. Goal

Each night, per site, after the site's working day:
1. **Catch-up (J1).** Find sessions a later-uploaded recording made stale, and send them back
   through **those lines' own request paths**. Nightly builds no prompt and writes no `continues`
   or status row.
2. **Evidence repair (J2, model).** For recent final items with no live `supported_by` link, ask a
   model which numbered transcript turn the item came from. Code checks the answer, and only then
   writes a `supported_by` link silently (`method='nightly_turn_pick_v1'`).
3. **`claim_type` recompute (J3).** Re-derive `claim_type` for sessions whose links changed tonight,
   or whose speaker names landed but whose recompute timed out or failed.
4. **Escalation facts (J4).** A small set of facts only visible across days become `site_events`
   for the decision hub, which decides whether a card is warranted. Nightly never writes a card.

### 2.1 Non-goals
- A third matcher for continuity or status (§1).
- Writing `continues`, `resolves`, `supersedes`, any item status, any item text.
- Replacing a link that some other method wrote. Nightly only fills gaps and only removes its own
  rows.
- A whole-history backfill as a nightly job (§3 J2 scope).
- Re-emitting requests that failed for reasons other than upload order (§3 J1, deferred).
- Cross-site or cross-company anything; live or group extractions.
- Per-site time zones beyond NZ in v1 (§4.1 keeps the seam).

### 2.2 Anti-goals: a failure even if it "works"
- A `supported_by` link to the wrong words. It makes the evidence chain lie, which is worse than no
  chain (Track C §3). Precision bars come first; coverage is reported.
- Nightly spending more model calls than the per-session hooks it backs up. Catch-up is capped per
  site per night (§6), and a night with no new finals costs zero calls.
- A silent write that a person cannot undo, or that cannot be told apart from another writer's.
- A nightly card stream (parent §12 bar: ≤3 cards per recipient per NZ working day, p95, all
  sources together).

## 3. Jobs

### J1 Catch-up for late arrivals (no model call of its own)

**Precondition (cross-line, §8):** both owning lines must order items by **recording time**, not by
DB write time. Today `find_items.first_seen` is the first row's `created_at` (the write time;
`item_history.py:112`), and the cross-day plan's `select_candidates` drops candidates whose
`first_seen` is not earlier than the new items'. With write time, a Monday recording uploaded
Wednesday looks newer than Tuesday's, so (a) re-emitting Tuesday's request would drop Monday's
items again, and (b) Monday's own request can link Monday → Tuesday, an edge reversed in site time.
(b) is a defect in the cross-day line independent of this design and is reported to TrackB.
`find_items` already returns the recording date (`first_date` = the topic's `report_date`) and
`source.session_base`, which encodes the session start (`Benl1_2026-03-20_12-18-34[_sid…]`, parsed by
`transcript_utils.extract_base_time_from_filename`; naive, device-local NZ; None = unknown). Both lines accepted recording-time ordering on 2026-11-10 (§8); J1 is built on their rev-3 builders.

**Detection**, per site and night: a session L whose final was written in the last 24 h, and an
earlier-written session N on the same site where:
- L was **recorded before** N (recording instant, not date: at reconcile R0 both are the same
  recorder on the same day), and
- L's final was **written after** N's final, and
- L's recording falls inside N's window: reconcile at N's rung (R0: same recorder, same NZ date);
  cross-day: L.date ≥ N.date − 28 days.

**Action:** for each stale (N, line) pair, newest N first, re-read N's final extraction from S3
in-VPC (the item writer already reads extractions from S3) and call the owning line's **request
builder** with it (§8), then put the artifact under that line's prefix with `"trigger": "nightly"`.
- Reconcile's key (`sha256(extraction_key)[:16]`) is overwritten. That is acceptable: the S3 event
  fires again, the worker re-runs on the larger candidate set, and the writer's
  `UNIQUE (stable_id, suggested_status)` absorbs repeats.
- Cross-day's `run_hash` is the extraction key plus `extracted_at`, so a re-emit would collide with
  the original's record dedupe. The builder takes an optional `nonce` (the night's date) folded into
  `run_hash` (§8).

**Record:** a small table `nightly_reemits` (site_id, line, n_extraction_key, l_extraction_key,
artifact_key, night, created_at, `UNIQUE (line, n_extraction_key, l_extraction_key)`). This is not
`decision_records`, whose `subject_stable_id` is NOT NULL and item-keyed (0073); a re-emit has no
item subject. The unique key is the "never twice for one (N, L)" invariant.

**Dropped requests: after both lines' per-request records land.** Today a request whose retries ran
out looks like one with zero claims. Both lines agreed (§8) to record per request: cross-day an
`item_continuity_xday_run` record, reconcile a `reconcile_results/` file. Once both exist, J1 also
re-emits a request older than 6 h with no such record, once, counted in `nightly_reemits` (L = null).

### J2 Evidence repair: turn pick (model)

**Scope:** finals written in the last 7 nights whose live items lack a live `supported_by` link of
any method. Items from before that window are a **backfill script**, run once per site with a call
count the owner approves first, not the nightly job. So the nightly cost is driven by new finals
only, and a night with none costs nothing.

**Input** (built in-VPC by the hub handler, read by a non-VPC worker): per session, at most 60
items: `{alias I1.., kind, stable_id, row_id, text, topic time_range}`, plus `run_id` (§4.2). Item
text may leave the VPC (Track C §5.2 whitelist); `origin` and other sessions' text never do.

**Worker** (non-VPC):
- Rebuild the session's turns with the shared `assemble_session_turns` chain. The reconcile plan
  lifts it out of `lambda_extract_session`; whichever line builds first lifts it, and the other
  imports it.
- For each topic, number the turns inside the topic's `time_range` as `T1..Tn`.
- One model call per session: the items, and per topic its numbered turns fenced as quoted data
  (parent §4.1). The model answers per item `{"item": "I3", "turn": "T17" | null}`. Null is the
  expected answer when no single turn carries the item.
- The model comes from the registry entry `nightly_turn_pick` (§4.2). Claude models run only through
  the Claude API, never OpenRouter.

**Guards** (code, in order; the first failure names the drop):

| Guard | Rule |
|---|---|
| `shape` | known alias; the turn is one of the numbers sent for **that item's topic** |
| `window` | the turn lies inside the topic's `time_range` (rechecked from the turn's own times) |
| `twin` | two items that differ only by unit / block / level / number (Track C's twin test, numeral-normalised) may not pick the same turn, and an item may not pick a turn containing the twin's distinguishing token. Same-language pairs only; cross-language pairs are their own stratum (§7) |
| `same_language_floor` | when item and turn are in the same script, the item's distinguishing tokens (if any) must appear in the turn. A cross-language pick passes by construction and is measured separately |
| `already_linked` | the row gained a live `supported_by` link since the request was built (writer recheck) |

**Writer** (in-VPC, direct invoke): per pick (kept or dropped), one `decision_records` row of kind
`nightly_support`: `subject_type` = the item kind, `subject_stable_id` = the item's stable_id,
`object_ref` = the turn locator (`source_stem@start_sec`) or null, `question_set` = the prompt hash,
`input_key` = the request key, `output` = `{turn, guard}` (ids and enums only), `auto_outcome` =
accepted / rejected / abstained. This is per item, like cross-day's per-claim records. For each
accepted pick, `event_links.insert_supported_by` with `method='nightly_turn_pick_v1'`, a
`rule_version`, `score = null` (the model gives no score; a made-up number would invite a
threshold), `source_s3_key` = the session's extraction key, and `decision_record_id`. **The plan
extends `insert_supported_by`** with a `decision_record_id` parameter; today it does not write that
column (`event_links.py:66-98`). The writer re-reads each row through `deleted_predicates` and skips
it when it is gone, superseded, already linked, or on another site.

**Relation to Track C's next step and to the lexical linker.** Track C's planned next step for the
same hole is the extractor citing a topic-quote index (+~3% output tokens, a prompt change, paid
re-extraction to measure). My call: **measure J2 first, offline, on Track C's existing blind gold**
(§7). It needs no prompt change and no re-extraction, and it can see across languages.
- If J2 fails its bars, Track C's prompt option is next and J2 is dropped.
- If J2 passes, J2 is the **fallback writer**: it writes only where no live link of any method
  exists, and a per-session writer (lexical or prompt-index), if ever turned on, keeps first claim
  on new pass rows. `write_pass` writes links for each pass's own new rows, so the two never compete
  for one row. Readers prefer a scored link (`BEST_LINK_ORDER` is `score DESC NULLS LAST`), and that
  is the intended order.

### J3 `claim_type` recompute (no model)

`claim_type_store.recompute_session(conn, company_id=…, site_id=…, session_base=…,
statement_timeout="15s")` for:
- every session J2 wrote a link for tonight;
- every session whose names landed but whose voiceprint-writer recompute returned `timed_out` or
  `failed`. Today that is only logged (`lambda_voiceprint_writer.py:124-137`); the plan adds a small
  marker row so the nightly scan can find it without parsing logs.

Silent and deterministic. `claim_basis.link_id` points at the link row, whose `method` tells a
nightly-derived value from a per-session one; no new `claim_basis` field is needed.

### J4 Escalation facts → `site_events` (no model)

v1 has **one** fact, emitted to the hub, not written as a card:
- `item.recurring_open`: an action-item chain (TrackB's `continues` edges) raised on ≥3 distinct NZ
  days within 14 days, still `open`, with no status suggestion pending. `subject_ref` = the chain's
  earliest `stable_id`; bucket = ISO week. A hub rule decides whether it becomes a `fact` card
  ("raised Mon, Wed and Fri; still open — still needed?").

J4 waits until cross-day continuity is `on` somewhere. Before that there are no chains, and word
overlap is not a substitute.

## 4. Shape

### 4.1 When: site-local night

- One EventBridge `rate(1 hour)` tick.
- A site is due when its local time is in `[NIGHTLY_LOCAL_HOUR, +1h)`, default 01:00, and its night
  (the previous local date) has no `nightly.due` event yet.
- v1 has no site time-zone column, so every site is `Pacific/Auckland` through `nz_time.py`. One
  function, `site_local_now(site)`, is the seam for a later `sites.timezone` column.
- 01:00 is after quiet-hours start (21:00) and before the daily reports (`cron(0 16 * * ? *)` UTC =
  05:00 NZDT / 04:00 NZST), so a repaired link or recomputed `claim_type` is in the morning report.

### 4.2 Through the decision hub, and the model-call layer

Nightly is a **handler of the hub**, not a side system (the owner's framing). It gets runs, retries,
back-off, brakes and the replay judge from the hub.
1. The tick emits one `site_events` row per due site: `source='nightly_scan'`, `type='nightly.due'`,
   `subject_ref` = site id, bucket = the night's date. The dedupe key makes a second tick a no-op.
2. The hub dispatches handler `nightly_rematch`, which runs in-VPC: J1, J3 (for failed recomputes),
   J4, and builds J2's requests. Hub retry and back-off (parent §4.2.8) cover **this in-VPC run
   only**. The run ends `done` once J2's requests are written, or `nothing`.
3. J2's model call is the three-hop pattern (BUG-36): in-VPC handler →
   `nightly_requests/{site_id}/{night}/{session_hash}.json` → non-VPC worker (S3-triggered, wired in
   `scripts/wire-s3-events.sh`, BUG-33) → in-VPC writer by direct RequestResponse invoke, raising on
   `FunctionError` so the S3 retry fails closed. The worker's `MaximumRetryAttempts` is 1 (a retry
   is a second paid call). **J2 failures are the worker's, not the hub's**: they show as missing
   `nightly_support` records and in the circuit breaker, never as hub re-dispatches.
4. Run identity: the hub's per-company, per-site system principal (parent §4.8).

**The model-call layer: this plan builds the minimal slice J2 needs.** Parent §4.3 says the layer
arrives with the first model-backed rule; nightly is that rule. The slice:
- a registry entry (`nightly_turn_pick`: model + parameters + prompt hash, one place to change);
- the existing fallback chain, unchanged;
- accounting: the worker passes `{run_id, tokens_in, tokens_out, model, cost_usd}` in the writer
  invoke, and the writer adds them to that `event_runs` row (the run that built the request), so a
  night's cost is on the hub's run;
- the per-company daily budget **check happens in-VPC before a request is written** (the worker has
  no DB access). Over budget → the request is not written, counted, and retried the next night.

The full layer (moving the other four fallback chains onto it) stays out of this plan.

**Dependency:** P0 hub (plan `2026-11-08-site-decision-loop-p0.md` Tasks 1-4) must exist first. P0
still has no backend executor; this design does not change that.

### 4.3 Flag

`NIGHTLY_REMATCH` ∈ `off` (default) | `shadow` | `on`, wired in three places: template parameter
with AllowedValues, workflow variables `TEST_NIGHTLY_REMATCH` / `PROD_NIGHTLY_REMATCH`, and env.
- `off`: no tick.
- `shadow`: the tick runs; J1 counts stale pairs without re-emitting; J2 writes request artifacts
  without a model call (the worker is not wired in shadow); J3 and J4 count only. No DB write except
  the hub's own event and run rows.
- `on`: all jobs, each behind its own gate (§7). J1 additionally needs §3's precondition.

## 5. Silent writes: the rules

- Only J2 (`event_links` `supported_by`) and J3 (`claim_type`, `claim_basis`) write silently.
- Every nightly link has `method LIKE 'nightly_%'`, a `rule_version` and a `decision_record_id`.
- Nightly never updates or deletes a row it did not write. One live nightly link per source row:
  **four partial unique indexes**, one per source-row FK column (`src_action_item_id` …), each
  `WHERE method LIKE 'nightly_%' AND <col> IS NOT NULL`, insert `ON CONFLICT DO NOTHING`. Four
  indexes avoid depending on `NULLS NOT DISTINCT` and the Aurora PG version. Migration number: the
  next free one at plan time.
- **Revert:** `scripts/nightly_revert.py --site … --since … [--rule-version …]` deletes nightly
  links in that range and re-runs J3 for the affected sessions. Dry-run by default; prints counts.
- Logs and records carry ids, counts and codes only, never quote text.

## 6. Brakes (starting values, set from shadow counts)

| Brake | Value |
|---|---|
| J1 re-emits per site per night | 10 (newest N first; the rest wait for the next night, counted) |
| J1 look-back for late arrivals | finals written in the last 24 h; recordings up to 28 days old |
| J2 sessions per site per night | 20; items per call 60 |
| J2 model calls per session | 1 (no retry inside the worker) |
| Per-company daily budget | checked in-VPC before a request is written (§4.2); nightly is non-urgent, so it pauses first |
| Circuit breaker | parent §8.5; the same error type 5 times in one night pauses that job for the site |

## 7. Measurement and gates (pre-registered; frozen once the first result row exists)

### J2 `nightly_support`: before `on` anywhere (phase N0)
- **Set:** Track C's TEST set and blind gold, the same fit / held-out split. They exist only on the
  machine that ran Task 10 (`C:\Users\camil\wt\track-c-eval\scripts\item_support_eval\data` and
  `labels`, gitignored, counts only in git). N0 runs there. J2 has no constants to fit, so the fit
  half fixes the prompt wording only; the held-out half is scored once.
- **Owner adjudication** (Track C Task 10 Step 2) has not happened. Bar 5 needs about 40 items
  adjudicated by the owner; N0 schedules it after the runs, with the item list prepared.
- **Runs:** 3 reps, prompt frozen and hashed into `question_set`, the model the registry will
  deploy. About 59 sessions × 3 = 180 calls. **The count goes to the owner before any call.**
- **Bars on the held-out half (all must pass):**
  1. Link precision: wrong / accepted, one-sided 95% Clopper–Pearson upper bound ≤ 5% (Track C bar
     1, the same adjacency rule).
  2. Cross-language stratum (item and turn in different scripts): the same bar on its own, or
     "insufficient" below 30 accepted links. Insufficient means cross-language picks stay off.
  3. Twin hard negatives: 0 links to the twin's turn (Track C bar 2).
  4. Rep agreement: the same pick or the same null for ≥ 90% of items across the 3 reps.
  5. Agent–owner agreement on the adjudicated sample ≥ 85% (Track C bar 4).
- **Reported:** coverage on final items against the owner's 60% line, per kind and per language;
  among items the lexical linker left unlinked, how many J2 links, split by "cross-language" vs
  "same language".
- Then two weeks `on` on TEST, with a count of nightly links whose row is no longer live.

### J1 `nightly_catchup`
- Two weeks in `shadow` on TEST, counting stale (N, L) pairs per site per night. If the count is
  near zero, J1 stays in shadow and the plan says so. The owning lines' own bars judge every
  re-emitted output.

### J4 `item.recurring_open`
- The parent's card bars: owner labels shadow cards; precision ≥ 80% and ≤ 3 cards per recipient
  per day across all rules.

### Engineering (borrowed from parent §12)
- Invariants as SQL: no nightly link without a `nightly_support` record; no nightly link on a
  non-live row at write time; no two nightly links for one source row; no nightly row of a type
  other than `supported_by`; no two `nightly_reemits` for one (line, N, L).
- Mutation tests: drop the site filter, skip the `window` guard, skip the twin guard, let the writer
  overwrite a non-nightly link, re-emit the same (N, L) twice. Each must be caught.

## 8. Interfaces with other lines (interface and owner only)

| Line | Nightly needs | That line keeps |
|---|---|---|
| jev TrackB (cross-day) | **Accepted 2026-11-10, spec/plan rev 3 in pipeline#1273.** (1) X5/X8 order by recording time: `first_date`, then session start on the same date; unknown start on the same date = unordered, dropped; `created_at` never orders. (2) public `build_xday_request(conn, *, site, company_id, extraction, extraction_key, date, nonce=None, trigger="final")`, `run_hash = sha1(key + extracted_at + (nonce or ""))[:16]`; `trigger` in the artifact only. (3) the link writer writes one `item_continuity_xday_run` record per invocation ("processed, n claims"), deduped on (input_key, input_hash) | prompt, guards, writer, `continues` rows, bars |
| Status line (reconcile) | **Accepted 2026-11-10.** (1) recording-time ordering; it is also an R0 gap (a 14:00 item could absorb a 10:00 "done"). (2) `reconcile_request.build(conn, extraction, extraction_key, site, owner_company_id) -> dict \| None` and `reconcile_request.emit(s3, bucket, body)` (R0 Task 4); overwrite is safe: same S3 event, suggestions unique on (stable_id, suggested_status), pick cards on (segment_key, offset_sec, suggested_status). Caveat: a suggestion the person dismissed stays dismissed even when a re-run brings new evidence (0090). (3) later: `reconcile_results/<same key>.json` with counts after a successful writer invoke; no file = not processed | the same |
| Track C | `find_items`, `insert_supported_by` gaining `decision_record_id`, `recompute_session`; agreement that J2 vs the prompt-index option is decided by measurement, and that J2 is a fallback writer if both ever run | `event_links` schema, `item_support`, its harness and gold |
| P0 backend line | the hub's emit function and handler registration | the hub |

**Proposal to the status line (not a ruling):** reconcile §12.2 gives pick cards their own small
store. A pick card is a parent-design `choose` card. Storing it as `decision_cards` (purpose
`choose`, options = the ≤4 candidates plus "none of these") would give it the inbox, quiet hours,
recipients and the ≤3-per-day cap, and avoid a second card system. **Accepted as the target (2026-11-10):** R0 ships its own `status_pick_cards`
table, shaped 1:1 (≤4 candidates + "none"; pending / chosen / none), and moves once `decision_cards`
exists. This line pings the status line then.

## 9. Phasing

| Phase | What | Gate |
|---|---|---|
| N0 | J2 offline on Track C's gold (owner approves ~180 calls first), then owner adjudication of ~40 items | §7 J2 bars |
| N1 | tick + hub handler in `shadow` on TEST (needs P0 Tasks 1-4) | 2 weeks of counts; errors under 2% of nights |
| N2 | J3 `on`, and J2 `on` if N0 passed, on TEST | invariants hold; orphan count reported |
| N3 | J1 `on` on TEST if its precondition is met and shadow counts justify it; J4 once cross-day continuity is `on` | §7 |
| Prod | the owner's flag, per job, release PR from this line | — |

N0 does not depend on the P0 hub; it can run as soon as the owner approves the call count.

## 10. Decided, my calls, open

**Decided by the owner** (Track C §9.4): nightly belongs to the decision loop; silent writes only
in the store-only layer; user-visible results are proposals; one `decision_records` kind per
judgement.

**My calls (the owner's standing delegation; change any):**
1. Nightly does not re-implement continuity or reconcile; J1 re-feeds those lines' request paths (§1).
2. J1 waits for both lines to order by recording time; without it J1 is dropped (§3).
3. J2 is measured before Track C's prompt option, on the existing gold; if it passes it is the
   fallback writer (§3 J2).
4. J2's nightly scope is the last 7 nights; older history is an owner-approved backfill script.
5. Nightly runs as a hub handler; this plan builds the minimal model-call-layer slice for J2 (§4.2).
6. 01:00 site-local, NZ-only in v1, with one seam function for later time zones (§4.1).
7. Re-emits are recorded in `nightly_reemits`, not `decision_records`; J2 records are per item.
8. Dropped-request re-emits wait for the owning lines' per-request records (both agreed).
9. J4 has one fact in v1 and waits for real `continues` chains.
10. Brake values in §6 are starting values; the pick-card proposal (§8) is a proposal only.

**Open (not blocking the plan):** whether any job should run more often than nightly; per-site
time zones when the first non-NZ customer appears.

## 11. Left to the builder

Module and function names; the request artifact's exact JSON shape (within the whitelist above);
how the hub handler is split across Lambdas, provided in-VPC and non-VPC stay separate; the marker
J3 uses to find failed recomputes; the revert script's interface.

## 12. Builder check (Fable, 2026-11-10), folded in

| # | Question / error | Answer |
|---|---|---|
| Q1 | Owning lines order by `created_at`, so late arrivals stay "newer"; R0 has no strictly-earlier date | J1 precondition: recording time; detection uses the recording instant (§3 J1). Reported to TrackB as its own defect |
| Q2 | Who builds the model-call layer; which retry owns J2 | This plan builds the J2 slice; hub retries the in-VPC run, the worker owns J2 failures; accounting via the writer invoke (§4.2) |
| Q3 | `decision_records` is item-keyed | J2 records per item; re-emits in `nightly_reemits` (§3) |
| Q4 | Dropped-request detection misfires | Deferred; per-request outcome record proposed to both lines (§3, §8) |
| Q5 | Day-one J2 scope is all history | Last 7 nights; backfill is a script with an owner-approved count (§3 J2) |
| Q6 | Coexistence with the lexical linker; partial index on four nullable columns | J2 is a fallback writer; four partial indexes (§3 J2, §5) |
| Q7 | Gold is local and unadjudicated | Named location; N0 runs there; owner adjudication scheduled (§7) |
| Q8 | Request-builder shape; overwrite vs new run hash | Builder takes the extraction artifact re-read from S3; reconcile overwrite accepted; cross-day `nonce` (§3 J1, §8) |
| G1 | `claim_basis` has no link method | Dropped: `link_id` → the link row's `method` (§3 J3) |
| G2 | `insert_supported_by` lacks `decision_record_id` | The plan extends it (§3 J2) |
| G3 | §1 overstated the cross-language share | Rewritten with fit-half numbers; J2 reports the split (§1, §7) |
| G4 | Zero-candidate requests are never written | Part of Q4 |
| G5 | Builder signature | Corrected to the planned signature (§8) |
| G6 | Report time is 05:00 NZDT / 04:00 NZST | Corrected (§4.1) |
