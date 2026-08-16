# Implementation plan — deleting a span of a recording

**Date:** 2026-08-17
**Spec:** `docs/superpowers/specs/2026-08-14-deleting-a-span-of-a-recording.md` (`0bb59e4`)
**Adversarial review verdict: GO WITH CHANGES.** The direction survives — a new `rev{n}` key
rather than overwriting, no CASCADE, an S3 request artifact for the VPC crossing. Two P0 gaps
do not, and both are the same shape as defects this repo has already shipped once.
**This plan is the spec plus the required changes; where they disagree, this plan wins.**

**Not scheduled.** Nothing here is started. It is written so the cost is visible before
anyone commits, and so the review's findings are not rediscovered.

---

## 0. Decision record — what the review changed

### 0.1 The spec predates the fix to the feature it builds on

`8e967b0` landed AFTER the spec was written and changed the mechanism underneath it: whole
recording deletion no longer *filters* the search index at read time, it **moves** the rows
into `report_chunks_archive` (migration `0044_chunk_archive`), and `lambda_ingest` masks by
the S3 mirror so the nightly rebuild cannot put them back.

That fix exists because the read-time filter never worked: `lambda_ingest` stamps every chunk
with `source_s3_key = reports/{date}/{folder}/daily_report.json`, so the tombstone's
`extractions/...` source arm can never match a chunk, and the topic arm needs
`report_chunks.topic_id`, which is NULL for every turn that fell in the unassigned bucket.

**The spec's §7 lists only `lambda_report_generator` and `lambda_ask_agent` as consumers of
`ranges.json`.** It must also list **`lambda_ingest._load_turns`** and
**`lambda_embed_report`**. Without them the deleted span returns to the search index on the
next nightly rebuild — the identical hole `8e967b0` just closed, reopened by the feature built
on top of it.

Both consumers must apply the mask through **one shared pure function reading the S3 mirror**,
byte-for-byte identical, because the chunk sha256 sidecar is compared across them.

### 0.2 Revisions need a supersession rule, and it is also the idempotency rule

A second span delete on the same session produces `rev2`, and nothing hides `rev1`: the
original tombstone's prefix match deliberately does not reach rev paths (spec §2.1), and
`item_writer` only clears the SAME key. Both revisions' topics are visible at once and the
session's content doubles.

The same gap decides idempotency. `extraction_requests` consumers are retried by Lambda for up
to 6 hours (BUG-43). If `n` is computed as "runtime max + 1", a retry inserts a duplicate.

Rule as implemented: **`n` is fixed in the request artifact**, so the consumer's same-key clear
makes a retry idempotent; and when a new revision lands, the previous revision's topics are
**tombstoned in the same transaction, never hard-deleted** — the spec's own §1.2 table says a
hard delete CASCADEs away whatever a person has since ticked off on those rows.

Concurrent deletes on one session serialise behind a per-session advisory lock.

### 0.3 "Minutes" is true of the topic surface and false of search

Re-extraction lands in minutes, but the transcript-window chunks are rebuilt by the **nightly**
ingest under the new mask. So the surviving content is **absent from search for up to ~24
hours** after a span delete. This is a real product consequence, it must be in the UI copy, and
the spec currently implies otherwise.

The state transition must also be written down: the session enters `deleted_sessions.json`
during the interim (or the nightly rebuild resurrects it), and must **leave** it once
re-extraction lands, with `ranges.json` taking over — otherwise the surviving part of that
session never returns to search at all.

### 0.4 The defer predicate is load-bearing by accident

`_should_defer` calls `has_topics_for_source_prefix` (`repositories/topics.py`), which carries
**no visibility filter**, so tombstoned topics still count, defer stays true, and
`lambda_ingest`'s prefix cleanup does not run. That is what currently protects rev paths —
and it is a coincidence. If anyone "tidies up" that existence probe by adding a deleted
filter, an interim span-deleted day makes the cleanup fire, and its
`extractions/{folder}/{date}/%` LIKE **also matches the `rev{n}/` subpaths**, CASCADE-ing the
original and revision topics together.

Write it down as an invariant and pin it with a test that goes red.

### 0.5 Two factual corrections to carry forward

