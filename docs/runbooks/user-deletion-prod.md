# Runbook — customer-facing recording deletion (PROD)

Feature: `docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md`
Plan: `docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md`

**This feature is temporary by request.** Everything below is written so that a person who
has never seen it can find what it did, prove it did nothing destructive, and undo it.

---

## 0. What it is in one paragraph

A customer selects recordings in Evidence and deletes them. Every topic, action, task,
search hit, media file, presigned URL, report and RAG answer derived from those recordings
stops being visible — to them, to their colleagues, and to every tier above them. **No S3
object is deleted and no database row is dropped.** The raw data stays for analysis. The
hiding is a set of tombstone rows in `redactions`, and one row per delete action carries a
`batch_id` so a single delete can be undone as a unit.

## 1. The marker — how to find everything this feature ever did

The marker is a column, not a feature flag. A flag that is turned off leaves no way to find
what it did while it was on.

```sql
-- everything currently hidden by this feature, newest first
SELECT batch_id, company_id, target_type, target_key, actor_sub, created_at
FROM redactions
WHERE scope = 'deleted' AND reverted_at IS NULL
ORDER BY created_at DESC;

-- one delete action
SELECT * FROM redactions WHERE batch_id = '<batch-id>';

-- what has already been undone
SELECT batch_id, count(*), max(reverted_at)
FROM redactions WHERE scope = 'deleted' AND reverted_at IS NOT NULL
GROUP BY batch_id;
```

The S3 side leaves a marker too — one small JSON per affected day:

```
s3://<bucket>/redactions/{folder}/{date}/deleted_sessions.json
```

`aws s3 ls s3://<bucket>/redactions/ --recursive` lists every day this feature has touched.

## 2. Turning it on

The switch gates the **write** endpoints only.

```bash
gh variable set PROD_ENABLE_USER_DELETION --repo benzn-tech/fieldsight-pipeline --body "true"
# then run the prod deploy (merge to main, or re-run the deploy-prod workflow)
```

Confirm it landed by reading the live function, not the deploy record:

```bash
aws lambda get-function-configuration --function-name fieldsight-prod-org-api \
  --query 'Environment.Variables.ENABLE_USER_DELETION' --output text   # -> true
```

A deploy that reports success while the variable is unset is the exact failure this repo
has shipped before; the only proof is the function's own environment.

## 3. Turning it off — and what "off" does NOT do

```bash
gh variable set PROD_ENABLE_USER_DELETION --repo benzn-tech/fieldsight-pipeline --body "false"
```

**Setting the flag to `false` does not un-hide anything, and that is deliberate.** The read
filters are unconditional. If they followed the flag, flipping it off would republish every
recording a customer was told was gone — a second incident, not a rollback.

To actually restore content, use the undelete path (§5). To restore *everything* this
feature ever hid:

```sql
UPDATE redactions SET reverted_at = now()
WHERE scope = 'deleted' AND reverted_at IS NULL;
```

and then delete the S3 mirrors (`aws s3 rm s3://<bucket>/redactions/ --recursive`), because
the non-VPC lambdas read those and have no database.

## 4. Verifying it works (do this on a real recording you own)

1. **Before.** Open Evidence, note a session's topics/actions and that its audio plays.
2. **Delete.**
   ```bash
   curl -sS -X POST "$ORG_API/api/org/recordings/delete" \
     -H "Authorization: $ID_TOKEN" -H 'Content-Type: application/json' \
     -d '{"recordings":[{"folder":"<Folder>","date":"2026-08-14","sessionBase":"<base>"}],
          "reason":"runbook verification"}'
   ```
   Record the returned `batch_id`.
3. **After — check every surface, not just the one you deleted from.** Each of these has
   its own code path, and covering four of five is how a leak ships:
   - the Evidence list and the topic detail
   - **search** (search for a phrase you know is in that recording)
   - **Ask / RAG** (ask a question only that recording answers)
   - the **daily report** page for that day
   - **media playback** and any presigned URL you had open — it must 404, not 403
   - the **nightly report email** for that day (the generator is non-VPC and reads the S3
     mirror, so this is the surface most likely to lag)
4. **Prove nothing was destroyed.**
   ```bash
   aws s3 ls s3://<bucket>/audio_segments/<Folder>/<date>/ | grep <base>   # still there
   ```
   ```sql
   SELECT count(*) FROM topics WHERE source_key LIKE 'extractions/<Folder>/<date>/<base>%';
   -- still non-zero: the rows are hidden, not deleted
   ```
5. **Undo, and check it comes back.**
   ```bash
   curl -sS -X POST "$ORG_API/api/org/recordings/undelete" \
     -H "Authorization: $ID_TOKEN" -H 'Content-Type: application/json' \
     -d '{"batchId":"<batch-id>"}'
   ```
   Re-check the same six surfaces. What comes back must be exactly what went away — no
   more (another batch's content) and no less.

## 5. Undoing one customer's delete

```bash
curl -sS -X POST "$ORG_API/api/org/recordings/undelete" -H "Authorization: $ID_TOKEN" \
  -H 'Content-Type: application/json' -d '{"batchId":"<batch-id>"}'
```

The endpoint is company-guarded: one company's revert cannot resurrect another's content.
It also skips already-reverted rows, so running it twice does not double-count.

If the endpoint is unavailable, the SQL equivalent is:

```sql
UPDATE redactions SET reverted_at = now()
WHERE batch_id = '<batch-id>' AND company_id = '<company-id>' AND reverted_at IS NULL;
```

followed by rewriting or deleting the affected `deleted_sessions.json` mirrors.

## 6. Removing the feature entirely

It is temporary, so this is a real step and not a hypothetical:

1. Revert every active tombstone (§3) and clear the S3 mirrors.
2. Revert the phase PRs (they are all titled `feat(deletion): …`), or set
   `PROD_ENABLE_USER_DELETION=false` and leave the read filters — they are no-ops once no
   `scope='deleted'` row exists.
3. `redactions` keeps `batch_id` and `target_key`; both are nullable and unused by the
   pre-existing `analysis` redactions, so leaving them costs nothing and dropping them is
   the only irreversible act in the whole design. Prefer leaving them.

## 7. Things that will look like a bug and are not

| symptom | why |
|---|---|
| Flag turned off, content still hidden | By design — see §3. Undelete is the rollback. |
| Deleted content still in an email sent minutes earlier | Email is already delivered; only future sends are filtered. |
| The stored `daily_report.json` for that day still contains the deleted content | Nothing regenerates it, by design — the *serve* path refuses to hand back a pre-deletion report verbatim for a day that has a deletion, and the renderer drops deleted rows. The object on S3 is deliberately left intact, because this feature destroys nothing. |
| Presigned URL returns 404 rather than 403 | Deliberate: 403 tells the caller the object exists. |
| `redactions` row count grows and never shrinks | Reverting sets `reverted_at`; rows are never dropped. That is the audit trail. |
