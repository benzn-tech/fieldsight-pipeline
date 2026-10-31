# FieldSight Deployment Runbook

How code in GitHub reaches AWS. Both environments are SAM stacks built from
`src/template.yaml`; the branch decides which one.

```
push develop ──► deploy.yml ──► sam build/deploy fieldsight-test ──► migrate ──► wire S3 events ──► smoke /api/health
merge to main ──► deploy-prod.yml ──► `production` environment gate (15-minute wait)
              ──► sam build/deploy fieldsight-prod ──► migrate ──► wire S3 events ──► smoke /api/health
```

| | TEST | PROD |
|---|---|---|
| Branch | `develop` | `main` |
| Workflow | `.github/workflows/deploy.yml` ("Deploy FieldSight TEST (SAM)") | `.github/workflows/deploy-prod.yml` ("Deploy FieldSight PROD (SAM)") |
| Stack | `fieldsight-test` | `fieldsight-prod` |
| Database (shared Aurora cluster) | `fieldsight_test` | `fieldsight` |
| Data bucket | `fieldsight-data-test-509194952652` | `fieldsight-data-509194952652` |
| Migrations | `fieldsight-test-migrate` | `fieldsight-prod-migrate` |

- Both workflows skip pushes that only touch markdown, `docs/`, `ui/`, `frontend/` or
  `scripts/` (prod also skips `tests/`); see each file's `paths-ignore`.
  `workflow_dispatch` runs one by hand.
- Each workflow is serialised per stack (`concurrency`, no cancel), so merging several
  PRs queues deploys instead of failing on "UPDATE_IN_PROGRESS".
- Prod waits on the `production` GitHub environment, then deploys. Every migration
  merged to `main` runs on prod automatically, so experimental migrations never go to
  `main`.
- S3 ObjectCreated triggers are not in the template (SAM cannot attach events to an
  external bucket). `scripts/wire-s3-events.sh` merges them in after each deploy; on
  prod it applies only while the repo variable `PROD_WIRE_LAKE` is `true` (it is).
- Stack parameters and secrets come from repo variables (`TEST_*`, `PROD_*`) and GitHub
  secrets passed as `--parameter-overrides`. A parameter override for a parameter the
  template no longer has fails the deploy, so remove both in the same commit.
- Secrets in use include `AWS_ROLE_ARN` and the vendor API keys; the RealPTT/Fargate
  secrets are no longer used since 2026-10-29. Verify with `gh secret list`.

## Shipping

- **To TEST:** merge a PR into `develop`. Watch Actions → "Deploy FieldSight TEST (SAM)".
- **To prod:** open a release PR into `main` (a branch off `main` with cherry-picks, or
  `develop` → `main`), merge, then approve the waiting "Deploy FieldSight PROD (SAM)"
  run once the gate allows it.
- **Frontend:** the deployed web app is the separate `fieldsight-ui` repo on Amplify
  (app `d2fssznicvuckr`: branch `main` = prod, `dev` = test). The `ui/` and `frontend/`
  folders in this repo are not deployed.

## After a deploy

```bash
export AWS_PROFILE=fieldsight-deployer AWS_DEFAULT_REGION=ap-southeast-2
aws cloudformation describe-stacks --stack-name fieldsight-prod --query 'Stacks[0].StackStatus'
# A DELETE_FAILED during cleanup does not fail the update; check it when resources were removed
aws cloudformation describe-stack-events --stack-name fieldsight-prod   --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`]'
```

## Rollback

- **Backend:** revert the commit on the branch and let the workflow redeploy. A revert
  that re-creates removed resources needs their parameters and secrets to still exist.
- **Migrations** do not roll back by themselves; write a forward migration.
- **Frontend:** redeploy the previous build in Amplify.

## Guardrails

- Never `update-function-code` a hand-made zip. Every function is built from
  `CodeUri: src/`; a hand-picked bundle deploys and then fails at import.
- TEST sends real SES email. Scripts that create users or sessions on TEST use only the
  owner's alias accounts.
- Prod data writes (migrations aside) are run by the owner, not by CI.
- Local SAM: `sam build && sam deploy --config-env test` (samconfig.toml handles the
  template path and `base_dir`).