* `0032:61/65` is `topic_thread_suggestions`, not `topic_threads`. Confirmed threads survive a
  topic delete (`topics.thread_id` is the reverse SET NULL). What is lost is the topic's
  membership and its unanswered suggestions — the direction stands, the citation was wrong.
* "CASCADE has never fired for extraction topics" is **false**. The BUG-43 two-layer extraction
  (PR #217/#219) writes live and final to the SAME key, so `item_writer`'s clear CASCADEs the
  live topics every time a final lands. Anything a person ticked on a live topic is already
  being lost today — worth its own look, independent of this feature.

### 0.6 The cheap alternative is rejected, and for one more reason than the spec gives

Hiding transcript turns plus "any topic whose text contains material from them" is the
turn→topic guess the spec itself rejects, wearing different clothes: a paraphrase does no
string matching, so the miss is silent. And retrieval is **semantic** — a topic chunk that
restates the removed passage is still found by meaning, which is RAG doing its job, not an
edge case. It does not meet 不能再被别人搜出来. It also does not save what it claims: the
nightly-resurrection work (§0.1) is needed either way.

---

## Phase 1 — The mask, shared by every consumer

**RED first:** a test that the same pure function is called by `lambda_ingest._load_turns`,
`lambda_embed_report`, `lambda_report_generator` and `lambda_ask_agent`, and that a turn inside
a masked range is absent from all four outputs. Fails now: two of them do not read
`ranges.json` at all.

**Change:** one pure `span_mask` module reading `redactions/{folder}/{date}/ranges.json`;
all four consumers call it. No consumer parses the JSON itself.

**Merge alone:** yes — inert until a `ranges.json` exists.
**Verification:** a real nightly rebuild on TEST over a day with a masked range, then the
positive-control search from §Verification below.

## Phase 2 — Revision keys, supersession, idempotency

**RED first:** a second span delete on a session already at `rev1` leaves `rev1`'s topics
tombstoned and only `rev2`'s visible; replaying the same `extraction_requests` artifact twice
produces one set of rows, not two; two concurrent deletes on one session serialise.

**Change:** `n` carried in the request artifact; new-revision landing tombstones the previous
revision in the same transaction; per-session advisory lock.

**Merge alone:** no — meaningless without phase 3.

## Phase 3 — The endpoint

`POST /api/org/recordings/delete-span {folder, date, sessionBase, ranges:[{start,end}], reason}`.
Authorization identical to the whole-recording delete (`_can_delete_folder`). Writes the
tombstone, the mirror, `ranges.json`, and the `extraction_requests` artifact; enters
`deleted_sessions.json` for the interim.

**Response says what is about to be true, not what is true**: the session is hidden NOW and the
surviving part returns after re-extraction, search after the nightly rebuild.

## Phase 4 — Undelete

Per-span. Reverting one of two spans **re-triggers re-extraction with the remaining spans** —
it is not "delete the rev rows". Rev topics are tombstoned, never hard-deleted. The archived
chunks of a span delete can never be restored (they contain the removed audio's text); that
终局 must be stated in the response, not discovered.

## Phase 5 — The invariants nobody would otherwise notice

* `has_topics_for_source_prefix` must NOT gain a visibility filter (§0.4) — test.
* `lambda_ingest`'s prefix cleanup must exclude `rev{n}/` paths — test.
* `_match_report_topics_to_extraction` must skip tombstoned topics and prefer rev rows, or the
  chunk→topic join drags surviving content back out of search.
* The live extraction layer short-circuits for a span-deleted session instead of paying for an
  LLM call whose result `item_writer` discards.

## Verification (against a real database, not doubles)

The two LIKE behaviours this design stands on point in OPPOSITE directions and must each be run
on a real database, per CLAUDE.md:

* `DELETED_SOURCE_PREDICATE` (`LIKE r.target_key || '%'`) must NOT match a `rev{n}/` path;
* the ingest prefix cleanup's `extractions/{folder}/{date}/%` **does** match it.

Plus: a positive-control search that returns the passage BEFORE; absent after; the surviving
content back after re-extraction AND after the nightly rebuild; S3 object counts unchanged
throughout; one ticked action item on a surviving topic still ticked afterwards. If that last
one cannot be made true, this feature is not ready and the honest answer is to keep deleting
whole recordings.
