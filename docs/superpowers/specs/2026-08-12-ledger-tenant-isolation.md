# TEST writes into PROD's transcript ledger

Status: spec, 2026-08-12. Reviewed adversarially; the review changed four things and
is credited inline where it did.

Found while measuring batching, not while looking for it.

## The fact

`fieldsight-transcripts` is the only transcript ledger that exists. There is no
`fieldsight-test-transcripts`. Neither deploy workflow overrides
`TranscriptTableName`, so both stacks resolve the template default and both write to
the same table.

```
$ grep -rn TranscriptTableName .github/workflows/     # no matches
$ aws lambda get-function-configuration --function-name fieldsight-test-transcribe \
    --query 'Environment.Variables.TRANSCRIPT_TABLE'  # fieldsight-transcripts
```

Three functions per stage are given it in the template — `TranscribeFunction`
(`template.yaml:966`), `TranscribeCallbackFunction` (`:1138`), `FinalizeSweepFunction`
(`:2110`) — read by `lambda_transcribe.py:74`, `lambda_transcribe_callback.py:41`,
`lambda_finalize_claim.py:43`. Note prod's sweep has **no `TRANSCRIPT_TABLE` env yet**:
prod received the batching template only in tonight's release, so "three per stage" is
template-true and not yet live-true for prod.

It is not theoretical. A scan of the prod table found `BATCH#` rows that only TEST
could have written — prod has never had batching enabled, so nothing else writes them.

**Snapshot, 2026-08-12 ~11:40 NZ:** 17 rows in **one** session (`be4190…`, 13 `CHUNK#`
+ 4 `SEAL#`, all `sealed`). Table total 978 items.

**This number moved during the writing of this spec.** An earlier scan the same evening
found 37 rows across 4 sessions, including a `SEAL#0004` stuck at `status=sealing`.
Those ~20 rows were deleted by another session cleaning up its test artefacts, along
with the matching transcripts in the test bucket. Two things follow: the stuck-seal
exhibit no longer exists, and **someone already did the thing scope item 5 below argues
against** — reaching into prod's table to tidy a test's mess. That is not a reprimand;
it is the clearest possible demonstration of why the table should not be shared. When a
number here disagrees with the table, trust the table.

The table is unmanaged: no CloudFormation tags, belongs to no stack
(`list-tags-of-resource` → `[]`), which is why nothing ever flagged the sharing.

## Why it matters, stated precisely

The rows are bookkeeping, not content: which chunks belong to a batch, and one `SEAL#`
row per run acting as a claim so two workers cannot concatenate the same batch twice.

- **It is a claim with a 15-minute takeover, not a permanent mutex.**
  `batch_ledger.claim_seal` (`:130-162`) first tries
  `put_item(ConditionExpression="attribute_not_exists(SK)")`; on failure it re-reads,
  and if the existing claim is `sealing` and older than `SEAL_RETRY_SECONDS=900` it
  overwrites unconditionally. Session ids are random, so cross-environment collision
  remains vanishingly unlikely — the correctness argument survives.
- **The takeover only fires while a session is still `pending_close`.**
  `_seal_tail_batches` is called from one place (`lambda_finalize_claim.py:603`), inside
  the loop over `list_due_finalize`, which selects `status = 'pending_close'`
  (`repositories/meeting_session.py`). After a session finalizes it is never revisited,
  so a claim stranded at that moment is stranded for good. The review asserted the sweep
  would re-drive it after 15 minutes; that is true only before finalize, and the stuck
  row actually observed tonight belonged to an already-finalized session.
- **The blast radius is the real argument.** A test-side bug that deletes or rewrites
  ledger rows reaches prod's table — and tonight's ~20-row deletion shows that is a
  live habit, not a hypothetical.
- **Capacity and cost are prod's.** PAY_PER_REQUEST, so test traffic bills prod's table.
- **Investigation is poisoned.** "Query prod's ledger to see what prod did" is not a
  question this table can answer, and the person asking will not know that.

What this is **not**: data loss, and not a reason to hold tonight's release. Batching is
off in prod (`PROD_BATCH_TRANSCRIPTION` unset), so prod writes no `BATCH#` rows today.
The window to fix it cheaply is exactly now — before that switch is flipped and prod's
rows start interleaving with test's.

## Constraints that shape the fix

1. **The table predates the stack and is referenced by name**, like `ItemsTableName` /
   `ReportsTableName` / `AuditTableName`. BUG-34: declaring an existing resource fails
   the deploy, so prod's table must stay external.
2. **The deploy role cannot create tables.** `template.yaml:2126-2129` records it
   already: *"the deploy role has no `dynamodb:CreateTable` (verified with
   simulate-principal-policy), so a new table would CREATE_FAILED and roll the whole
   stack back."* This is decisive — it rules out declaring the test table in the
   template, and it means creating it needs credentials that are not the deploy role.
