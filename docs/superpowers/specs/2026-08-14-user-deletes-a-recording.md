# A user deletes recordings, and everything derived from them disappears

**Status:** proposed
**Requested:** 2026-08-14 — customer-facing, to be deployed on PROD.
**Explicitly temporary.** It must carry a marker that makes it findable and revertible in
one step; see §7.

---

## 1. What was asked for

A user selects recordings — A, B, C, D — and deletes them. Everything derived from those
recordings must stop being visible: topics, action items, tasks, findings, and anything a
search or the Ask agent could surface.

> "不能再被别人搜出来了造成信任危机"

Two constraints that shape the whole design:

* **The S3 objects are NOT deleted.** The audio, the transcripts and the extractions stay,
  because they are still wanted for analysis.
* **Nothing is hard-deleted from the database either** — the rows are archived, so the
  deletion is auditable and reversible.

So this is a *redaction*, not a delete. The word "delete" is what the customer sees; what
happens underneath is that the content stops being readable by anyone.

## 2. The mechanism already exists, and it is the wrong shape by exactly one thing

`redactions` (repositories/redactions.py, migration for life-conversation separation) is
already: a soft exclusion, reversible via `reverted_at`, audited with actor and role, and
routed through one named choke point — `company_excluded_topic_ids`.

Its docstring states the limitation that matters here:

> "The site/self tier does NOT use this."

It was built so a personal conversation could be hidden **from the company** while the
person it belongs to still sees it. What is being asked for now is the opposite: hidden from
**everyone**, the deleting user included.

**Design decision:** do not build a second mechanism. Add a `scope` value that means
*all tiers*, and make the tier-scoped and all-tier exclusions two values of one thing.
`create_redaction` already takes `scope`, defaulting to `'analysis'`; this adds `'deleted'`.

## 3. The derivation chain, and what "everything" means

```
recording  →  transcript  →  extraction (source_s3_key)  →  topic
                                                              ├─ action_items      (CASCADE)
                                                              ├─ findings          (CASCADE)
                                                              ├─ safety_observations (CASCADE)
                                                              ├─ topic_photos      (CASCADE)
                                                              ├─ topic_thread_suggestions (CASCADE)
                                                              ├─ report_chunks     (SET NULL)  ← RAG
                                                              └─ programme_progress_suggestions (SET NULL)
```

`topics.source_s3_key` is the join back to the recording, and it is already the key the
cleanup paths use (`topics.delete_by_source_key`). The children hang off `topic_id`.

**Redacting the topic is therefore sufficient for the children that CASCADE** — they are
only ever read through their topic. It is **not** sufficient for the two that `SET NULL`:
`report_chunks` is what RAG searches, and a chunk whose `topic_id` is nulled is an orphan
that outlives its topic. Those must be excluded explicitly, or the exact failure the request
names — "搜出来" — happens through the search box.

## 4. The real work is the read paths, not the write

`company_excluded_topic_ids` is described as "the single choke point every company-tier read
routes through". It has **two callers today** (`rollup.py:71`, and reindex). Meanwhile
`repositories/topics.py` alone holds **twelve** `FROM topics` reads — day lists, single
topic, date lists, search, threads — and `lambda_org_api` has its own.

A redaction that the write side records and the read side ignores is worse than no feature:
the customer is told the content is gone, and it is still there. **This is the same shape as
every silent failure this project has hit — a guard whose success is never positively
observed.**

So the acceptance is not "the endpoint returns 200". It is: for every read path that can
surface a topic, a redacted topic does not appear — proven by a test that enumerates the
read paths rather than a list someone maintained by hand.

## 5. What the user selects

The request says "在 evidence 里面 batch 选择". Note that `EMIT_EVIDENCE` is **false on
PROD**, so `topics.evidence` is NULL for every prod topic today and there is nothing to
select. The selection unit therefore has to be the recording/session, which is what the user
described anyway ("用户删了视频 A,B,C,D").

Evidence-level selection is a later refinement and is out of scope here.

## 6. Tiers

"客人和其他层级不再能看到" — a `scope='deleted'` redaction excludes at **every** tier:
company, site, self, platform_admin. The only readers that still see it are the offline
analysis paths that go to S3 directly, which is the point of not deleting the objects.

Note for whoever implements: `platform_admin` spans companies on graded read paths, and
every write endpoint has had to be taught span-all separately. The exclusion must be applied
where the rows are read, not where the caller's tier is decided, or the admin view becomes
the hole.

## 7. Temporary, and how it is recalled

Required by the request. Three things, all cheap:

1. **One feature flag**, `ENABLE_USER_DELETION`, wired repo-variable → workflow → template →
   the functions that read it. Off by default; turning it off makes the endpoint refuse and
   the read filters no-op, which un-hides everything in one deploy.
2. **Every row written carries `scope='deleted'`** and the redaction's own `id`, so the set
   of things this feature ever hid is one query:
   `SELECT * FROM redactions WHERE scope='deleted' AND reverted_at IS NULL`.
3. **A revert path that already exists** — `revert_redaction` sets `reverted_at`, keeping
   the audit row. Bulk revert is that query plus a loop.

The marker is deliberately in the data, not only in the code: a flag that is turned off
leaves no way to find what it did, and this feature will be asked about after it is gone.

## 8. What this must not do

* **Never delete an S3 object.** Not the audio, not the transcript, not the extraction.
* **Never hard-delete a database row.** `topics.delete_by_source_key` exists and is not to
  be used here — it is for cleanup of test artifacts.
* **Never let the write succeed while a read path still shows the content.** If the read
  filtering cannot be applied to a path, the endpoint refuses to redact rather than lying
  about it.

## 9. Verification

On PROD, after the first real use:

* the selected recordings' topics are absent from every list, search and Ask answer;
* the S3 objects are all still present (counted before and after);
* `SELECT count(*) FROM redactions WHERE scope='deleted'` matches what the user selected;
* one revert restores exactly what one delete hid, counted the same way.

A run that redacts nothing must say so — the count is the evidence that the path executed,
not the absence of an error.
