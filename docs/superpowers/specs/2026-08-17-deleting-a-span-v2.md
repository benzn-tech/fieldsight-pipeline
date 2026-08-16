# Deleting a span of a recording — v2

**Status:** proposed. Nothing built.
**Supersedes:** `2026-08-14-deleting-a-span-of-a-recording.md` (v1). A review of v1 found
nine problems; six of them exist only because of a design choice this version drops.
**Predecessor:** `2026-08-14-user-deletes-a-recording.md` — whole-recording delete, live.

---

## 0. The requirements, unchanged

1. **After a delete nobody sees or searches that content.** A trust requirement — a
   paraphrase of the removed sentence surviving in a generated summary fails it.
2. **A span inside one recording can be deleted**, not only the whole recording.

And one permission granted since v1, which is what makes this version simpler:

> 可以把 vector DB 和 relational DB 的内容移除或者 archive，但是如果 restore 则再召回。

## 1. What v1 got wrong

v1 re-extracted to a **new key** — `extractions/{f}/{d}/rev{n}/{base}.json` — so the
original topics could be tombstoned instead of deleted, because deleting them CASCADEs
away `action_items` and the check-off is a column on that row.

The reasoning was right. The mechanism was not, and the review showed why:

* nothing in `lambda_extract_session` writes a rev key — `out_key` is computed once and the
  throttle, `_supersedes`, the final-rerun request and the group-merge key all derive from
  it. A later live pass would write the ORIGINAL key, which is tombstoned, so
  `lambda_item_writer` would skip it and the rest of the meeting would be invisible
  permanently;
* the original tombstone is a `LIKE` prefix, so a **subsequent whole-recording delete**
  would miss every rev topic and report `topics_hidden: 0` while the content stayed live;
* `uuid5(target_key)` plus the partial unique index means a **second span on the same
  recording** silently does nothing;
* `EXTRACTION_KEY_RE` would have to widen, undelete's `parts[3]` would read `"rev1"` as the
  session base, and no repository function exists to delete rev rows on revert.

**Archiving removes the reason for all of it.** If the rows can be moved aside and moved
back, re-extraction can run in place on the key it already uses, and every one of those
five problems is simply absent.

## 2. The mechanism

### 2.1 A ranged tombstone

