# Cross-day item continuity: link today's items to the same work on earlier days

**Date:** 2026-11-09 · **Revision:** 3 (rev 2 after Fable review 2026-11-09: 2 critical / 6 important / 8 minor, folded in; rev 3 2026-11-10: recording-time order, nightly re-emit interface; §8)
**Status:** design. No code yet.
**Line:** continuity (jev TrackB). **Builds on:** `2026-09-30-extractor-declares-item-continuity-design.md`
(same-session continuity), `2026-11-08-continuity-linker-design.md` (the linker call),
Track C `2026-11-08-track-c-item-history-and-evidence-chain-design.md` (`find_items`,
`event_links`; PR #1260, migration 0098).
**Owner decisions in force:** 2026-11-08 cross-day split — Track C owns `find_items` and
`event_links`; the status line owns cross-day status suggestions ("reconcile"); cross-day
continuity stays with this line; `continues` edges are written only by this line. Review
questions were decided by the controller under the owner's standing delegation (§8); prod flag
changes remain the owner's.

## 1. Problem

Every item a site raises lives inside one recording. When the same job comes up on Tuesday and
again on Thursday ("Gutter replacement quote from roofer" / "Chase the roofer for the gutter
quote"), the system holds two unrelated items. Nothing can say "this was raised two days ago and
is still open", count how long a job has been talked about, or let a later status statement find
the item it is about. Same-session continuity cannot help: everything it does is scoped to one
extraction key.

## 2. The change

After a session's **final** pass is written, record which of its items continue an item first
seen in an **earlier session on the same site**, as an `event_links` row: `link_type='continues'`,
`src` = the new item, `dst` = the earlier item.

A link is an edge only. It never moves a `stable_id`, never copies a status or a human edit,
never supersedes a row. A wrong link is undone by deleting one row; same-session identity is
untouched.

**Why a separate call, not the final-pass linker (linker spec L9 anticipated reuse).** The site of
a session is resolved by the item writer at write time (declared site, directory), after the
extraction call; and `find_items` is in-VPC. So the cross-day prior cannot be in hand when the
final pass's linker runs. A post-write call is the only place both the site and the candidates
exist. It also leaves the just-measured same-session linker untouched.

## 3. Decisions

- **X1. Flag, three states.** `CROSS_DAY_CONTINUITY` ∈ `off` (default) | `shadow` | `on`, wired in
  three places (template parameter with AllowedValues, workflow variables
  `TEST_CROSS_DAY_CONTINUITY` / `PROD_CROSS_DAY_CONTINUITY`, item-writer env; memory:
  *fieldsight-unwired-toggle-trap*).
  - `off`: nothing.
  - `shadow`: hop 1 only — the request artifact is written; no model call, no DB write. Used to
    measure on real candidates (§4).
  - `on`: all hops.
- **X2. Trigger: final passes only, complete ones.** Inside the item writer's connection block,
  **after** `_carry_forward_children` and `number_new_items` (so src stable_ids are final), build
  the request; put it to S3 only after the commit, alongside the other post-commit emits. Skip
  (counted): no resolved site; `extraction["incomplete"]` (a fuller final follows and re-keys
  rows); zero new items; zero candidates.
- **X3. Hops** (BUG-36: in-VPC lambdas have no egress; `find_items` is in-VPC; the model is not).
  The `match_requests/` pattern, including its direct writer invoke:
  1. **Item writer (in-VPC)** writes `continuity_xday_requests/{site_id}/{date}/{run_hash}.json`
     (`run_hash` = hash of extraction key + `extracted_at` + optional `nonce`, so an overwritten
     request is detectable and a deliberate re-emit gets its own records; the builder is public
     for the nightly re-match agent, which passes a nonce and logs `"trigger": "nightly"` in the
     artifact only). Registered in `lake_keys` (site-scoped, like `match`).
  2. **Cross-day linker (new non-VPC function, S3-triggered on that prefix)** calls the model,
     applies the guards (X7), then invokes **a new small in-VPC `continuity-link-writer`**
     (clone of `lambda_suggestion_writer.py`'s shape) RequestResponse, raising on `FunctionError`
     so the S3 retry is fail-closed (as `lambda_programme_matcher.py` does). Its
     `EventInvokeConfig.MaximumRetryAttempts` is pinned to 1: a retry is a second paid model call.
  3. **Link writer (in-VPC)** applies the write-time checks (X8) and inserts links and records.
  No results prefix, no second trigger on the item writer's hot path. The new S3 trigger is wired
  through `scripts/wire-s3-events.sh` (BUG-33), and IAM gets PutObject on the request prefix
  (item writer) and GetObject on it (linker).
- **X4. New items** = the final pass's live child rows of kinds **action item, question,
  finding** (not decisions: a restated decision is a new decision), each
  `{stable_id, item_id (may be null), kind, text, first_seen}`, at most 40 per kind.
- **X5. Candidates**, per kind, through `find_items` with `since = date − 28 days`,
  `until = date`, `exclude_source_keys` = every key of this session (all `-r{n}` reopen rounds),
  statuses: action items `open|in_progress|blocked`, questions `open`, findings unfiltered.
  Selection, capped 40 per kind: the union of (a) recency top 20 and (b) for each new item of that
  kind, the lexical top 5 from `find_items(query=<new item text>)`. Then:
  - drop candidates not **recorded** strictly earlier than the new item. Order is recording
    time, never DB write time (`find_items.first_seen` is `created_at`, so a device uploading
    Monday's recording on Wednesday would otherwise look newer than Tuesday): compare
    `first_date` (the recording date), and on the same date the session start time; a same-date
    pair whose start times are not both known is not ordered and is dropped (a missed link costs
    nothing that exists today);
  - **collapse chains**: candidates already joined by `continues` edges are shown once, as the
    newest member of the chain; a link to it is a link to the chain.
  Hop 1 records, per kind, how many live candidates existed before the cap (counts only).
- **X6. Model call.** A new renderer (the same-session one requires a transcript and has no
  dates): constant `XDAY_INSTRUCTION` (fixed in the plan before any record is written; hashed into
  `XDAY_QUESTION_SET`), earlier items with aliases `A1/F1/Q1` **and their first-seen date**, new
  items with refs `N1…`, **no transcript**, thinking off, the deployed extraction model of that
  stage, caller tag `continuity_xday`. The instruction says: link only when it is the same piece
  of work — the same job on the same thing; a routine task repeated on another day (clean-up,
  toolbox talk, daily inspection) is a new item, not a continuation, even when worded identically.
- **X7. Guards (code).** No exact-text auto-accept across days: identical texts go to the model
  like any other pair (echo is then trivially satisfied). Then, from `item_continuity`'s pieces:
  existence, kind, echo (unique prefix among same-kind candidates; it disambiguates the alias, it
  does not prove sameness), one-to-one in both directions (two claims on one candidate, or one new
  item claiming two, drop all of them). **Fuse:** if more than half of a request's new items are
  linked, drop the whole request's links (counted) — a model treating everything as a
  continuation is a failure, not a result.
- **X8. Write-time checks (link writer).** Per link: both endpoints have a live row on the
  request's site (src resolved by stable_id, falling back to `item_id` lineage when non-null;
  else skipped and counted); dst still recorded earlier than src (X5's order). Insert with
  `ON CONFLICT DO NOTHING` on a new partial unique index
  `(src_stable_id, dst_stable_id) WHERE link_type = 'continues'` (migration in this line's plan).
  Link row: src FK column of the src kind and `source_s3_key` = the request's extraction key, so
  `delete_topics_for_source` cascades it and `idx_event_links_site_source` finds it;
  `method = 'llm_echo'`, `rule_version = 'xday_v1'`.
- **X9. Records.** Every claim → `decision_records`: kind `item_continuity_xday`,
  `subject_type` = src kind's record type, `subject_stable_id` = src stable_id, `object_ref` = dst
  stable_id, `question_set` = `XDAY_QUESTION_SET`, `input_key` = extraction key, `input_hash` =
  `run_hash`, output `{src_stable_id, dst_stable_id, outcome, guard}` (ids and enums only).
  Re-delivery dedupe key: (`kind`, `input_key`, `input_hash`, `object_ref`,
  `output->>'src_stable_id'`). The link's `decision_record_id` points at its record. The link
  writer also records once per request "processed, n claims" (kind `item_continuity_xday_run`,
  `input_key`/`input_hash` as above), so a request whose retries ran out is told apart from one
  that legitimately produced zero links. Counts-only
  logs; EMF `XdayLinks` by outcome and `XdayOrphanedLinks` (links whose endpoint is no longer
  visible, counted by a periodic read).
- **X10. Failure never touches the extraction.** Hop 1 errors are logged and skipped after the
  commit; hops 2–3 errors leave no links.
- **X11. Not in scope:** status changes (the status line's reconcile may read these links), UI,
  reports, cross-site or cross-company links, live passes, group extractions, decisions.

## 4. Measurement (pre-registered, before `on` anywhere)

1. **Shadow on TEST, then prod** (prod `shadow` is an owner flag change; S3 only, no model call,
   no DB write). Collect request artifacts for at least two weeks or 30 requests with candidates,
   whichever is later. They are the real `find_items` candidates, with the pre-cap counts.
2. **Offline run:** the deployed cross-day linker code over those artifacts, 3 reps, under the
   **prod** stage's model config.
3. **Blind agent labels** (the annotator sees the new item, the dated candidate list — uncut, from
   a second read of the same window — and nothing about the model's claim), owner adjudication of
   every accepted link the agent disputes plus a random 10%. A chain counts as one counterpart:
   either endpoint is correct.

| bar | threshold |
|---|---|
| wrong links (adjudicated) | 0 |
| wrong links among identical-text pairs | 0, reported separately |
| hard-negative pairs (same topic, different work) | ≥ 50 labelled, 0 wrong; fewer → "insufficient" |
| recall, capped (labelled same-work pairs inside the cap the run linked) | ≥ 0.6 |
| true counterpart beyond the cap | reported; > 10% sends the plan back to X5 |
| fuse trips | reported |
| linker errors | ≤ 2% of requests |

Recall is gated low on purpose: a missed cross-day link costs nothing that exists today; a wrong
one tells a user two different jobs are the same.

## 5. What other lines get (interface only)

- Track C: rows `link_type='continues'`, `src_kind = dst_kind` ∈ {action_item, question,
  finding}, `dst_stable_id` set, `method='llm_echo'`, `rule_version='xday_v1'`, a dst that may be
  the newest member of an earlier chain. Readers resolve endpoints by stable_id through
  `visible_topics_predicate`; an endpoint that is no longer visible hides the link.
- Status line (reconcile): may follow `continues` edges to the earliest open item of a chain;
  this line writes no statuses.
- Item writer: gains the request emit (X2) behind the flag; no new trigger.

## 6. Owner actions (numbered when the time comes)

`TEST_CROSS_DAY_CONTINUITY=shadow` is this line's to set (TEST). `PROD_CROSS_DAY_CONTINUITY`
(`shadow`, later `on`) is the owner's, with a release PR and run number supplied by this line.

## 7. Open for the plan

- The exact `XDAY_INSTRUCTION` text.
- The migration number for the partial unique index (next free number at plan time).

## 8. Review log (Fable, 2026-11-09)

- C1 exact pass across days → removed; identical-text stratum measured.
- C2 recency cut drops long-running items → union of recency and per-item lexical top 5; pre-cap
  counts; beyond-cap rate reported.
- I1 hop-1 placement, item_id, incomplete finals → X2, X4, X8.
- I2 results prefix → direct invoke of a new in-VPC link writer; retries pinned.
- I3 rows → X8 (FK, source key, unique index), X9 (subject, object_ref, dedupe key).
- I4 chains → collapse to newest member; gold treats a chain as one counterpart.
- I5 harness ≠ find_items → shadow mode supplies real candidates; hard-negative floor 50; prod
  model config named.
- I6 why not L9 → §2.
- Minor: orphan count (X9), echo's role (X7), fuse (X7), no decisions (X4), new renderer (X6),
  time order (X5, X8), reopen rounds (X5), IAM and run hash (X3).
- Owner questions decided by the controller: (1) identical routine items are linked only when the
  model judges them the same job — no auto-link; (2) window 28 days, not 14, with relevance
  selection so the wider window does not crowd out the counterpart; (3) prod shadow is acceptable
  as a design, flipping it is the owner's; (4) same-day links between devices are allowed when the
  earlier item was recorded earlier.
- Rev 3 (2026-11-10, from the 架构研究 line's review): "earlier" was DB write time, so a late
  upload reversed edges → recording date + session start (X5, X8); public request builder with
  optional nonce, and a per-request processed record (X3, X9) for the nightly re-match design
  (pipeline#1272). Nightly never writes `continues` edges.
