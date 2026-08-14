# Deleting part of a recording

**Status:** proposed. Nothing here is built.
**Requested:** 2026-08-14, immediately after the whole-recording delete shipped to `dev`.
**Predecessor:** `2026-08-14-user-deletes-a-recording.md`, whose §5 deferred exactly this
and called it "a later refinement". This is that refinement, written up so the cost is
visible before anyone commits to it.

---

## 1. What was asked for

> 仍然没法删掉某一特定时间段内的语音内容 … 在 evidence → audio/video 里面用时间块
> sorting 内容,然后客人可以 batch select 不想要的内容删掉

Correct on both counts. The shipped endpoint's unit is one recording — one
press-record → stop. A customer who said one sentence they regret has to delete the whole
forty-minute walk to remove it.

The UI half of the request has shipped without this: `/evidence` → Audio and Video now
group each day into per-recording blocks headed by their time span, with batch select and
delete. The **selectable unit is still the whole block**, and individual clips are
deliberately not given checkboxes rather than given ones that refuse.

## 2. Why the shipped mechanism cannot be extended by adding a parameter

The tombstone is a **source prefix**: `extractions/{folder}/{date}/{session_base}`, matched
with `LIKE` (`deleted_predicates.DELETED_SOURCE_PREDICATE`). It has no time dimension, and
adding two columns to `redactions` would not be the hard part.

The hard part is that **the thing which has to disappear is the topic, and nothing reliably
maps a clock range onto topics.**

* A topic's only time field is `time_range`. `repositories/topics.py:68` calls it a display
  field; `session_scope.py`'s module docstring is blunter — it is **LLM free text**, its
  `HH:MM – HH:MM` shape is enforced only by the extraction prompt, and *"It must NEVER
  decide which session a topic is in."* Using it to decide which topics fall inside
  10:15–10:22 is precisely the use the pipeline forbids, and it would fail silently: a
  malformed or hallucinated range yields a confident wrong answer, not an error.
* Even with a perfect map, hiding topics is not enough. The day's `daily_report.json`
  prose, the executive summary, the findings and the RAG chunks were all generated **from
  the whole session**. Removing the audio for 10:15–10:22 does not unwrite the sentence in
  the summary that came out of it.

So partial deletion is not a filter. It is **re-derivation from the surviving audio**.

## 3. The shape that follows from that

One delete action becomes:

1. **A ranged tombstone.** `redactions` gains `range_start_sec` / `range_end_sec` (NULL =
   the whole source, i.e. today's behaviour, so existing rows keep meaning what they mean).
   Keyed on `target_key` exactly as now.
2. **Immediate hiding at the session level.** Every topic under that source is hidden the
   moment the request commits — the same rows today's delete hides. This is deliberately
   coarser than what the customer asked for, and it is the only honest interim state: until
   step 4 lands we cannot say which topics survive, and showing a topic that *might* quote
   the removed passage is the failure the whole feature exists to prevent. The response
   says so.
3. **A redaction manifest in S3**, `redactions/{folder}/{date}/ranges.json`, listing the
   removed spans per `session_base`. The non-VPC lambdas (report generator, ask agent) have
   no database — this is the same split, and the same mirror pattern, as
   `deleted_sessions.json`.
4. **Re-extraction of the surviving audio.** An `extraction_requests/` artifact for that
   session carrying the removed spans; `lambda_extract_session` drops the transcript turns
   inside them before building its prompt, and writes to the **same S3 key** (the existing
   idempotent overwrite + `delete_topics_for_source`). New topics land under new uuids that
   no tombstone names, so the session un-hides itself when the clean extraction commits.
5. **A regenerated day report**, as the whole-recording path already enqueues.

Undelete removes the ranged rows, rewrites the manifest, and re-extracts again — the
original audio is still on S3, which is what makes this reversible at all.

## 4. What this costs, stated plainly

* **The window is not instant.** Between step 2 and step 4 the customer's other content
  from that session is hidden too. Minutes, not seconds — extraction is an LLM call.
* **Re-extraction is not free and not identical.** The same audio minus a span produces a
  *different* extraction, not the old one with a hole: topic boundaries move, counts
  change, action items may merge or split. Anything a person already ticked off, threaded,
  or linked to a programme task hangs off topic uuids that will not exist afterwards.
  **This is the single largest hazard in the feature** and it does not exist in the
  whole-recording path, where the deleted topics simply stay hidden. Confirmed threads
  (`topic_thread_suggestions`), resolved compliance marks and programme impact links all
  need a re-attachment story before this ships, or the feature quietly discards a
  supervisor's work every time it runs.
* **Transcript turns are addressable; extraction inputs are the join.** A turn carries
  `source_filename` + `chunk_start` + `duration` — the same triple the speaker-correction
  write uses — so "which turns fall in this span" is exact and cheap. That is the one part
  of this that is easy, and it is why step 4 filters turns rather than topics.
* **The known `_off` offset defect applies here too.** For a non-batched segment whose
  filename carries `_off{T}` with `T > 0`, `lambda_speaker_embed.py:110-115` slices at
  `chunk_start` without adding the segment's own offset. Any new code that maps a clock
  span onto in-file audio must not copy that arithmetic.

## 5. A cheaper alternative worth pricing first

If the real requirement is "a specific sentence must stop being readable and searchable",
**redacting the transcript turns and the topics that quote them** achieves that without
re-extraction: hide the turns (exact, by `source_filename` + offsets), hide any topic whose
text contains material from them, and re-index. The audio for that span stays retrievable
by anyone with S3 access, and the report prose is still whatever it was.

That is weaker than what was asked for and it should not be described as deletion. But it
is days rather than weeks, it destroys nothing a person already did, and for the stated
motivation — 不能再被别人搜出来 — it may be the whole answer. **Decide between §3 and §5
before anyone writes code**; they share almost nothing.

## 6. Out of scope

* Deleting a span across several recordings in one gesture. Per recording first.
* Photos. `topic_photos` CASCADEs from its topic and PhotoGrid already has its own keyframe
  delete — a different mechanism that must not end up behind the same button.
* Any actual S3 deletion. Unchanged from the predecessor spec §8: never.

## 7. Verification this must be held to

Inherited from the predecessor's §9, plus:

* a positive control **before** — a search that returns the passage being removed;
* after step 2, that search returns nothing, and the rest of the day is *visibly* hidden
  too, matching what the response said would happen;
* after step 4, the surviving topics are back, the removed passage is not, and
  `SELECT count(*) FROM topics t WHERE t.source_s3_key LIKE '<prefix>%'` is non-zero — a
  zero there means re-extraction failed and the session is hidden forever;
* the S3 object count under the session's prefixes is unchanged throughout;
* one ticked action item on a surviving topic is still ticked afterwards. If that cannot be
  made true, §3 is not ready to ship and §5 is the answer.
