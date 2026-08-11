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

Two fixes shipped with it and both are the reason it could not be turned on earlier:
re-arming used to leave the group invisible to every scan (#346), and an empty merge used to
delete the records it replaced (#348). If a merge does run and produces nothing, check that
the members kept their own topics — that is #348's guarantee.

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
| evidence matcher (#349–#352) | a fresh extraction's topics | citations present, not fewer than yesterday; the "wrong hour" case no longer flagged as fabrication |
| DashScope file transcription (#354) | nothing — contract tests only | no change |
| `opened_at` in UTC + action-table email (#345) | the confirmation email's subject and times | local NZ time, not a day off |

A report that renders is not evidence for any of these. Each one acts before rendering.

---

## What is deliberately still broken in the morning

- **0.9% of chunk uploads are still lost.** `observe` measures the hole; it does not close
  it. Closing it needs 0.6.4 on the devices, then `enforce`.
- **Speaker attribution.** Unchanged tonight. The Phase 0 gate — can two people at 6 m be
  told apart — has no material yet; the recording script is at
  `fieldsight-vad-check/2026-08-11-blockV-script/`.
- **Microphone silence.** The device now records the evidence; the backend does not yet read
  those fields.
