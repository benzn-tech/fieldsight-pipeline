# Re-bind a day's photos without losing the day

**Date:** 2026-09-08
**Status: WITHDRAWN.** Review found three blocking defects and all three are
the same mistake: every number in this document was measured against the S3
`daily_report.json` artifacts and then stated about the live Aurora read model.
The two disagree, the UI reads Aurora, and checking Aurora dissolves the
problem this spec was written to solve. §11 records it. Do not implement any
of what follows.
**Scope:** backend. One new entry point plus a repository function; no
migration, no new table, no frontend change.

---

## 1. The situation

Three PRs shipped on 2026-09-07 improving how a photo binds to a topic —
#762 (photos belong to the day), #773 (a photo belongs to what was last said
before it), #775 (location is a state, not an event). They were measured
against the complaint that started the thread: a photo taken while nobody was
talking bound to nothing and was invisible everywhere.

**None of it has taken effect on a single day.** Binding runs inside
`lambda_ingest`, which is S3-event driven:

| | |
|---|---|
| `fieldsight-prod-ingest` deployed | **2026-09-07 13:56 Z** |
| its last actual invocation | **2026-09-07 09:25 Z** |
| `daily_report.json` written after the deploy | **0** |

So the new code has never executed. The only way to apply it today is to
re-trigger ingest for a day, and that is the problem this spec exists to
avoid.

## 2. Why re-running ingest is the wrong lever

`lambda_ingest.py:649` runs `topics.delete_topics_for_source(conn, report_key)`
**unconditionally** — the comment says "always" — and five tables cascade off
`topics(id)`:

| child | rebuilt by a re-run? |
|---|---|
| `action_items` | yes, but `status` returns to `open` — **every check-off is lost** |
| `safety_observations` | yes |
| `topic_photos` | yes — this is the thing we actually want |
| **`findings`** | **no.** The ingest path never writes findings; only `lambda_item_writer` does, from an extraction |
| **`topic_thread_suggestions`** | **no.** And `thread_id` means, per its own migration comment, *"a person said yes"* |

Measured on prod: **12 `action_items` rows are `done`**, on four
`(date, user)` pairs. `findings` and confirmed threads have no such count
because they would simply be gone.

`lambda_item_writer` faced the identical problem and wrote down its verdict:
it does not try to save the check-off, it makes the loss *visible*
(`_warn_if_discarding_checkoffs`). **`lambda_ingest` does not even do that** —
it deletes silently.

Trading a permanent loss of findings and human-confirmed threads for photo
bindings is not a trade worth making, and it is not one this system should be
able to make by accident.

## 3. What to do instead

**Synchronise `topic_photos` alone, from the current binding code, without
touching the topic row.**

Everything the delete-and-reinsert path destroys hangs off `topics(id)`. Leave
that row alone and none of it is at risk: `topic_photos` has **no children of
its own** (nothing `REFERENCES topic_photos`), so it is the one table in the
group that can be rewritten in isolation.

## 4. Measured: what this would do

Dry-run over the whole prod corpus, comparing the current binding code's output
against what `topic_photos` records today:

| | |
|---|---|
| days that would change | **11** |
| bindings already correct | **74** (untouched) |
| bindings that would be **added** | **86** |
| topic rows deleted | **0** |

```
  +28  2026-09-02 Neil_Blunden      25 -> 53
  +20  2026-08-18 Neil_Blunden       5 -> 25
  +15  2026-04-07 Ben_Test          29 -> 32
   +7  2026-08-12 Sam_Yu             0 ->  7
   +5  2026-08-14 Neil_Blunden       0 ->  5
```

## 5. The finding that decides the design: **binding REASSIGNS**

`2026-08-07 Ben_UCPK` gains 2 bindings and its total goes **12 → 11**. Three
of its existing bindings point at a topic the new code no longer chooses.

So this is **not** an insert-only backfill. `add_topic_photo_if_absent`
(`repositories/topics.py:803`) already exists — idempotent, `WHERE NOT EXISTS`,
per-row SAVEPOINT for the concurrent-re-extraction race — and using it alone
would leave the **stale** bindings in place. The same photo would then sit
under two topics, and Evidence's group-by-topic view would show it twice.

The operation is a **difference**, in this order:

1. compute the new binding set for the day
2. **insert** what is new, via the existing `add_topic_photo_if_absent`
3. **delete** the `topic_photos` rows for that day's topics whose
   `(topic_id, s3_key)` the new set does not contain
4. leave everything else alone

Step 3 is the new code. Its blast radius is one table and one day.

## 6. Tombstones are the trap

Migration 0053 made a photo a first-class deletion target: a photo a user
deleted is recorded in `redactions` with `target_type='photo'`, and the row in
`topic_photos` is **not** what carries that decision.

