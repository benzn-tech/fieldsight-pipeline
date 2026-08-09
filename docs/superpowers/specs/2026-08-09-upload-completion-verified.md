# Uploads are marked complete without checking the bytes arrived

**Status:** spec · 2026-08-09
**Scope:** `lambda_org_api.complete_recording` + `repositories/recordings.mark_uploaded`. No mobile change.

## The defect

`POST /org/recordings/{id}/complete` takes the client's word. `mark_uploaded` runs

```sql
UPDATE recordings SET uploaded_at=now(), size_bytes=COALESCE(...) WHERE id=%s AND company_id=%s
```

with no check that anything exists at `s3_key`. The endpoint returns 200 whether or not the
upload happened.

That would be merely optimistic if the client also verified. It does not — and it explicitly
relies on the backend to be the verifier. `UploadWorker.doWork` deliberately ignores the
result of the PUT:

```kotlin
// ... If the object is absent the backend just stays pending
// and we retry the PUT on the next attempt.
// Return value deliberately ignored — `complete` below is the real
// verdict on whether the object made it (see the comment above).
client.putFile(urlResult.uploadUrl, contentType, file)
val status = client.completeStatus(idToken, urlResult.recordingId, file.length(), ...)
```

**The contract that comment describes was never implemented.** A PUT that fails is followed by
a `complete` that returns 200, the row is stamped `uploaded_at`, the worker marks the local
record `uploaded`, and the retry the comment promises never happens. The bytes are gone.

Ignoring the PUT result is itself correct and should stay — a client-side give-up does not
mean S3 rejected the body, and that assumption is what stranded 69 recordings on 2026-08-03
(present in S3, row never completed). The bug is that the other half of the design is missing.

## Evidence

Every `recordings` row on prod claiming `uploaded_at`, diffed against a full listing of
`s3://fieldsight-data-509194952652/users/`:

| path | missing / claimed | |
|---|---|---|
| chunk-session (`_sid`, the current recording path) | **6 / 684 (0.9%)** | all 2026-08-07 |
| gallery-style names (`VID_`/`IMG_`/`AUD_`) | 61 / 61 (100%) | one-off 2026-07-13 import |
| legacy video/pictures | 74 / 193 (38%) | mostly July |
| **total** | **141 / 938 (15%)** | |

The missing objects **never existed**, they were not deleted later:

- bucket versioning has been on since **2026-07-04** (CloudTrail `PutBucketVersioning`,
  `GitHubActions`) — before every missing row — and surviving objects from those same days
  carry real version IDs, not `null`
- each missing key has **zero versions and zero delete markers**
- no lifecycle rule touches the `users/` prefix (the three rules cover `transcripts/`,
  `pending_downloads/`, `voice/`)

The timing signature matches a stalled PUT followed by an accepted `complete`: for the
chunk-session losses the created→uploaded lag is a median of **55s**, against **2s** for rows
whose object is present.

## Fix

Make the backend behave the way the client already assumes.

`complete_recording` verifies the object before `mark_uploaded`. Absent → **409**, and the row
stays pending. The mobile side needs no change: `isTransient` is `NO_RESPONSE || 429 || 5xx`,
so a 409 falls into the worker's final branch, which is `markUploadStatus("failed")` +
`Result.retry()` — the PUT is retried inside the existing 7-day age budget.

### Verify with `list_objects_v2`, not `HeadObject`

`HeadObject` is the obvious call and it is the wrong one here. S3 answers **403, not 404**,
for a key you cannot see when the caller lacks `s3:ListBucket` on the bucket — and the
org-api role's ListBucket grant is conditioned on `s3:prefix`:

```json
{"Action":"s3:ListBucket","Resource":"arn:aws:s3:::fieldsight-data-509194952652",
 "Condition":{"StringLike":{"s3:prefix":["programmes/*","transcripts/*",
   "audio_segments/*","web_video/*","users/*"]}}}
```

A `HeadObject` request carries no `s3:prefix`, so the condition cannot be satisfied and a
missing object is indistinguishable from a broken permission. Measured with
`simulate-principal-policy` on the live role, not read off the template:

| call shape | `s3:ListBucket` decision |
|---|---|
| with `s3:prefix=users/…` (what `list_objects_v2` sends) | **allowed** |
| with no prefix (what `HeadObject` sends) | implicitDeny |

`s3:GetObject` on `users/*` is allowed either way. So verification uses
`list_objects_v2(Bucket, Prefix=key, MaxKeys=1)` with an exact key comparison: it fits the
permission that already exists, needs no IAM change, and its failure mode is an explicit
`AccessDenied` exception rather than a 403 that means two different things.

This is the third recurrence of the 403-vs-404 confusion (BUG-43, PR #288). The reason it
keeps landing is that the safe-looking reading — "it threw, so treat it as absent" — is the
one that turns a permission slip into a total outage.

### Three states, not a boolean

`UPLOAD_VERIFY_MODE`, wired through all three segments (repo variable → workflow
`--parameter-overrides` → template Parameter), because a switch that only exists in code
takes its default forever and no error is raised.

| mode | on absent | why |
|---|---|---|
| `off` | 200 (current behaviour) | rollback |
| `observe` | 200, but log the verdict | evidence with zero risk to a live recording day |
| `enforce` | 409, row stays pending | the fix |

Prod ships **`observe` tonight**, deliberately. The morning manual test matters more than
closing this hole one day sooner: the failure mode of a wrong `enforce` is *every* upload
rejected, against a measured 0.9% loss if we wait. `observe` produces the log line that says
which one prod would have done, and `enforce` follows once a real recording day has passed
through it. Test ships `enforce` immediately.

Any unexpected exception from S3 degrades to accept-and-log in every mode. Verification is a
guard, and a guard that cannot read its input must not be the thing that stops uploads.

### Size is logged, never enforced

The client sends `file.length()` of the exact file it PUT, so a size that disagrees with the
object's `Size` means a truncated upload — real, silent quality loss. It is logged with the
verdict but does not reject in this change: a systematic off-by-something in how either side
counts would reject every upload, and that risk is not worth taking in the same release that
introduces the existence check. Enforcement is a follow-up once the observe logs show the two
numbers agree in practice.

### The 141 existing rows are not rewritten

Clearing `uploaded_at` on them would not cause a re-upload — the worker's state lives in the
device's own database, and for the July rows the files are long gone from the devices. It
would only churn prod data. They stay as they are; `scripts/missing_chunk_audit.py` already
reports the holes honestly, and the diff in this document is reproducible.

## How this is verified

1. Unit tests over `complete_recording` with a faked S3 client: object present → 200 and
   `uploaded_at` set; absent under `enforce` → 409 and `uploaded_at` still NULL; absent under
   `observe` → 200 plus the log line; `AccessDenied` → 200 in every mode; size mismatch →
   logged, never rejected.
2. A wiring test already pins that every `--parameter-overrides` name exists in the template
   and that both workflows pass every boolean Parameter
   (`test_template_workflow_parameter_wiring.py`).
3. On the deployed function, **read the env** to confirm `UPLOAD_VERIFY_MODE` is present with
   the intended value — the switch existing in the repo is not evidence it reached Lambda.
4. After the morning recording, count the observe verdicts in CloudWatch and re-run the
   S3-vs-database diff. `enforce` on prod is gated on that count being explainable.
