# Cross-day item continuity: link today's items to the same work on earlier days

**Date:** 2026-11-09 · **Status:** design, Fable review pending. No code yet.
**Line:** continuity (jev TrackB). **Builds on:** `2026-09-30-extractor-declares-item-continuity-design.md`
(same-session continuity), `2026-11-08-continuity-linker-design.md` (the linker call),
Track C `2026-11-08-track-c-item-history-and-evidence-chain-design.md` (`find_items`,
`event_links`; PR #1260, migration 0098).
**Owner decisions in force:** 2026-11-08 cross-day split — Track C owns `find_items` and
`event_links`; the status line owns cross-day status suggestions ("reconcile"); cross-day
continuity stays with this line. `continues` edges are written only by this line.

## 1. Problem

Every item a site raises lives inside one recording. When the same job comes up on Tuesday and
again on Thursday ("Gutter replacement quote from roofer" / "Chase the roofer for the gutter
quote"), the system holds two unrelated items. Nothing can say "this was raised two days ago and
is still open", count how long a job has been talked about, or let a later status statement find
the item it is about. Same-session continuity cannot help: everything it does is scoped to one
extraction key (prior list, carry candidates, `carried_from`, the status writer's guard).

## 2. The change

After a session's **final** pass is written, record which of its items continue an item from an
**earlier session on the same site**, as an `event_links` row:
`link_type='continues'`, `src` = the new item's `stable_id`, `dst` = the earlier item's
`stable_id`.

A link is an edge only. It never moves a `stable_id`, never copies a status or a human edit,
never supersedes a row. Two items stay two items; readers decide what to show (Track C's
history reads, later UI). This keeps a wrong link cheap to undo (delete one row) and keeps the
same-session identity machinery untouched.

The judgement is the same shape as the same-session linker: the model states the
correspondence with a verifiable echo, and code outside the model checks it.

## 3. Decisions

- **X1. Flag.** `CROSS_DAY_CONTINUITY`, default off, wired in three places (template parameter,
  workflow variables `TEST_CROSS_DAY_CONTINUITY` / `PROD_CROSS_DAY_CONTINUITY`, function env;
  memory: *fieldsight-unwired-toggle-trap*). Off = nothing is emitted, nothing runs.
- **X2. Trigger: final passes only.** Live passes change every few minutes; the final is what
  people read later. The in-VPC item writer, after it commits a final pass's rows for a session
  with a resolved site, writes one request artifact. A pass with no site is skipped (counted).
- **X3. Three hops, because of the VPC** (BUG-36: in-VPC lambdas have no egress; `find_items` is
  in-VPC only; the model is outside). The `match_requests/` pattern:
  1. **Item writer (in-VPC)** reads candidates with `find_items` and writes
     `continuity_xday_requests/{site_id}/{date}/{key_hash}.json`.
  2. **Cross-day linker (new non-VPC function, S3-triggered)** calls the model and writes
     `continuity_xday_results/{site_id}/{date}/{key_hash}.json`.
  3. **Item writer (in-VPC, S3-triggered on the results prefix)** applies the write-time checks
     (X8) and inserts the links and decision records.
  Both prefixes are registered in `lake_keys` (site-scoped, like `match`), so tenancy deletion
  and the ownership index cover them.
- **X4. New items** = the final pass's live child rows: `{stable_id, kind, text}`, all four kinds,
  at most 40 per kind (the same-session cap).
- **X5. Candidates** = `find_items(conn, company_id, site_id, kinds=[kind], statuses=…,
  since=date − 14 days, until=date, exclude_source_keys=[this extraction key], limit=40)` once
  per kind, no `query` (recency order). Statuses: action items `open`, `in_progress`, `blocked`;
  questions `open`; findings and decisions unfiltered (Track C forbids a status filter there).
  Same-day earlier sessions are included; the session's own key is excluded. Zero candidates
  across all kinds → no request.
- **X6. Model call.** Reuses the linker's prompt machinery (`render_linker_prompt` shape:
  instruction, earlier items with aliases `A1/F1/D1/Q1`, new items with refs `N1…`), with its own
  constant `XDAY_INSTRUCTION` ("EARLIER items were recorded in OTHER sessions on this site in the
  last two weeks … link only when it is the same piece of work — the same job on the same thing,
  not the same trade or the same area"), its own `XDAY_QUESTION_SET` hash, **no transcript**
  (the earlier items' transcripts are not in hand, and the new item's text is what is compared),
  thinking off, the deployed extraction model, caller tag `continuity_xday`. Each earlier item is
  shown with its first-seen date, so "next Tuesday" items are not confused across weeks.
- **X7. Guards (code, before anything is written).** Reuse `item_continuity`'s guard pieces
  without its id assignment:
  1. exact pass: a new item whose normalised text equals exactly one candidate of its kind (and
     that candidate equals no other new item) is linked with method `exact`, no claim needed;
  2. existence (alias was sent), kind (same kind), echo (`starts` is a prefix of the claimed
     candidate's tokens and of no other same-kind candidate; min `min(4, len)` tokens);
  3. one-to-one in **both** directions: two new items claiming one candidate, or one candidate
     claimed twice, drops all of them;
  4. exact beats claim (as same-session D4.5).
  Accepted links carry method `llm_echo`.
- **X8. Write-time checks (results hop).** Each link is written only if both endpoints still have
  a live row on the same site, and no `continues` row with the same (src, dst) exists
  (idempotent re-delivery). A src whose stable_id changed since the request (a later re-extraction
  carried it) is resolved through the request's `item_id` lineage; if that fails, skipped and
  counted.
- **X9. Records.** Every claim → a `decision_records` row, kind `item_continuity_xday`, output ids
  and enums only (`{src_stable_id, dst_stable_id, outcome, guard}`), `question_set` =
  `XDAY_QUESTION_SET`; the link row's `decision_record_id` points at it. `rule_version` =
  `xday_v1`. Counts-only logs and EMF `XdayLinks` by method/outcome.
- **X10. Failure never touches the extraction.** Any error in hops 2–3 leaves no links and is
  retried by S3 at most per its function's policy; the extraction and its rows are already
  committed and are never rolled back or re-run.
- **X11. Not in scope:** status changes (the status line's reconcile reads these links later),
  UI, report rendering, links across sites or companies, links older than 14 days, live passes,
  group extractions.

## 4. Measurement (pre-registered, before TEST flag-on)

There is no labelled set; build one offline from prod data (read-only, owner OK required at run
time). The harness cannot reach Aurora, so candidates are reconstructed from **published
extractions** of the same site's earlier sessions within 14 days (the latest published pass per
key ≈ the live rows `find_items` returns; status filter applied from the extraction's own status
fields where present). Sessions: every prod site with at least two recorded days in a 14-day
window, newest day as "today".

- Run the cross-day linker as deployed (same prompt, same model config) 3 times per day-pair.
- Blind agent labels (same packaging as the same-session runs: the annotator sees the new item,
  the candidate list with dates, and nothing about the model's claim), owner adjudication of
  every accepted link the agent disputes plus a random 10%.

| bar | threshold |
|---|---|
| wrong links (adjudicated) | 0 |
| hard-negative pairs (same topic, different work) | ≥ 30 labelled pairs, 0 wrong; fewer → "insufficient", not pass |
| recall (labelled same-work pairs the run linked) | ≥ 0.6 |
| linker status ≠ ok | ≤ 2% |

Recall is gated low on purpose: a missed cross-day link costs nothing that exists today; a wrong
one tells a user two different jobs are the same.

## 5. What other lines get (interface only)

- Track C: rows of `link_type='continues'`, `src_kind = dst_kind`, `dst_stable_id` set,
  `method ∈ {exact, llm_echo}`, `rule_version='xday_v1'`. Readers resolve endpoints by
  stable_id through `visible_topics_predicate` (Track C's rule).
- Status line (reconcile): may use `continues` edges to reach the earliest open item of a chain;
  this line does not write statuses.
- Item writer: gains the request emit (X2) and the results consumer (X3.3) behind the flag.

## 6. Open for the plan

- Whether the results consumer is the item writer on a new prefix or a small new in-VPC writer;
  the plan checks the item writer's trigger configuration and concurrency first.
- The exact `XDAY_INSTRUCTION` text (fixed before any record is written; it is hashed).