`redactions` gains `range_start_sec` / `range_end_sec` (both NULL = the whole source =
today's behaviour, so every existing row keeps meaning what it means).

**`target_id` must include the range.** It is `uuid5(source_prefix)` today, and
`uq_redactions_active_deleted` is unique on `(target_type, target_id)` where active — so a
second span on the same recording collides and `ON CONFLICT DO NOTHING` swallows it while
the endpoint reports success. Make it `uuid5(f"{prefix}:{start}-{end}")`. Two spans, two
rows, and the retry-idempotency the index was added for still holds.

### 2.2 Archive, then re-extract in place

Before the re-extraction runs, move the session's rows aside, keyed on the delete's
`batch_id`:

| table | why it must be carried |
|---|---|
| `topics` | the anchor everything else hangs off |
| `action_items` | **the check-off is `status`, a column on this row** — plus assignee, deadline, priority |
| `safety_observations`, `findings`, `topic_photos` | CASCADE from topics |
| `topic_threads` | confirmed recurring-item links, CASCADE |
| `report_chunks` | already done — `archive_chunks_for_session` ships |
| `programme_suggestions` | SET NULL, so the row survives with a dangling link; record the link |

`report_chunks_archive` (migration 0044) is the pattern: `CREATE TABLE … (LIKE …)` plus
`batch_id` and `archived_at`, no foreign keys (so nothing CASCADEs into the archive) and no
indexes beyond the batch lookup.

Then `lambda_item_writer` runs unchanged — it already clears by source key and re-inserts.

**Undelete** restores the archived rows and deletes the ones the re-extraction produced.
Exactly the inverse, exactly one batch.

### 2.3 The mask

`assemble_session_turns` (`lambda_extract_session.py`) is where the extraction gets its
turns, and its docstring already says why that matters: the Tier-2 extraction, the Tier-1
rolling summary and the confirmation email all consume its output *"so all three describe
exactly the same session."*

Turns carry `abs_start` / `abs_end` — naive device-local datetimes on the one session
clock, and `_rebase_batch_turns` puts batched turns on that same clock (verified). That is
the same clock the UI's time blocks are drawn from, so the span the customer draws and the
span the backend removes are one coordinate system with no conversion between them.

**Not `chunk_start` + `duration` within a file.** That arithmetic is what the `_off{T}`
defect gets wrong (`lambda_speaker_embed.py:110`), and a batched turn's `source_filename`
is the stitched WAV rather than the device upload.

### 2.4 The other four transcript readers

This is the largest unstated cost in v1 and it does not go away. Four consumers read
`transcripts/` independently, and each implements deletion today as *drop the whole file
for a deleted session* — none can express a span:

* `lambda_report_generator._transcript_objects_for`
* `lambda_ask_agent.load_transcripts`
* `lambda_ingest._load_turns`
* `lambda_meeting_minutes.collect_transcripts`

They need one shared helper — normalize, then drop turns inside the ranges — reading the
ranges from the S3 mirror (§2.5), because two of those lambdas have no database. That
helper has to include the batch rebasing, or a batched session's turns are compared against
the wrong clock.

### 2.5 The mirror

`redactions/{folder}/{date}/ranges.json`, alongside the existing `deleted_sessions.json`,
with both lessons that one learned the hard way already applied:

* **merge, never overwrite** — a second span-delete on the same day must not free the first;
* **read strictly** — an unreadable mirror must abort the write rather than clobber it
  (`deletion_mirror.MirrorUnreadable`), because a lenient read turns a merge into an
  overwrite and an undelete's subtraction into an empty document.

## 3. The interim window

Between the request committing and the re-extraction landing — **minutes**, it is an LLM
call — the **entire session** is hidden, not just the span.

Deliberate, and the only honest interim state: until the new extraction exists nothing can
say which topics quoted the removed passage, and showing one that *might* is the failure
the feature exists to prevent. The response says so and the UI must show it.

**If the re-extraction never lands, the session stays hidden.** That is a silent permanent
outage of real content. Reuse what exists rather than inventing:
`lambda_finalize_claim._request_extraction` already writes `extraction_requests/` from a
per-minute in-VPC sweep, and `batch_redrive` already implements bounded attempts with a
DynamoDB attempt counter and a count logged including zero.

Detection query — and note the escape, because `_` is a SQL wildcard and session bases are
full of them (`topics._escape_like` exists for exactly this):

```sql
SELECT count(*) FROM topics WHERE source_s3_key LIKE :prefix || '%' ESCAPE '\';
```

Zero means the re-extraction failed and that session is hidden forever.

## 4. Check-offs: reconcile explicitly, never automatically

The re-extraction's action items are new rows. The archived originals still exist, so
nothing is lost — but the new ones start unticked.

**Do not match them automatically.** The model may reword, merge two into one, or split one
into two. A confident wrong match puts a supervisor's tick on a *different* action item and
nobody will ever see it. A missing tick is visible and recoverable; a moved tick is neither.

After the re-extraction lands, the person who deleted the span gets a reconciliation list —
"3 action items here were ticked before; this is what they map to now" — confident 1:1
matches pre-selected, the rest left for a human. One extra click, in exchange for removing
a whole class of undetectable error. (Decided with the customer, 2026-08-17.)

## 5. What this removes, and what it does not

**Removed:** the turns in the span everywhere the transcript is read; the
`transcript_window` chunks built from them; the topic text and its chunks, because the
topics are *replaced* by the re-extraction rather than filtered; the day's report prose,
which regenerates through the same masked turns; and the stored `daily_report.json` is not
served verbatim for a day with redactions — reusing the guard both gateways now have.

**Not removed, by design:** the audio and the original transcript objects on S3. Nothing is
ever deleted there. Email already delivered.

## 6. Out of scope

One gesture across several recordings. Photos (`topic_photos` CASCADEs and PhotoGrid has
its own keyframe delete — a different mechanism that must not end up behind the same
button). Any S3 deletion, ever.

## 7. Verification

Inherited from the predecessor's six surfaces (Evidence, search, Ask, daily report,
media/presign, nightly email), on **both gateways** — the legacy one now honours deletions
too and has its own copy of every read path. Plus:

* **A positive control first** — search for the passage and get a hit *before* deleting.
  Without it, "nothing found" afterwards proves nothing.
* After commit: that search returns nothing **and** the rest of the day is visibly hidden,
  matching what the response said.
* After the re-extraction: the surviving topics are back, the passage is not — check the
  topic text and the summary prose, not only search — and the count query above is
  **non-zero**.
* The S3 object count under the session's prefixes is unchanged throughout.
* A ticked action item on a surviving topic: its archived row still says ticked, and the
  reconciliation list offers it.
* **Delete a second span on the same recording** and confirm the first is still hidden.
  This is the case the `uuid5` collision silently swallowed, and the case v1's tests would
  have passed while failing.
* Undelete: the original topics come back with their check-offs, and the re-extraction's
  rows are gone.

## 8. Cost, stated plainly

Six archive tables, one shared transcript-masking helper threaded through four lambdas, a
schema change, a re-drive, and a reconciliation UI. This is weeks, not days. The cheaper
thing — masking turns and the topics that quote them, without re-extraction — was priced in
v1 §5 and **does not meet requirement 1**: nothing links a topic back to the transcript
span it came from (`time_range` is LLM free text the pipeline forbids using this way, and
`topics.evidence` is NULL on every existing row because prod runs `EMIT_EVIDENCE=false`),
so the paraphrase survives.

If the requirement is ever relaxed from "nobody sees it" to "nobody can search the exact
words", the cheap version becomes viable again and is days rather than weeks.
