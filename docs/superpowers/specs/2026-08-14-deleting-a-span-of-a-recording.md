# Deleting a span of a recording

**Status:** proposed. Nothing here is built.
**Supersedes:** `2026-08-14-deleting-part-of-a-recording.md` (the frontend session's write-up),
whose §3 and §5 are both rejected here, for reasons that write-up did not have.
**Predecessor:** `2026-08-14-user-deletes-a-recording.md` — the whole-recording delete, shipped
and live on test and prod.

---

## 0. The two requirements

Stated by the customer, 2026-08-14, and they are the whole acceptance test:

1. **After someone deletes, neither they nor anyone else sees or searches that content on the
   web.** This is a trust requirement, not a filtering requirement — a paraphrase of the
   removed sentence surviving in a summary fails it.
2. **A specific span inside one recording can be deleted**, not only the whole recording.

Grouping and selection are ours to choose; the two requirements are not.

---

## 1. Why both obvious designs are rejected

### 1.1 "Hide the topics in that time range" — the mapping does not exist

A topic's only time field is `time_range`, and it is **LLM free text**: `session_scope.py`'s
docstring says outright *"It must NEVER decide which session a topic is in."* Using it to
decide which topics fall in 10:15–10:22 is the use the pipeline forbids, and it fails
**silently** — a hallucinated range gives a confident wrong answer, not an error.

`topics.evidence` (migration 0037) looks like the missing link and is not:

* prod runs `EMIT_EVIDENCE=false` (read off the live function), so **every existing topic has
  NULL evidence**;
* even with it on, the anchor is an `at` timestamp the model *copies* from the transcript.
  The prompt itself warns "do not recompute it, do not assume it matches when the session
  started" — that is a field written by the thing we would be checking.

Any variant of "hide the topics that quote the removed turns" relocates the same guess from
`time → topic` to `turn → topic`. Both failure directions are silent: hide too much and we
delete content the customer kept; hide too little and the feature did not work.

### 1.2 "Re-extract over the same key" — it destroys work, by CASCADE

`lambda_item_writer.py:681` clears with `delete_topics_for_source(extraction_key)` before
re-inserting. Seven tables hang off `topics.id`:

| table | on topic delete | what is lost |
|---|---|---|
| `action_items` (0003:16) | **CASCADE** | **the check-off itself** — it is `action_items.status`, a column on the cascaded row — plus assignee, deadline, priority |
| `safety_observations` (0003:28) | CASCADE | the row |
| `topic_photos` (0003:39) | CASCADE | the row |
| `findings` (0010:10) | CASCADE | the row |
| `topic_threads` (0032:61, :65) | CASCADE | confirmed recurring-item threads |
| `report_chunks` (0004:6) | SET NULL | RAG citations lose their topic |
| `programme_suggestions` (0008:7) | SET NULL | programme impact links |

The check-off is not an independent record that could be re-matched afterwards. It is a
column on a row that CASCADE removes. **Nothing survives to re-attach to.**

This has never fired for extraction topics: prod runs `AUTHORITY_FLIP=true` (read off the
live function), so `lambda_ingest.py:590`'s `delete_topics_for_source_prefix` takes the defer
branch and never runs. A span-delete that re-extracts in place would be the first caller.

---

## 2. The decision — re-extract to a NEW key, never over the old one

Re-derivation is required (§1.1: nothing else removes the paraphrase). Destroying rows is not.
Separate the two:

```
original      extractions/{folder}/{date}/{base}.json          ← stays, tombstoned
re-extraction extractions/{folder}/{date}/rev{n}/{base}.json   ← new rows, new uuids
```

* The original topics are **hidden, not deleted** — `action_items`, threads, findings, photos
  and every check-off stay on disk behind the tombstone. No CASCADE fires.
* The re-extraction is a plain insert under a key nothing has tombstoned.
* **Undelete** = revert the tombstone and delete the `rev{n}` rows. The original audio is
  untouched on S3, which is what makes this reversible at all.

### 2.1 The trap this shape exists to avoid

Tombstones match with `LIKE target_key || '%'` (`deleted_predicates.DELETED_SOURCE_PREDICATE`).
So a revision key of `{base}__r1` — the obvious naming — **is matched by the original's own
tombstone**, and the re-extraction is hidden the moment it lands. The feature would be dead on
arrival with no error anywhere: the customer deletes a span and the whole session stays gone.

`rev{n}/` as a **separate path segment** before the base is not matched by
`extractions/{folder}/{date}/{base}%`. That is the entire reason for the shape; do not
"simplify" it back into the filename.

### 2.2 `session_id` must stay stable across revisions

`EXTRACTION_KEY_RE = ^extractions/([^/]+)/([^/]+)/([^/]+)\.json$` (`session_scope.py:135`) has
one definition on purpose and both the writer and every reader use it. It must be widened to
accept an optional `rev{n}/` segment and **still return the same `session_base`**, so:

* `session_ref` keeps returning one session id for the recording;
* `GET /sessions` keeps showing one row, not one per revision;
* the existing whole-recording delete keeps working on it unchanged;
* the UI needs no new identifier.

The revision is a storage detail. It must not leak into any identity the UI or the customer
sees.

---

## 3. Where the mask goes — one choke point, not several

`assemble_session_turns` (`lambda_extract_session.py:990`) is the single place extraction
gets its turns, and its docstring already states why that matters: Tier-2 extraction, the
Tier-1 rolling summary and the confirmation email all consume its output, *"so all three
describe exactly the same session."* A rule about what counts as speech belongs there — the
announcement filter was moved there for exactly this reason after living elsewhere and
letting the same session read differently depending on which surface you looked at.

The span filter goes in the same place, immediately after the turns are on the one session
clock and before any consumer sees them.

**Coordinates.** A normalized turn carries `abs_start` / `abs_end` as absolute datetimes on
the session clock (`transcript_utils.py:284–287`) — the same clock as the session's
`started_at` / `ended_at` that the UI's time blocks are drawn from. So the span the customer
draws on screen and the span the backend removes are the same coordinate system, with no
conversion in between. **Do not filter on `chunk_start` + `duration` within a file**: the
`_off{T}` offset defect (`lambda_speaker_embed.py:110`) is exactly that arithmetic done
wrong, and a batched turn's `source_filename` is the stitched WAV, not the device upload.

---

## 4. Schema and artifacts

1. **`redactions` gains `range_start_sec` / `range_end_sec`** (both NULL = the whole source =
   today's behaviour, so every existing row keeps meaning what it means). Keyed on
   `target_key` exactly as now.
2. **`redactions/{folder}/{date}/ranges.json`** — the S3 mirror of the removed spans per
   `session_base`, for the lambdas that have no database (report generator, ask agent). Same
   split and same pattern as `deleted_sessions.json`, including the two lessons that mirror
   learned the hard way: **merge, never overwrite** (a second span-delete on the same day must
   not free the first), and **read strictly** — an unreadable mirror must abort the write
   rather than clobber it (`deletion_mirror.MirrorUnreadable`).
3. **`extraction_requests/`** artifact carrying the session and its removed spans, which is
   what triggers the re-extraction.

---

## 5. The interim window — hide the whole session, and say so

Between the request committing and the re-extraction landing (**minutes** — it is an LLM
call) the entire session is hidden, not just the span.

This is deliberate and it is the only honest interim state: until the new extraction exists,
nothing can say which topics quoted the removed passage (§1.1). Showing a topic that *might*
contain it is precisely the failure requirement 1 exists to prevent.

The response says so explicitly, and the UI must show it — "removing… the rest of this
recording is temporarily hidden" — rather than letting the customer discover their other
content vanished.

**If the re-extraction never lands, the session stays hidden.** That is a silent, permanent
outage of real content, so it needs the same treatment the batching sweep got: a bounded
re-drive, a count logged including zero, and an alarm on sessions tombstoned with a range but
with no surviving extraction after N minutes. `SELECT count(*) FROM topics WHERE
source_s3_key LIKE '<prefix>%'` returning zero is the query that detects it.

---

## 6. Check-offs: reconcile explicitly, never carry over automatically

The re-extraction's action items are new rows. The old ones still exist (§2), so nothing is
lost — but they are attached to hidden topics, and the new ones start unticked.

**Do not match them automatically.** The model may reword ("周五前订钢材" → "安排周五的钢材
送货"), merge two into one, or split one into two. A confident wrong match puts a
supervisor's tick on a *different* action item, and no one will ever notice. A missing tick
is visible and recoverable; a moved tick is neither.

So: after the re-extraction lands, the person who deleted the span gets a reconciliation
list — "3 action items in this recording were ticked before; here is what they map to now" —
with the confident 1:1 matches pre-selected and the rest left for a human to decide. One
extra click, in exchange for removing a whole class of undetectable error.

`content_edits` and the DynamoDB `AUDIT#{date}` log both survive regardless, so "who closed
this and when" is always recoverable even if nobody reconciles.

---

## 7. What this does and does not remove

Stated plainly because requirement 1 is a trust requirement, and overclaiming is how trust
is actually lost.

**Removed:**
* the transcript turns in the span, everywhere the transcript is read;
* `transcript_window` RAG chunks built from them (`chunking.py:17`) — so search stops
  returning the passage;
* `topic` RAG chunks and the topic text itself, because the topics are replaced by the
  re-extraction rather than filtered (`chunking.py:13`);
* the day's report prose, which regenerates through the same masked turns;
* the stored `daily_report.json` is not served verbatim for a day with redactions — reusing
  the guard the whole-recording delete already has.

**Not removed, by design:**
* the audio and the original transcript objects on S3. Nothing is ever deleted there; that is
  the predecessor spec's §8 and it does not change.
* email already delivered.

---

## 8. Out of scope

* One gesture spanning several recordings. Per recording first.
* Photos. `topic_photos` CASCADEs from its topic and PhotoGrid has its own keyframe delete —
  a different mechanism that must not end up behind the same button.
* Any S3 deletion, ever.

---

## 9. Verification

Inherited from the predecessor's §9 (six surfaces: Evidence, search, Ask, daily report,
media/presign, nightly email), plus the ones specific to this:

* **A positive control first.** Search for the passage and get a hit, *before* deleting.
  Without it, "search returns nothing" afterwards proves nothing.
* After commit: that search returns nothing, **and** the rest of the day is visibly hidden
  too, matching what the response said.
* After the re-extraction: the surviving topics are back, the passage is not — check the
  topic text and the summary prose, not only search — and
  `SELECT count(*) FROM topics WHERE source_s3_key LIKE '<prefix>%'` is **non-zero**. Zero
  means the re-extraction failed and the session is hidden forever.
* The S3 object count under the session's prefixes is unchanged throughout.
* A ticked action item on a surviving topic: its old row still exists and still says ticked,
  and the reconciliation list offers it. (The predecessor's version of this check demanded it
  be *automatically* still ticked; §6 rejects that as the wrong guarantee.)
* Delete a second span on the same day and confirm the first is still hidden — the mirror
  merge, which the whole-recording delete got wrong on its first attempt.
* Undelete: the original topics come back with their check-offs intact, and the `rev{n}` rows
  are gone.
