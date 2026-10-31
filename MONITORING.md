# Monitoring & Debugging Guide

> Rewritten 2026-11-01. The previous version described the RealPTT orchestrator,
> downloader and Fargate path, which no longer exist (#1198). Resource names below are
> for prod (`fieldsight-prod-*`); TEST is the same with `fieldsight-test-*`.

Region: **ap-southeast-2**. Profile: `fieldsight-deployer`.

## Where things are

| What | Where |
|---|---|
| Stacks | CloudFormation `fieldsight-prod`, `fieldsight-test` |
| Functions | Lambda `fieldsight-<stage>-<name>` (list in `ARCHITECTURE.md`) |
| Logs | CloudWatch `/aws/lambda/fieldsight-<stage>-<name>` (14 to 90 day retention) |
| Alarms | CloudWatch alarms `fieldsight-<stage>-*`; email goes to `AlertEmail` through the stack's alert topic |
| Dashboard | the stack's `MonitoringDashboard` |
| Data | S3 `fieldsight-data-509194952652` (prod), `fieldsight-data-test-509194952652` (TEST) |
| Database | Aurora cluster `fieldsight-db-test-dbcluster-hywiixu8ihi9`, database `fieldsight` (prod) / `fieldsight_test` (TEST) |
| Deploys | GitHub Actions `Deploy FieldSight TEST (SAM)` and `Deploy FieldSight PROD (SAM)` |

## Alarms

| Alarm | Meaning |
|---|---|
| `transcribe-errors`, `transcribe-throttles` | ASR step failing or throttled |
| `extract-session-errors`, `item-writer-errors` | extraction or the Aurora write failing |
| `extraction-backlog`, `extraction-pending` | transcribed words not reaching extraction (see `lambda_extraction_backlog.py`) |
| `recording-stranded` | a recording never progressed past upload |
| `recording-segments-throttles` | the day-segments rebuild is being throttled |
| `report-errors` | report generator failing |
| `api-5xx` | org API returning server errors |

A throttled Lambda writes no log line, so an empty log group is not proof that nothing
happened: check the `Throttles` metric.

## First checks

```bash
export AWS_PROFILE=fieldsight-deployer AWS_DEFAULT_REGION=ap-southeast-2

# Recent errors in one function
aws logs tail /aws/lambda/fieldsight-prod-extract-session --since 2h --filter-pattern ERROR

# Did the S3 triggers survive the last deploy? (dry run prints the diff)
bash scripts/wire-s3-events.sh fieldsight-data-509194952652 prod ap-southeast-2

# Was a daily report produced? Per-user and per-company
aws s3 ls s3://fieldsight-data-509194952652/reports/<YYYY-MM-DD>/ --recursive | head

# After a deploy that removes resources: a DELETE_FAILED does not fail the update
aws cloudformation describe-stack-events --stack-name fieldsight-prod   --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`]'
```

`aws lambda invoke` returns `StatusCode: 200` even when the function returned an error:
read the payload's own `statusCode`/`error`. When passing `--payload`, add
`--cli-binary-format raw-in-base64-out`.

A 504 from the API gateway means the gateway's 29 s limit was hit; the Lambda may still
have finished.

## Common situations

- **A recording is uploaded but nothing appears.** Follow the prefixes in
  `ARCHITECTURE.md` in order (`users/` -> `audio_segments/` -> `transcripts/` ->
  `extractions/`) and find the first one with no object. That function's log has the
  reason.
- **Extraction stopped for everyone.** Usually the LLM vendor (quota, arrears, model
  name). `extraction-backlog` re-drives once it recovers.
- **Session never closed / no confirmation email.** `finalize-sweep` runs every minute;
  check its log, then `session-finalize`. TEST sends real SES email.
- **Company summary missing.** Reports are per company now
  (`reports/<date>/companies/<company_id>/`); the lake-wide `summary_report.json` is off.
- **Times look a day off.** Lambdas run in UTC; NZ dates go through `nz_time.py`.

## Cost

Use Cost Explorer filtered by the `fieldsight-*` stacks. Aurora is the floor cost; see
the scale-to-zero spec (`docs/superpowers/specs/2026-08-11-aurora-scale-to-zero-design.md`).
