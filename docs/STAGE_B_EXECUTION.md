# Stage B Execution Plan — Dev Environment Bring-Up

> **Companion to:** [`INTEGRATION_PLAN.md`](../INTEGRATION_PLAN.md) §6 Stage B, [`ROADMAP.md`](../ROADMAP.md) P3 Stage B
> **Status:** Plan committed 2026-05-09. B.0 diagnostics complete; B.1–B.5 pending user approval per step.
> **Branch:** `claude/review-develop-branch-5gYGo` (this doc); template/config patches will be a follow-up PR off the same branch.

---

## 0. Why this doc exists

The original Stage B in ROADMAP.md jumped straight from "run B.0 diagnostics" to "deploy UI to dev S3 + CloudFront." The diagnostics revealed that **the dev backend stack does not exist** — only 3 DynamoDB tables and 1 Cognito pool with mismatched naming. There are no dev Lambda functions, no dev API Gateway, no SAM stack.

Stage B therefore needs an explicit infrastructure phase **before** the UI deployment phase. This doc breaks Stage B into 5 sub-steps and spells out every change.

---

## 1. B.0 Diagnostic findings (2026-05-09)

| Finding | Detail | Implication |
|---|---|---|
| **F1** | 10 prod Lambdas exist; `fieldsight-api` tags = `{}` | Prod was deployed manually, not via SAM |
| **F2** | `aws cloudformation list-stacks` returns empty for fieldsight/sitesync | No stack ever existed (not even ROLLBACK_COMPLETE) — SAM has never been run end-to-end |
| **F3** | DynamoDB has `fieldsight-test-{items,reports,audit}` ✅ but missing `fieldsight-test-{users,corrections,transcripts}` | samconfig.toml `[test]` section relies on defaults that point at **prod** tables for the missing 3 |
| **F4** | Cognito `fieldsight-users-test` exists (suffix style); SAM template would create `test-fieldsight-users` (prefix style) | Naming convention conflict |
| **F5** | DynamoDB tables use **infix** style (`fieldsight-test-items`); Cognito pool uses **suffix**; SAM template uses **prefix** | Three conventions in play; must unify |
| **F6** | 4 `sitesync-*` DynamoDB tables + 5 `sitesync-*` IAM roles still present | Phase4 cleanup never finished |
| **F7** | `github-actions-fieldsight-deploy` IAM role exists | CI deploy path partially set up; can be reused |
| **F8** | No `test-fieldsight-*` Lambda or IAM role exists | SAM deploy of test stack will not collide on those names |

---

## 2. Decisions

| # | Decision | Choice | Why |
|---|---|---|---|
| B-1 | Naming convention | **Infix** (`fieldsight-test-*`) for all dev resources | 3 of the existing test resources already use infix; Cognito test pool has zero users so renaming is free; Lambda/IAM roles do not exist yet so naming is free |
| B-2 | Prod deployment path (short term) | Stays manual for now; document import pathway in [`STAGE_C_PROD_IMPORT.md`](STAGE_C_PROD_IMPORT.md) (to be written) | Resource Import is a 1–2 day exercise with 20+ resources; do it after test stack proves the template |
| B-3 | Sitesync legacy cleanup | Defer to a separate one-shot cleanup script; not part of Stage B critical path | Removing 4 tables + 5 IAM roles is independent of UI integration |
| B-4 | Where to land template/config patches | **Separate PR off `claude/review-develop-branch-5gYGo`**, not on `claude/review-feature-content-hsaO3` | hsaO3 is a feature PR; deploy infrastructure changes need their own review trail |
| B-5 | DynamoDB table creation | Manual `aws dynamodb create-table` via a script committed to `scripts/aws/` | SAM template references tables as parameters (does not manage them); explicit script keeps schema versioned in git |
| B-6 | Test Cognito pool migration | **Delete + let SAM recreate** | User confirms zero users in `fieldsight-users-test`; deletion is non-destructive |

---

## 3. B.1 — Code changes (template + samconfig)

### 3.1 `src/template.yaml` — rename `Environment` → `EnvSuffix`, switch to infix

Currently (hsaO3 branch):
```yaml
Parameters:
  Environment:
    Type: String
    Default: ""
    AllowedValues: ["", "test-"]
...
  OrchestratorFunction:
    Properties:
      FunctionName: !Sub "${Environment}fieldsight-orchestrator"
```

Change to:
```yaml
Parameters:
  EnvSuffix:
    Type: String
    Description: "Environment suffix for resource names (empty for prod, '-test' for test)"
    Default: ""
    AllowedValues: ["", "-test"]
...
  OrchestratorFunction:
    Properties:
      FunctionName: !Sub "fieldsight${EnvSuffix}-orchestrator"
```

