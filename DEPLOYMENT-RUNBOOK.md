# FieldSight Deployment Runbook

How code in GitHub reaches AWS — automatically, per branch, with zero manual copy-paste.

```
                      ┌────────────── push develop ──────────────┐        ┌──────── merge → main ────────┐
   you commit ───────►│  GitHub                                  │        │  (PR approved & merged)       │
                      └──────────┬────────────────────┬──────────┘        └────────┬──────────────┬───────┘
                                 │ backend (SAM)       │ ui/ (S3+CloudFront)        │ backend        │ ui/
                                 ▼                     ▼                            ▼ (approval gate) ▼
                    Actions: deploy.yml         deploy-ui.sh test          Actions: deploy-prod-code  deploy-ui.sh prod
                    sam build → deploy           build ui/ + env-config     (waits for reviewer)   build ui/ + env-config
                    → fieldsight-test stack       → test URL                → update-function-code  → prod URL
                    → wire-s3-events --apply       (existing prod lambdas)                                  → wire-s3-events (dry-run)
                    → smoke /api/health                                       → smoke /api/health
```

Two independent tracks, both branch-driven:
| Tier | Hosted by | Trigger | TEST (`develop`) | PROD (`main`) |
|---|---|---|---|---|
| Backend Lambda code | GitHub Actions | push (non-`ui/`, non-docs) | **`develop`**: full SAM → `fieldsight-test` stack (`deploy.yml`) | **`main`**: code-only `update-function-code` to the existing prod lambdas (`deploy-prod-code.yml`, approval gate) |
| Frontend (`ui/`, static) | S3 + CloudFront (this account; `scripts/deploy-ui.sh`, run manually) | — | `fieldsight-web-test-*` + CF `E34AAK2PCGPWVZ` | `fieldsight-web-*` + CF `E12IVML224YUEE` |

- **Canonical SAM template** = `src/template.yaml` (the complete 9-Lambda stack; `CodeUri: src/` resolves via `base_dir = "."`). The old root `template.yaml` is legacy.
- **`samconfig.toml` is committed** (no secrets; secrets injected from GitHub secrets via `--parameter-overrides`).
- **PROD is NOT in CloudFormation.** The prod backend was hand-assembled (10 lambdas sharing `fieldsight-lambda-role`, crons in EventBridge Scheduler group `sitesync`, a manual `fieldsight-api` Gateway). A `sam deploy` to prod would collide with all of it, so **prod ships CODE-ONLY** via `deploy-prod-code.yml` (`update-function-code`, the documented manual process, automated). Full SAM adoption of prod is a separate migration (not done here). TEST is full SAM (fresh, isolated).

---

## One-time setup (AWS console / IAM — do once)

> GitHub repo secrets are already set (`AWS_ROLE_ARN`, `REALPTT_*`, `CLAUDE_API_KEY`, `FARGATE_*`). Verify with `gh secret list`.

1. **OIDC role** — confirm an IAM role trusts `token.actions.githubusercontent.com` for this repo and is the `AWS_ROLE_ARN` secret. Audit:
   ```bash
   aws iam list-open-id-connect-providers
   aws iam get-role --role-name <role-from-AWS_ROLE_ARN>   # check trust policy repo sub
   ```
   Permissions the role needs: CloudFormation, Lambda, S3, DynamoDB, Cognito, ECS, EventBridge, IAM (for SAM), `s3:PutBucketNotification`, `lambda:AddPermission`.

2. **GitHub `production` environment** (the prod approval gate): repo → Settings → Environments → New environment `production` → add **Required reviewers**. `deploy-prod-code` will pause until approved.

3. **TEST data plane** — `aws login`, then:
   ```bash
   bash scripts/bootstrap-env.sh test            # read-only audit: what exists / what's missing
   bash scripts/bootstrap-env.sh test --create   # create test bucket + 3 tables (mirrors prod schema)
   ```

