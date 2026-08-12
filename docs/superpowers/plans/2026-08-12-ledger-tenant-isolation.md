# Plan — give TEST its own transcript ledger

Spec: `docs/superpowers/specs/2026-08-12-ledger-tenant-isolation.md`

Ordering is load-bearing. The table must exist before the workflow change deploys,
because a missing table does not fail the deploy — it loses audio silently
(`register_chunk` raises, `lambda_transcribe.py:496` swallows it, the chunk is neither
batched nor transcribed).

## Preconditions

- No TEST session mid-batch. A `sealing` claim left in prod's table at the moment of the
  split is orphaned: test's sweep will look in the new table, and nothing re-drives a
  claim once its session has finalized.
  Check: `aws dynamodb scan --table-name fieldsight-transcripts --filter-expression
  "begins_with(PK,:p) AND #s = :s" ...` for `status=sealing`, expect none.
- Credentials with `dynamodb:CreateTable`. The GitHub deploy role does **not** have it
  (`template.yaml:2126-2129`); the `fieldsight-deployer` user does — verified with
  `simulate-principal-policy`.

## Task 1 — create the table (before anything merges)

```
aws dynamodb create-table --table-name fieldsight-test-transcripts \
  --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST
```

Verify against prod's shape rather than against this command: same key schema, same
attribute types, PAY_PER_REQUEST, no GSI, no TTL.

## Task 2 — point TEST at it

`.github/workflows/deploy.yml`, beside the other three table overrides:

```
"TranscriptTableName=fieldsight-test-transcripts" \
```

`deploy-prod.yml` is **not** touched; prod keeps the template default.

Because all three grants are `DynamoDBCrudPolicy: TableName: !Ref TranscriptTableName`,
this one line moves the env var and the IAM policy together. There is no second grant.

## Task 3 — the test that stops this half-landing

In `tests/unit/test_template_workflow_parameter_wiring.py`: assert the two workflows do
not pass the same `TranscriptTableName`. Model it on the ElevenLabs-key test, which
exists for the identical reason.

This is the most important task in the plan. `fieldsight-users-test` exists live and
nothing wires it — a table created for a split that never happened. Without this test,
that is the likely fate of this one too.

Verify by deleting the override line and watching it go red.

## Task 4 — disarm the legacy script

`src/deploy_transcribe_callback.sh:34` hardcodes `TABLE_NAME="fieldsight-transcripts"`
and sets the callback's env directly (`:167`, `:178`, `:273`). After the split, running
it against the test callback silently re-points test at prod's table.

Make it refuse rather than make it clever: if the target function name contains
`-test-`, exit non-zero with the reason. A script nobody runs is not worth a parameter;
a script that quietly undoes a tenancy split is worth a guard.

## Task 5 — record the out-of-band resource

CLAUDE.md, beside the S3 CORS rule and the DynamoDB VPC endpoint: what the table is, why
it is not in any template (the deploy role cannot create tables), and that deleting it
breaks TEST batching silently.

## Task 6 — verify on live resources

After the TEST deploy:

```
for f in transcribe transcribe-callback finalize-sweep; do
  aws lambda get-function-configuration --function-name fieldsight-test-$f \
    --query 'Environment.Variables.TRANSCRIPT_TABLE' --output text
done                     # all three: fieldsight-test-transcripts

aws lambda get-function-configuration --function-name fieldsight-prod-transcribe \
  --query 'Environment.Variables.TRANSCRIPT_TABLE' --output text   # fieldsight-transcripts
```

Then the behavioural check, which is the only one that proves the split: count `BATCH#`
rows in both tables, run one TEST batching session, count again. Test's must rise; prod's
must not move. "It worked" and "it wrote to the wrong table" are indistinguishable from
inside the application.

## Not in scope

- Deleting the 17 remaining `BATCH#` rows from prod's table. Nothing scans, both readers
  Query a full PK, so they are inert. Reaching into prod's table to tidy a test's mess is
  the habit this whole change exists to end.
- `UsersTableName`, which is shared the same way. Same argument, different blast radius,
  and bundling them would make this change hard to reason about.
