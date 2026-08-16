# Span deletion — reconciling two designs written in parallel

**Date:** 2026-08-17. **Not scheduled.** Nothing here is started.

Two sessions worked on this at once and produced different mechanisms. This note says which
one wins and why, carries forward everything from the other that survives the change, and
records two factual corrections — one of them to a claim I made.

| document | mechanism |
|---|---|
| `specs/2026-08-14-deleting-a-span-of-a-recording.md` (v1) + `plans/2026-08-17-deleting-a-span-of-a-recording.md` | re-extract to a new `rev{n}/` key, tombstone the original |
| `specs/2026-08-17-deleting-a-span-v2.md` (v2) | **archive the rows, re-extract in place** |

---

## 1. v2's mechanism wins, and the reason is information, not judgment

v1 chose the rev key because deleting the original topics CASCADEs away `action_items`, and
the check-off is a column on that row. That reasoning was correct and neither document
disputes it.

**What changed is a permission granted after v1 was written** (customer, 2026-08-17):

> 可以把 vector DB 和 relational DB 的内容移除或者 archive，但是如果 restore 则再召回。

Once rows can be moved aside and moved back, the rev key has no job left, and five problems
the v1 review found exist only because of it:

* nothing threads a revision through `lambda_extract_session`'s `out_key`;
* a later whole-recording delete's LIKE prefix misses rev topics and reports
  `topics_hidden: 0` with the content live;
* `uuid5(target_key)` plus the partial unique index swallows a second span silently;
* `EXTRACTION_KEY_RE` widening, and undelete reading `"rev1"` as a session base;
* no repository function exists to remove rev rows on revert.

**Superseded from the v1 plan:** Phase 2 entirely (revision keys, supersession, the
rev-specific idempotency rule), the rev-path exclusions in Phase 5, and Phase 4's "rev topics
are tombstoned, never hard-deleted". None of it has anything to stand on once the mechanism
is archive-in-place.

The v1 plan is otherwise the better-developed document and Phases 1, 3 and most of 5 apply
unchanged.

## 2. What the v1 plan found that v2 must adopt

These are design-independent and v2 was silent or wrong on all four.

### 2.1 `lambda_embed_report` belongs in the consumer list

v2 §2.4 names four transcript readers. There is a fifth, and it is the one that breaks
loudly: `lambda_embed_report` calls `lambda_ingest._load_turns` and is **not in the VPC**. If
its mask and the ingest's mask differ by a single turn, the surviving turns pack into
different windows, `chunk_text` differs, its sha256 differs, every vector lookup misses and
`embed_from_sidecar` raises — the whole report fails to ingest, every night, for everyone.

The mask must therefore be **one pure function reading the S3 mirror**, byte-for-byte
identical across all of them. This is not a preference; it is the same constraint that
already forced `_load_turns` to read the mirror rather than Aurora.

### 2.2 "Minutes" is true of topics and false of search

Re-extraction lands in minutes. The `transcript_window` chunks are rebuilt by the **nightly**
ingest, so the surviving content is **absent from search for up to ~24 hours** after a span
delete. v2 §3 implies otherwise. It is a product consequence and belongs in the UI copy.

With it comes a state transition v2 never wrote down: the session enters
`deleted_sessions.json` for the interim (or the nightly rebuild resurrects it) and must
**leave** it once re-extraction lands, with `ranges.json` taking over. Without that handoff
the surviving part of that session never returns to search at all.

### 2.3 `_should_defer` is load-bearing by accident

`_should_defer` calls `has_topics_for_source_prefix`, which carries **no visibility filter**,
so tombstoned topics still count and `lambda_ingest`'s prefix cleanup does not run. Under
v2 the rev-path half of the concern disappears, but the invariant does not: if anyone
"tidies up" that existence probe by adding a deleted filter, an interim span-deleted day
makes the cleanup fire against `extractions/{folder}/{date}/%`. Write it down and pin it with
a test that goes red.

### 2.4 One more reason the cheap alternative fails

v2 §8 rejects mask-without-re-extraction because nothing links a topic back to its transcript
span. The v1 plan adds a second, independent reason: **retrieval is semantic**. A topic chunk
that restates the removed passage is still found by meaning. String matching cannot miss
that; it was never in the running.

## 3. Two factual corrections

### 3.1 "CASCADE has never fired for extraction topics" is FALSE — and it was my claim

I wrote it in the v1 spec and repeated it in a commit message. Verified wrong:
`lambda_extract_session` computes `out_key` **once** and writes both the live and the final
tier to that same key (`tier: TIER_FINAL if final else TIER_LIVE` rides inside the artifact);
`lambda_item_writer` clears by that key before re-inserting.

So every final pass CASCADEs away the live pass's topics — and with them any
`action_items.status` a person ticked while the meeting was still running. **That is
happening today, on prod, with no deletion feature involved.** It is a separate defect and
deserves its own look; the window is short and it needs someone ticking during a live
meeting, but the loss is silent and permanent.

### 3.2 The thread citation was wrong

`0032:61/65` is `topic_thread_suggestions`, not `topic_threads`. Confirmed threads survive a
topic delete — `topics.thread_id` is the reverse link and is SET NULL. What a delete costs is
the topic's membership and its unanswered suggestions. The direction stands; v2's archive
table should name the right table.

## 4. What is actually next

Nothing is scheduled and nothing should start tonight. When it does:

1. Fold §2.1–§2.4 into `specs/2026-08-17-deleting-a-span-v2.md`, and mark the v1 spec and the
   rev-specific phases of the v1 plan superseded **in those files**, so the next reader does
   not have to find this note first.
2. Take Phase 1 of the v1 plan verbatim — the shared mask — since it is the largest piece,
   it is design-independent, and it is inert until a `ranges.json` exists.
3. Only then the schema and archive work.

And separately from this feature entirely: **§3.1**.