**A naive re-bind puts every deleted photo back on screen.** The binder reads
S3, which still holds the object; the deletion lives only in the tombstone
table. This is the single way this change could do real harm, and it would look
like a feature working correctly.

So: the new set is filtered against `redactions` where `target_type='photo'`
before step 2, and a test drives one tombstoned photo through the whole
operation and asserts it is neither inserted nor left behind.

Note that `photo_list_for_day` does no tombstone filtering of its own — its own
docstring says so — so this filter cannot be borrowed from there and has to be
explicit here.

## 7. Shape

A new op on an existing lambda rather than a new function: `lambda_ingest`
already imports `photo_binding`, already knows how to list a day's pictures,
and already has the DB connection. It gains an invocation mode that runs
**only** steps 1–4 above:

```json
{"op": "rebind_photos", "user_folder": "Neil_Blunden",
 "date": "2026-09-02", "dry_run": true}
```

- **`dry_run` defaults to TRUE.** The caller has to ask for the write. A
  backfill whose default is "write" is one keystroke from a bad afternoon.
- The response reports `{would_add, would_delete, unchanged, tombstoned_skipped}`
  so a dry run is worth reading, not just worth running.
- No S3 event trigger. This is invoked deliberately, one day at a time.

## 8. What this deliberately does not do

**It does not touch the topic row**, so `findings`, `action_items` (and their
check-offs), `safety_observations` and confirmed threads are all untouched by
construction rather than by care.

**It does not re-run extraction or regenerate the report.** The report document
in S3 keeps whatever `related_photos` it was written with; this changes only
the Aurora read model the UI actually reads. That divergence is worth stating:
after this runs, `daily_report.json` and `topic_photos` disagree, and the UI
follows Aurora.

**It does not backfill automatically.** Eleven days is a list a person can read.

## 9. Testing

- **The difference is a pure function.** Given the new set and the current
  rows, assert the three buckets (add / delete / unchanged) partition the
  input, and that a day where nothing changed yields three empty sets.
- **A reassigned photo produces one insert AND one delete** — the
  `2026-08-07` case. Assert both, because an insert-only implementation passes
  every other test in this list.
- **A tombstoned photo is neither inserted nor retained.** Drive the real
  operation with one `redactions` row.
- **`dry_run: true` writes nothing.** Assert via the connection double that no
  INSERT or DELETE was issued — and run the same case with `dry_run: false` to
  prove the assertion can fail.
- **The topic row and its other children are untouched.** Count
  `findings`/`action_items` before and after against a real database, not a
  double: this is exactly the class of claim that unit tests have passed and
  reality has refused in this repo (`CLAUDE.md`, "Run the SQL against a real
  database").

## 10. Open

**Should the report document be rewritten to match?** Leaving it stale means
two records of the same fact disagree, which is how the site-attribution bug
started. Rewriting it means touching an S3 object an S3 event watches, which is
the BUG-13 loop family. Neither is obviously right and it does not block this
change — the UI reads Aurora.

---

## 11. Why this spec is withdrawn

Verified against prod (read-only Data API) after review, not assumed:

**The photos are already bound.** `2026-09-02 Neil_Blunden` has **53 rows in
`topic_photos`** right now. This spec's headline claim — "+28, 25 → 53" — was
already true in the database before a line of it was written. Every "current"
number in §4 (25, 5, 12, 29) matches that day's `related_photos` in the S3
report document and matches **nothing** in Aurora.

**Binding does not run where this spec says.** prod `fieldsight-prod-ingest`
has `AUTHORITY_FLIP=true`, and `_should_defer` makes ingest defer on any day
that has extraction topics. `photos_for_topics` is only reached in the
`else` branch. Every day named in §4 is extraction-sourced, so ingest binds
nothing on them. The binding that matters runs in `lambda_item_writer`.

**§2's risk argument points at the wrong lever.** `delete_topics_for_source`
at ingest:649 deletes only `reports/…`-sourced rows. All 196 `findings` and
all 12 `done` action items hang off extraction topics, and
`topic_photos` is 141 extraction rows to 2 report rows. Re-running ingest on
those days would destroy none of it — and would also re-bind nothing. The
destructive re-run is `lambda_item_writer`'s, keyed on the extraction.

So: nothing is broken, nothing needs backfilling, and the "ingest last ran
before the deploy" observation that started this was a measurement of a lambda
that does not do the binding.

**The pattern, because it is the fifth instance in two days.** Measure the
lake, design for the page. Measure the S3 artifact, argue about the database.
The report document and the Aurora read model are two records of the same fact
and they diverge by design — the report is a document, Aurora is the item
store, and the UI reads Aurora. Four earlier specs made the same substitution
in different clothes. The check costs one query and it is the one I did not run.
