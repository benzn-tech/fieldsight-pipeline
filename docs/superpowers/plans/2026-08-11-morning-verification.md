# Morning verification — 2026-08-11

What went to prod on the night of 2026-08-10/11, and how to tell — in the morning, before
trusting anything — whether each piece actually landed and did no harm.

The 2026-08-09 version of this document was never run to completion. This one replaces it
for tonight's changes; the ElevenLabs-quota warning at the top of that file still applies and
is not repeated here.

**Read the live state, never the deploy record.** Every check below reads the deployed
Lambda, S3, or the database. A green workflow run is not evidence: a parameter can be absent
from the workflow and take its template default forever, with no error anywhere. That has
shipped twice.

---

## 0. Did the prod deploy actually happen?

The deploy for PR #359 was **merged to `main` and then paused at the `production` approval
gate**. Nothing below is true until someone approved it.

```bash
gh run list --repo benzn-tech/fieldsight-pipeline --branch main --limit 5 \
  --json name,status,conclusion,databaseId
```

Expect the `Deploy FieldSight PROD (SAM)` run to be `completed / success`. If it is
`waiting`, approve it and start again:

```bash
gh api repos/benzn-tech/fieldsight-pipeline/actions/runs/<id>/pending_deployments \
  -F "environment_ids[]=18099403741" -f state=approved --method POST
```

If more than one prod deploy is queued (other sessions were merging tonight), they run one at
a time — `concurrency: deploy-prod, cancel-in-progress: false` — so a queued second run is
normal, not a fault. **A paused run deploys the commit it was created from, not the newest
`main`.** If `main` moved while it waited, the deploy that finishes is not the code you last
merged. Check the run's commit sha against `origin/main`.

---

## 1. Group merge is on (this is the night's one behavioural change)

```bash
for f in fieldsight-prod-item-writer fieldsight-prod-session-finalize fieldsight-prod-ingest; do
  echo -n "$f "
  aws lambda get-function-configuration --function-name $f \
    --query 'Environment.Variables.ENABLE_GROUP_MERGE' --output text
done
```

Expect `true` on all three. Anything else means the repo variable
`PROD_ENABLE_GROUP_MERGE=true` did not reach CloudFormation, and the flip did not happen.

**It is inert until a new multi-device recording exists.** As of tonight prod's
`session_group` table is empty, and the only group that has ever existed (3 sessions,
2026-08-07) predates the row that queues a merge, so nothing will be retro-merged. Seeing no
merge activity in the morning is the expected result, not a failure.

