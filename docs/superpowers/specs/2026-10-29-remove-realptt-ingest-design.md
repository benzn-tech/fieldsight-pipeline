# Remove the RealPTT ingest path (orchestrator, downloader, Fargate)

Date: 2026-10-29
Status: implemented in `chore/remove-realptt-ingest` (PR to develop)

## Intent

The original ingest path scraped recordings from RealPTT (`lambda_orchestrator`), fanned
them out to a per-file downloader Lambda (`lambda_downloader`), and fell back to a Fargate
task for files too large for Lambda (`fargate_downloader.py`, launched daily by an inline
`FargateTriggerFunction`). Devices now upload directly, so this path is dead. It is also
the last code that reads the frozen `config/user_mapping.json`. M5 (#1184) removed every
other reader.

Removing it:

- deletes code that no longer runs but still has to be kept passing;
- removes a daily Fargate-trigger invocation on prod that only logs "No pending downloads";
- drops two deploy secrets (RealPTT credentials) and two network parameters from every deploy;
- lets the owner delete `config/user_mapping.json` afterwards.

## Evidence (measured 2026-10-29, read-only)

| Fact | Value |
|---|---|
| `fieldsight-prod-orchestrator` invocations | ~427/week through early October, then stopped (week of 10-09: 244; none after ~10-12) |
| Orchestrator Nightly/Sweep EventBridge rules | DISABLED on prod and TEST |
| `fieldsight-prod-downloader`, `fieldsight-test-downloader` | 0 invocations in 90 days |
| `fieldsight-test-orchestrator`, `fieldsight-test-fargate-trigger` | 0 invocations in 90 days |
| prod `FargateTriggerFunctionFargateSchedule` | ENABLED, runs daily 07:30Z, logs `No pending downloads. Skipping Fargate.` |
| TEST Fargate schedule | DISABLED |
| `pending_downloads/` in the prod data bucket | 0 objects |
| Running ECS tasks in `fieldsight-prod-downloader-cluster` | 0 |
| ENIs using either Fargate security group | 0; no other SG references them |
| Stack outputs for these resources | no `Export:`; nothing in the repo reads them |
| Prod data-bucket notifications | none target orchestrator/downloader/fargate-trigger |

## Removal list

**Template resources** (`src/template.yaml`): `OrchestratorFunction` (and its
Nightly/Sweep schedule events), `DownloaderFunction`, `DownloaderCluster`,
`FargateExecutionRole`, `FargateTaskRole`, `FargateSecurityGroup`,
`FargateDownloaderTask`, `FargateTriggerFunction` (and its schedule),
`OrchestratorLogGroup`, `DownloaderLogGroup`, `FargateLogGroup`,
`FargateTriggerLogGroup`, `OrchestratorErrorAlarm`, `DownloaderErrorAlarm`.

**Template parameters** (used only by the above): `RealPTTAccount`, `RealPTTPassword`,
`StartDaysAgo`, `DownloadAudio`, `DownloadVideo`, `DownloadFiles`, `TimeDifferenceMs`,
`ScheduleExpression`, `FargateSubnetIds`, `FargateVpcId`.

**Template outputs**: `OrchestratorFunctionArn`, `DownloaderFunctionArn`,
`FargateClusterArn`, `FargateTaskDefArn`, `FargateLogsUrl`, `LogsUrl` (pointed at the
orchestrator log group), and the ECS line of `MonitoringDashboard`.

**Workflows** (`deploy.yml`, `deploy-prod.yml`): the `REALPTT_*` and `FARGATE_*` env
entries, the VPC/SUBNETS normalisation, and the four `--parameter-overrides` lines. These
must leave in the same commit as the parameters: SAM fails a deploy that overrides a
parameter the template no longer declares.

**Source**: `src/lambda_orchestrator.py`, `src/lambda_downloader.py`,
`src/fargate_downloader.py`.

**Tests**: `test_lambda_orchestrator.py`, `test_lambda_downloader.py` and
`test_download_claims.py` are deleted. The orchestrator section of
`test_the_directory_replaces_the_frozen_mapping.py` and the downloader cases of
`test_lake_put_adopted_shared.py` are dropped. Lists that named the removed resources are
trimmed: the deleter/caller sets in `test_lake_ownership_template.py`, the
required-secret set in `test_deploy_workflow_params.py`, the unwired-parameter exceptions
in `test_template_workflow_parameter_wiring.py`, and the handler-count floor in
`test_the_prod_zip_carries_what_the_handler_imports.py`.

**Legacy script** `scripts/deploy-lambda-code.sh` (used only by the manual
`deploy-prod-code.yml` for the hand-made March lambdas): the `orchestrator` and
`downloader` map entries are dropped because their source files are gone.

## What stays

- `lake_keys` still classifies `download_claims/`, `pending_downloads/` and
  `scripts/fargate_downloader.py` as global. Old objects may exist under those keys, and
  the classifier must keep recognising them.
- `lambda_transcribe_callback` still accepts `realptt_` job names (historical jobs).
- `safe_name` in org-api is unchanged. Existing folders were named by the orchestrator's
  rule, so the comments now describe it as the old rule.
- `EnableSchedules` / `ShouldEnableSchedules` stay; many other schedules use them.
- The hand-made unmanaged lambdas (`fieldsight-orchestrator`, `fieldsight-downloader`,
  `fieldsight-fargate-trigger`, and others without a stage prefix) are not in this stack.
  They are retired separately by the owner, together with the out-of-stack log groups
  `/aws/lambda/fieldsight-{orchestrator,downloader,fargate-trigger}`, the `realptt-*` log
  groups and the disabled EventBridge Scheduler entries `daily-download` and
  `fargate-trigger`. This deploy does not touch any of them.

## Deploy and rollback

The next TEST deploy (from develop), and later the prod deploy (from main), makes
CloudFormation delete the listed resources. Log groups are deleted with them: 30-day
retention for the orchestrator, 14 days for the rest. No deletion is expected to block:
there are no ENIs on the security groups, no running tasks, and no retained resources.
The roles have fixed names but no external attachments.

Gate before releasing to main (Fable review). A DELETE_FAILED in the cleanup phase of an
update does not fail the update: the stack still reaches UPDATE_COMPLETE and the resource
is orphaned, and a later revert would then collide on its fixed name. So after the TEST
deploy, and again after prod, check `aws cloudformation describe-stack-events` for
DELETE_FAILED on the removed logical IDs (or `aws iam get-role` on the four
`fieldsight-<stage>-fargate-*` roles and `describe-log-groups` on the fixed names). A green
deploy alone is not the gate.

Avoid deploying prod around 07:30Z: the prod Fargate trigger schedule fires then, and an
invocation landing after its log group is deleted but before the function is would
recreate `/aws/lambda/fieldsight-prod-fargate-trigger` without retention (delete it if so).

The prod orchestrator log group (about 1.9 MB, the last RealPTT sweeps up to ~10-12) goes
with the stack resource; export it first if that history is wanted.

Rollback means reverting the PR. CloudFormation recreates the resources, but the
deploy-workflow secrets and `s3://.../scripts/fargate_downloader.py` must still exist
then, so the owner deletes both only after prod has deployed cleanly.

## Owner follow-ups (after prod is clean)

1. Delete `s3://fieldsight-data-509194952652/config/user_mapping.json`. After this change
   no function reads it; a versioned bucket keeps the old version recoverable.
2. Optionally delete `s3://fieldsight-data-509194952652/scripts/fargate_downloader.py`.
3. Delete the repo secrets `REALPTT_ACCOUNT`, `REALPTT_PASSWORD`, `FARGATE_SUBNET_IDS`
   and `FARGATE_VPC_ID`.