**Resources affected** (10 Lambda + 3 Cognito + IAM roles):
- 10 `FunctionName` lines: orchestrator, downloader, transcribe, vad, report-generator, transcribe-callback, meeting-minutes, ask-agent, fargate-trigger, api
- `UserPool.UserPoolName`: `!Sub "fieldsight${EnvSuffix}-users"`
- `UserPoolClient.ClientName`: `!Sub "fieldsight${EnvSuffix}-web-client"`
- `UserPoolDomain.Domain`: `!Sub "fieldsight${EnvSuffix}-${AWS::AccountId}"`
- IAM roles (if `RoleName` is set): `!Sub "fieldsight${EnvSuffix}-lambda-role"`, etc.

**Sed-style verification before commit:**
```bash
# After edit, no occurrence of ${Environment} should remain:
grep -n 'Environment}' src/template.yaml || echo "✅ clean"
# All Sub patterns should follow the new shape:
grep -nE 'fieldsight\$\{EnvSuffix\}-' src/template.yaml | wc -l   # expect ~14
```

### 3.2 `samconfig.toml` — fix test parameter overrides

Current `[test.deploy.parameters]`:
```toml
parameter_overrides = [
    "Stage=test",
    "DataBucketName=fieldsight-data-test-509194952652",
    "EnableSchedules=false",
    "ItemsTableName=fieldsight-test-items",
    "ReportsTableName=fieldsight-test-reports",
    "AuditTableName=fieldsight-test-audit",
]
```

After change:
```toml
parameter_overrides = [
    "EnvSuffix=-test",
    "DataBucketName=fieldsight-data-test-509194952652",
    "EnableSchedules=false",
    "ItemsTableName=fieldsight-test-items",
    "ReportsTableName=fieldsight-test-reports",
    "AuditTableName=fieldsight-test-audit",
    "UsersTableName=fieldsight-test-users",
    "CorrectionsTableName=fieldsight-test-corrections",
    "TranscriptTableName=fieldsight-test-transcripts",
]
```

`[default]` and `[prod]` sections: drop the old `Stage=prod`, leave everything else; `EnvSuffix` defaults to `""` so prod gets unprefixed names. (Note: `Stage` parameter is not in the template, was a leftover.)

### 3.3 New file: `scripts/aws/create_test_dynamodb_tables.sh`

```bash
#!/usr/bin/env bash
# Creates the 3 missing test DynamoDB tables with PAY_PER_REQUEST billing.
# Schema mirrors the corresponding prod tables (verified via describe-table on prod).
# Idempotent: skips tables that already exist.
set -euo pipefail
REGION=ap-southeast-2

create_table_if_missing() {
  local name=$1 ; local pk=$2 ; local sk=${3:-}
  if aws dynamodb describe-table --table-name "$name" --region "$REGION" >/dev/null 2>&1; then
    echo "✓ $name exists"
    return
  fi
  if [[ -n "$sk" ]]; then
    aws dynamodb create-table --table-name "$name" --region "$REGION" \
      --billing-mode PAY_PER_REQUEST \
      --attribute-definitions AttributeName="$pk",AttributeType=S AttributeName="$sk",AttributeType=S \
      --key-schema AttributeName="$pk",KeyType=HASH AttributeName="$sk",KeyType=RANGE
  else
    aws dynamodb create-table --table-name "$name" --region "$REGION" \
      --billing-mode PAY_PER_REQUEST \
      --attribute-definitions AttributeName="$pk",AttributeType=S \
      --key-schema AttributeName="$pk",KeyType=HASH
  fi
  echo "✓ created $name"
}

# Match prod schema — TODO: confirm SK names by running:
#   aws dynamodb describe-table --table-name fieldsight-users   --region ap-southeast-2
#   aws dynamodb describe-table --table-name fieldsight-corrections --region ap-southeast-2
#   aws dynamodb describe-table --table-name fieldsight-transcripts --region ap-southeast-2
# Then update the args below.
create_table_if_missing fieldsight-test-users         email
create_table_if_missing fieldsight-test-corrections   pk          sk
create_table_if_missing fieldsight-test-transcripts   transcript_key
```

> ⚠️ Schema is a placeholder. **Before running, confirm prod schema** for the 3 tables and adjust the script. Stage B.2 step 1 below makes this the first action.

---

## 4. B.2 — AWS state preparation

Run in this order. Each step has a verification command.