Three fixes shipped with it and they are the reason it could not be turned on earlier:
re-arming used to leave the group invisible to every scan (#346); an empty merge used to
delete the records it replaced (#348); and the "updated" email was written with **no
recipient at all**, so the worker skipped it *before* writing a result — the member got no
email and nothing anywhere recorded that (#363). If a merge does run and produces nothing,
check that the members kept their own topics — that is #348's guarantee.

If a merge runs and you expect emails, the thing to look for is the log line
`updated-email: no recipient for member … — not enqueued`. It is now loud on purpose: the
previous behaviour was the same silence with no trace.

One thing to know about #363's shape, in case it misbehaves: `lambda_item_writer` now
imports `_resolve_context` from `lambda_finalize_claim`. That is safe under a SAM deploy —
every function is built from `CodeUri: src/`, so the whole tree is in each package, and
`lambda_finalize_claim` has no module-level work beyond `os.environ.get` with defaults. It
would **not** be safe under a single-entry hot-fix zip, which is a deployment technique this
repo has used before. If item-writer ever starts failing on the group path with an
`ImportError`, that is the reason.

---

## 2. Upload verification is observing, not enforcing

```bash
aws lambda get-function-configuration --function-name fieldsight-prod-org-api \
  --query 'Environment.Variables.UPLOAD_VERIFY_MODE' --output text
```

Expect **`observe`**. `enforce` in the morning would be wrong and should be rolled back
immediately (`PROD_UPLOAD_VERIFY_MODE=off`, redeploy): enforce is gated on GrandTime 0.6.4
being on the devices, because an older build classifies a `complete` 409 as operator-fixable
and freezes the upload instead of re-sending it.

After a real recording, count what enforce *would* have done:

```bash
aws logs filter-log-events --log-group-name /aws/lambda/fieldsight-prod-org-api \
  --filter-pattern '"upload-verify"' --start-time <epoch_ms>
```

Three shapes, and they mean different things:

| log line | meaning |
|---|---|
| `object absent at …` | this upload would have been rejected — a real loss, counted |
| `size mismatch … client said X, S3 has Y` | a truncated upload; logged, never rejected |
| `could not read … — accepting` | **the guard is blind.** Not "no problem" — a permissions or S3 fault, and the check is doing nothing |

The third is the one to look for. It is not a failure of the upload; it is the check silently
not working, which is exactly the shape that turned a permission slip into a total outage
before (BUG-43).

Then re-run the diff that produced the 141/938 figure and see whether the day added holes:

```bash
python scripts/missing_chunk_audit.py
```

`enforce` on prod is gated on the observe count being explainable — not on a date.

---

## 2b. Batching is present and OFF on prod

The whole batched-transcription feature (phases 1–6a) shipped with this release and is
**inert**: `BatchTranscription` defaults `'false'` in the template, both workflows fall back
to `'false'`, and `PROD_BATCH_TRANSCRIPTION` is unset.

```bash
aws lambda get-function-configuration --function-name fieldsight-prod-transcribe \
  --query 'Environment.Variables.BATCH_TRANSCRIPTION'          # expect: false
aws lambda get-function-configuration --function-name fieldsight-prod-finalize-sweep \
  --query 'Environment.Variables.BATCH_TRANSCRIPTION'          # expect: false
```

**`false` is the pass. The key being ABSENT is not** — that would mean the parameter never
reached Lambda, and the switch could not be turned on or off later without a code change.
Both readings look like "batching is off" and only one of them is.

On TEST it is **on**, and has run end to end on a real 6-minute recording: 13 chunks, three
batches of four plus a sealed tail, four transcripts, zero member transcripts, and a final
extraction whose topics land inside the true 16:50–16:56 window.

## 2c. The session picker shows an end time

Every chunk session used to render `12:11 – ?`, because the end-time lookup asked
`recordings` with a pattern that matches **0 of 87** chunk sessions while
`meeting_session.closed_at` sat unread in the row the picker already loads.

Open any day with a recording and read a row in the picker. Expect `HH:MM – HH:MM`.

- A `?` still appearing means the session genuinely has no `closed_at` — check the row
  before assuming the fix regressed.
- A span that crosses a day now renders `(+1d)`, and one that runs backwards renders the
  start alone. Both are deliberate: the two ends come from device clocks. Seeing `(+1d)`
  on a short meeting is a **clock** finding, not a display bug.
- **Those eight prod sessions were backfilled on 2026-08-13** and no longer carry the
  12-hour error, so this is no longer the first explanation to reach for. They were found
  with `opened_at > last_segment_at` — a session cannot start after it ends — and each was
  exactly 12 hours out, the device's NZ wall clock written into a column that stores UTC.
  Afterwards: zero rows NULL, zero impossible, zero whose NZ date disagrees with their
  segments. If a **new** session shows the same shape, it is a fresh instance of the
  BUG-37 family and worth reporting, not the known backlog.

## 2d. A merged meeting's email quotes something

Only reachable once a real two-device recording happens. The group artifact has no
top-level summary and nothing built one from its topics, so every member of every merged
meeting would have been emailed a record that quoted nothing. If a merge does run, open the
"updated" email and check the summary is not empty.

## 3. The seam de-duplication is not eating Chinese again

Two ASCII-normalisation bugs shipped and were fixed this week; both deleted content and both
were invisible to an all-English test. Worth one look at a real transcript with any CJK in
it: the seam between two chunks should read as continuous speech, not as a sentence that
stops mid-clause.

If a session has no CJK, this check proves nothing — say so rather than marking it passed.

---

## 4. The app on the devices

The devices must be running **0.6.4 (18)** or later before `enforce` can be considered.
0.6.4 is the first build that contains:

- `complete` 409 classified as retryable (the precondition for enforce)
- microphone-silence visibility
- the haptic vocabulary

Check the build actually on the device, not the tag in the repo — a build reaches a device
only by being installed. If the device shows an older build, `enforce` stays off regardless
of what the backend says.

---

## 5. Nothing else regressed

The rest of tonight's prod payload is meant to be invisible:

| change | what to look at | what "no harm" looks like |
|---|---|---|
| evidence matcher (#349–#352, #358, #360) | a fresh extraction's topics | citations present, not fewer than yesterday; a spliced quote checked fragment by fragment; a wrong hour no longer called fabrication |
| DashScope file transcription (#354) | nothing — contract tests only | no change |
| `opened_at` in UTC + action-table email (#345) | the confirmation email's subject and times | local NZ time, not a day off |

A report that renders is not evidence for any of these. Each one acts before rendering.

---

## What is deliberately still broken in the morning

- **0.9% of chunk uploads are still lost.** `observe` measures the hole; it does not close
  it. Closing it needs 0.6.4 on the devices, then `enforce`.
- **Speaker attribution.** Unchanged tonight. The Phase 0 gate — can two people at 6 m be
  told apart — has no material yet; the recording script is at
  `fieldsight-vad-check/2026-08-11-blockV-script/` and the analysis harness is
  `scripts/speaker_phase0.py` (run it on the raw *and* the normalised copy; §0.3 requires
  both and they have disagreed before).
- **Microphone silence.** The device now records the evidence; the backend does not yet read
  those fields.