4. **UI (S3 + CloudFront — this account has NO Amplify)** — the frontend infra already exists on account 509194952652:

   | env | bucket | CloudFront | URL |
   |---|---|---|---|
   | prod | `fieldsight-web-509194952652` | `E12IVML224YUEE` | d20o8d5t6bd7gm.cloudfront.net |
   | test | `fieldsight-web-test-509194952652` | `E34AAK2PCGPWVZ` | d3qwnuldpg1tmp.cloudfront.net |

   Deploy manually — `deploy-ui.sh` generates `ui/scripts/env-config.js`, syncs to the bucket, invalidates CloudFront (needs only the aws CLI; runs in Git Bash / WSL / CloudShell):
   ```bash
   bash scripts/deploy-ui.sh test    # test API URL auto-read from the fieldsight-test stack output
   COGNITO_CLIENT_ID=<test-pool-client> bash scripts/deploy-ui.sh test   # add the test Cognito client for login
   bash scripts/deploy-ui.sh prod    # prod API + fieldsight-web-client (4ratjdjonqm17tln6bs2761ci3) are built in
   ```
   (`amplify.yml` is the alternative path for an account where an Amplify app is connected — e.g. the company account / FS — **not** this account.)

5. **PROD S3 events** — nothing to do. Prod's S3 notifications are already wired and working (`vad-on-users` on `users/`, `transcribe-on-segments` on `audio_segments/*.wav`). Since prod is not under SAM, leave them as-is. (`wire-s3-events.sh` is used only for the fresh TEST bucket, applied automatically by `deploy.yml`.)

---

## Day-to-day (the whole point)

- **Ship to test**: commit → push `develop`. Actions deploys the backend to `fieldsight-test`. UI: run `bash scripts/deploy-ui.sh test` (manual).
- **Ship to prod**: open PR `develop → main`, merge. `deploy-prod-code.yml` **pauses for a reviewer** → approve → updates the existing prod lambdas' CODE (`update-function-code`, each published as a new version; infra/layers/env untouched). UI: run `bash scripts/deploy-ui.sh prod` (manual).
- **Roll back prod code**: each update publishes a Lambda version — in the console (or CLI) point the function back to the previous version, or revert the commit on `main` and re-merge.
- **UI-only change** (`ui/**`): backend workflow is path-ignored, so just run `deploy-ui.sh`. **Backend-only change**: push triggers the SAM (test) / code (prod) workflow; UI untouched.

## Where to look
| What | Where |
|---|---|
| Backend deploy logs | GitHub → Actions → "Deploy FieldSight (SAM backend)" |
| Stack resources / drift | CloudFormation console → `fieldsight-test` / `fieldsight-pipeline` |
| Lambda runtime logs | CloudWatch → `/aws/lambda/fieldsight[-test]-<fn>` |
| UI deploy | local terminal output of `deploy-ui.sh` + CloudFront invalidation status |
| PR validation (template lint) | GitHub → Actions → "CI (PR validation)" |

## Rollback
- **Backend**: CloudFormation console → stack → **Stack actions → previous changeset**, or revert the commit and re-push (redeploys the prior template).
- **UI**: re-run `deploy-ui.sh <env>` from a previous commit (or enable S3 bucket versioning to restore prior objects), then invalidate CloudFront.
- **Schedules**: TEST has `EnableSchedules=false` (no RealPTT polling / cron). PROD has them ENABLED.

## Notes / guardrails
- TEST and PROD are fully isolated: separate stack, S3 bucket (`fieldsight-data-test-*`), DynamoDB tables (`fieldsight-test-*`), Cognito pool, function names (`fieldsight-test-*`). A test deploy can never touch prod data — `deploy.yml` passes every env param explicitly.
- Never commit secrets. They live only in GitHub secrets (backend CI). The UI's `env-config.js` holds only public values (API URL, Cognito client id) and is generated at deploy time, never committed.
- Local SAM use: `sam build && sam deploy --config-env test` (samconfig handles template + base_dir).