### 4.1 Confirm prod table schema (so create script matches)
```bash
for t in fieldsight-users fieldsight-corrections fieldsight-transcripts; do
  echo "=== $t ==="
  aws dynamodb describe-table --table-name "$t" --region ap-southeast-2 \
    --query 'Table.{Keys:KeySchema,Attrs:AttributeDefinitions,GSI:GlobalSecondaryIndexes[].IndexName}' \
    --output json
done
```
**Expected output:** key schema (PK/SK names), attribute types, any GSIs. Patch `create_test_dynamodb_tables.sh` to match.

### 4.2 Create the 3 missing test tables
```bash
bash scripts/aws/create_test_dynamodb_tables.sh
# Verify:
aws dynamodb list-tables --region ap-southeast-2 \
  --query "TableNames[?starts_with(@,'fieldsight-test-')]" --output table
# Expect: 6 tables (items, reports, audit, users, corrections, transcripts)
```

### 4.3 Delete legacy Cognito test pool (zero users — confirmed safe)
```bash
# 1. Find the pool id
POOL_ID=$(aws cognito-idp list-user-pools --max-results 60 --region ap-southeast-2 \
  --query "UserPools[?Name=='fieldsight-users-test'].Id | [0]" --output text)
echo "Pool to delete: $POOL_ID"

# 2. Confirm zero users (sanity check)
aws cognito-idp list-users --user-pool-id "$POOL_ID" --region ap-southeast-2 \
  --query 'Users[].Username' --output table
# Expect: empty / no users

# 3. Find any domain attached
aws cognito-idp describe-user-pool --user-pool-id "$POOL_ID" --region ap-southeast-2 \
  --query 'UserPool.Domain' --output text
# If a domain string is returned (e.g. fieldsight-XXXXX-test), delete it first:
#   aws cognito-idp delete-user-pool-domain --domain <name> --user-pool-id "$POOL_ID" --region ap-southeast-2

# 4. Delete the pool
aws cognito-idp delete-user-pool --user-pool-id "$POOL_ID" --region ap-southeast-2

# 5. Verify gone
aws cognito-idp list-user-pools --max-results 60 --region ap-southeast-2 \
  --query "UserPools[?contains(Name,'fieldsight') && contains(Name,'test')]"
# Expect: []
```

### 4.4 (Optional, deferred) Sitesync legacy cleanup
**Not blocking Stage B.** Captured here as a follow-up:
- 4 DynamoDB tables: `sitesync-{audit,items,reports,transcripts,users}` (5 actually — diagnostic showed 5 incl. transcripts)
- 5 IAM roles: `sitesync-{fargate-execution,fargate-task,lambda-role,scheduler-role,transcribe-callback-role}`

Before deleting any: `aws cloudtrail lookup-events --lookup-attributes AttributeKey=ResourceName,AttributeValue=sitesync-items --start-time $(date -d '30 days ago' --iso-8601) --region ap-southeast-2` — confirm no recent reads/writes.

---

## 5. B.3 — SAM deploy test stack

### 5.1 Pre-flight
```bash
# 1. Validate template
sam validate --region ap-southeast-2

# 2. Check it parses with EnvSuffix=-test
sam validate --region ap-southeast-2 \
  --parameter-overrides 'EnvSuffix=-test DataBucketName=fieldsight-data-test-509194952652'
```

### 5.2 Build
```bash
sam build --use-container=false
# Expect: .aws-sam/build/ populated, no errors
```

### 5.3 Deploy (first time = guided, to confirm everything before commit)
```bash
sam deploy --config-env test --guided
# Walk through prompts:
#   Stack Name: fieldsight-test
#   Region: ap-southeast-2
#   Confirm changes: y
#   Allow SAM CLI IAM role creation: y
#   Save arguments to samconfig.toml: n  (samconfig.toml is already curated)
```

### 5.4 Post-deploy verification
```bash
# 1. Stack status
aws cloudformation describe-stacks --stack-name fieldsight-test --region ap-southeast-2 \
  --query 'Stacks[0].StackStatus'
# Expect: CREATE_COMPLETE

# 2. All Lambdas tagged with stack
aws lambda list-functions --region ap-southeast-2 \
  --query "Functions[?starts_with(FunctionName,'fieldsight-test-')].FunctionName" --output table
# Expect: 10 functions

# 3. Cognito pool created
aws cognito-idp list-user-pools --max-results 60 --region ap-southeast-2 \
  --query "UserPools[?Name=='fieldsight-test-users']"
# Expect: 1 pool

# 4. Drift detection (sanity)
aws cloudformation detect-stack-drift --stack-name fieldsight-test --region ap-southeast-2
# Wait ~30s, then:
aws cloudformation describe-stack-drift-detection-status --stack-drift-detection-id <id-from-above>
# Expect: StackDriftStatus: IN_SYNC
```