3. **IAM follows the parameter.** All three grants are
   `DynamoDBCrudPolicy: TableName: !Ref TranscriptTableName` (`:987`, `:1143`, `:2136`)
   — by ref, not by wildcard — so a workflow override rewrites the env var and the
   policy in the same deploy. There is no separate grant to remember.
4. **Create must precede deploy, and the failure mode if it does not is silent.**
   Nothing in the template requires the table to exist, so CFN succeeds either way. At
   runtime `register_chunk` against a missing table raises, the per-key `except` at
   `lambda_transcribe.py:496` swallows it, and the chunk is **neither batched nor
   transcribed**. Ordering is not a nicety here; getting it wrong loses audio quietly.

## Options

**A. Out-of-band table + workflow override.** Create `fieldsight-test-transcripts`
(PK `S` hash, SK `S` range, PAY_PER_REQUEST, no GSI, no TTL — matching prod exactly),
add `"TranscriptTableName=fieldsight-test-transcripts"` to `deploy.yml` only.

**B. Stage-prefix the keys.** `PK = f"{STAGE}#BATCH#{session_id}"`. No new resource. But
both environments stay in one table, so blast radius and billing are unchanged; every
reader and writer must change in step; and the rows already written are orphaned.

**C. Declare the test table in the template under a condition.** **Impossible**, per
constraint 2 — CREATE_FAILED would roll the whole stack back.

**Recommended: A**, and the review strengthened rather than weakened this: C is not
merely inelegant but unavailable, and B leaves every argument above intact except
"unmanaged resource".

**The honest cost of A** is that it can half-land. `UsersTableName` (`template.yaml:398`)
is shared exactly like this one, a `fieldsight-users-test` table exists live, and
**nothing wires it** — a table created for a split that never happened. That is A's
failure mode already sitting in this account. The wiring test in step 3 is the whole
defence against becoming the second example.

## Scope

1. Create `fieldsight-test-transcripts` matching prod's schema. **Not with the deploy
   role** (no `CreateTable`). Do this **before** the workflow change merges.
2. `deploy.yml`: add `"TranscriptTableName=fieldsight-test-transcripts"`.
3. Test in `test_template_workflow_parameter_wiring.py` pinning that **the two workflows
   do not pass the same table name** — same shape as the ElevenLabs-key test, which
   exists because a shared key silently spent prod's quota. This is what stops A
   half-landing.
4. `src/deploy_transcribe_callback.sh:34` hardcodes `TABLE_NAME="fieldsight-transcripts"`
   and sets the callback's env directly (`:167`, `:178`, `:273`). Running that legacy
   script against the test callback after the split silently re-points it at prod's
   table. Either make it take the table as an argument or make it refuse to run against
   a `-test-` function.
5. CLAUDE.md: record the new out-of-band resource beside the S3 CORS rule and the
   DynamoDB VPC endpoint.
6. Leave the remaining 17 rows. They are read-safe: there is no `.scan(` anywhere in
   `src/`, and both readers Query a full PK (`batch_ledger.py:85,172`;
   `lambda_transcribe_callback.py:83`), so nothing can encounter a foreign row by
   accident. Record the cleanup query in the doc rather than running it.

**Cutover caveat.** Do the switch when no TEST session is mid-batch. A `sealing` claim
left in prod's table at the moment of the split is orphaned by definition — test's sweep
will look in the new table and find nothing — and per the finalize note above, nothing
re-drives it. The cost is one session's tail going untranscribed.

## How it will be verified

Not by reading the template:

```
aws lambda get-function-configuration --function-name fieldsight-test-transcribe \
  --query 'Environment.Variables.TRANSCRIPT_TABLE'           # fieldsight-test-transcripts
aws lambda get-function-configuration --function-name fieldsight-test-transcribe-callback \
  --query 'Environment.Variables.TRANSCRIPT_TABLE'           # fieldsight-test-transcripts
aws lambda get-function-configuration --function-name fieldsight-test-finalize-sweep \
  --query 'Environment.Variables.TRANSCRIPT_TABLE'           # fieldsight-test-transcripts
aws lambda get-function-configuration --function-name fieldsight-prod-transcribe \
  --query 'Environment.Variables.TRANSCRIPT_TABLE'           # fieldsight-transcripts
```

Then a TEST batching run must add `BATCH#` rows to the **test** table and none to
prod's — scan both and compare counts before and after, because "it worked" and "it
wrote to the wrong table" look identical from the application's side.

## Closed

The earlier open question about `TranscribeCallbackFunction` and the 961 `DATE#` rows is
closed. They are written only at AWS-Transcribe job start (`lambda_transcribe.py:482`,
skipped on the ElevenLabs path at `:452-457`) and read only by the callback querying
`DATE#{date}` for the job it is handling. Nothing reads them historically or
cross-stage, and the S3-scan fallback is independent of the ledger (`:295-296`). Both
stages run `ASR_PROVIDER=elevenlabs`, so the path is dormant besides.