### 5.5 Smoke test the dev pipeline
```bash
# Get test API Gateway URL
TEST_API=$(aws cloudformation describe-stacks --stack-name fieldsight-test --region ap-southeast-2 \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue | [0]" --output text)
echo "Test API: $TEST_API"

# Health check (should 401 without auth — that's correct)
curl -s -o /dev/null -w "%{http_code}\n" "$TEST_API/api/timeline?date=2026-05-09"
# Expect: 401 or 403 (auth gate working)
```

---

## 6. B.4 — UI dev deployment

(This was the original Stage B before B.0 ate the prerequisite work.)

1. Create test S3 bucket for UI: `fieldsight-ui-test-509194952652`
2. Create dev CloudFront distribution pointing at it
3. Create test Cognito user via `aws cognito-idp admin-create-user` for first dev login
4. Run `bash scripts/deploy_ui_from_external_repo.sh` (already committed in this branch) with test bucket override
5. Verify UI shell loads with `?baseUrl=$TEST_API&mocks=0&cognitoPool=<test-pool-id>`
6. Login round-trip + `/today` page populates from real API
7. 12-page tour against test backend
8. Sign-off checklist (legacy frontend on prod still works, dev UI degrades gracefully on missing endpoints — both already in ROADMAP §Stage B)

---

## 7. B.5 — Acceptance gate before Stage C

All of the following must be ✅ before opening Stage C work:

- [ ] `aws cloudformation describe-stacks --stack-name fieldsight-test` returns `CREATE_COMPLETE`
- [ ] All 10 `fieldsight-test-*` Lambda functions invokable (one trigger from S3 test event each)
- [ ] Cognito test user created, can login through UI shell
- [ ] `/today` page in UI renders ≥1 real backend response
- [ ] No prod resource modified during the entire process (verify by `aws cloudtrail lookup-events --lookup-attributes AttributeKey=ResourceName,AttributeValue=fieldsight-api --start-time …`)
- [ ] Drift detection on `fieldsight-test` stack returns `IN_SYNC`

---

## 8. Risks & rollback

| Risk | Mitigation | Rollback |
|---|---|---|
| `sam deploy --guided` fails on resource name collision | F8 confirms no collisions; if false-positive, run `aws cloudformation describe-stack-events` for the failure reason | `aws cloudformation delete-stack --stack-name fieldsight-test` (no prod impact) |
| Test DynamoDB schema mismatch with prod (e.g. wrong PK name) | B.2 step 4.1 confirms schema before creating | Drop and recreate tables (no data loss — they were just created) |
| Cognito domain naming `fieldsight-test-509194952652` already used by another AWS account globally | Pick alternate domain in template if `sam deploy` errors with `Domain not available` | Change `UserPoolDomain.Domain` and redeploy |
| GitHub Actions deploy workflow on `main` triggers an unwanted prod deploy | `EnableSchedules=false` for test; verify `.github/workflows/deploy.yml` only runs on prod when explicitly invoked | Disable workflow before this work, re-enable after |
| `sam build` fails because `transcript_utils.py` not bundled | Per CLAUDE.md, every Lambda zip must include `transcript_utils.py` — verify SAM template lists it in each Function's `CodeUri` or via shared layer | Update template to add `Layers:` reference or copy file into each function dir |

---

## 9. What this plan does NOT cover (future stages)

- **Stage C — Prod resource import.** Detailed in `STAGE_C_PROD_IMPORT.md` (to be written after `fieldsight-test` stack is stable for ≥2 weeks).
- **Sitesync legacy cleanup.** A one-shot script to delete 4 tables + 5 IAM roles. Independent of UI integration.
- **`github-actions-fieldsight-deploy` workflow update.** Currently exists but its IAM policy may need broader scope to deploy the test stack — re-evaluate during B.3.
- **Custom Vocabulary for test environment.** Optional. Prod has `fieldsight-construction-nz`. Test can share or get its own copy.

---

## 10. Cross-references

- B.0 diagnostics raw output: see this conversation's tool results (2026-05-09 session)
- Naming convention rationale: this conversation's Q1 thread
- Why prod stays manual short-term: this conversation's Q2 thread + Decision B-2 above
- Original Stage B definition (now superseded by this doc): [`ROADMAP.md`](../ROADMAP.md) §P3 Stage B
- UI deploy script: [`scripts/deploy_ui_from_external_repo.sh`](../scripts/deploy_ui_from_external_repo.sh) (already committed)
