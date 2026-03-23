# Changelog: Template Fixes + Lambda Completion

> Date: 2026-03-23
> Scope: 4 files modified

---

## 1. template.yaml (678 → 1010 lines, +332 lines)

### New Parameters (+6)

| Parameter | Default | Purpose |
|---|---|---|
| `SiteName` | `SB1108 Ellesmere College` | Replaces hardcoded SITE_NAME in report generator |
| `VadLayerArn` | `''` (empty) | ARN of VAD Lambda Layer. Empty = skip VAD deployment |
| `DocxLayerArn` | `''` (empty) | ARN of python-docx Layer. Empty = Lambdas deploy without Word generation |
| `UsersTableName` | `fieldsight-users` | DynamoDB table for user profiles (API auth) |
| `TranscriptTableName` | `fieldsight-transcripts` | DynamoDB table for transcript ledger (callback) |
| `SiteName` | `SB1108 Ellesmere College` | Dynamic site name (was hardcoded) |

### New Conditions (+2)

| Condition | Logic |
|---|---|
| `HasVadLayer` | Deploy VAD Lambda only when VadLayerArn is provided |
| `HasDocxLayer` | Attach python-docx Layer to report/meeting Lambdas when provided |

### New Lambda Functions (+5)

| # | Function | Handler | Condition | Key Config |
|---|---|---|---|---|
| 2.5 | `fieldsight-vad` | `lambda_vad.lambda_handler` | `HasVadLayer` | 1024MB RAM, 2GB ephemeral, 15min timeout |
| 3b | `fieldsight-transcribe-callback` | `lambda_transcribe_callback.lambda_handler` | `ShouldEnableTranscribe` | EventBridge rule auto-created for Transcribe state changes |
| 6 | `fieldsight-meeting-minutes` | `lambda_meeting_minutes.lambda_handler` | Always | Optional DocxLayer, Claude Sonnet 4.6 |
| 7 | `fieldsight-api` | `lambda_fieldsight_api.lambda_handler` | Always | API Gateway integration, Cognito auth |
| — | `FieldSightApi` (API Gateway) | — | Always | Cognito authorizer, CORS, `/api/health` unauthenticated |

### New Cognito Resources (+3)

| Resource | Purpose |
|---|---|
| `UserPool` | `fieldsight-users` — email login, password policy |
| `UserPoolClient` | `fieldsight-web-client` — SRP auth, implicit OAuth, no secret |
| `UserPoolDomain` | `fieldsight-{account-id}` — auto-generated hosted UI domain |

### New Log Groups (+4)

`VadLogGroup`, `TranscribeCallbackLogGroup`, `MeetingMinutesLogGroup`, `ApiLogGroup`

### New Outputs (+8)

`VadFunctionArn`, `TranscribeCallbackFunctionArn`, `MeetingMinutesFunctionArn`, `ApiFunctionArn`, `ApiEndpoint`, `CognitoUserPoolId`, `CognitoUserPoolClientId`, `CognitoDomain`

### Fixes to Existing Resources

| Resource | Change | Before | After |
|---|---|---|---|
| `ReportGeneratorFunction` | Model version | `claude-sonnet-4-5-20250929` | `claude-sonnet-4-6` |
| `ReportGeneratorFunction` | Site name | Hardcoded `SB1108 Ellesmere College` | `!Ref SiteName` (parameter) |
| `ReportGeneratorFunction` | DynamoDB | Missing | `ENABLE_DYNAMODB: 'true'` |
| `ReportGeneratorFunction` | Layers | None | Optional DocxLayer via `!If HasDocxLayer` |
| `TranscribeFunction` | Env vars | Missing | `TRANSCRIPT_TABLE` added |
| `TranscribeFunction` | Policies | Missing | `DynamoDBCrudPolicy` for transcript table |

---

## 2. lambda_fieldsight_api.py (+18 lines)

### Security Fix: Presigned URL Permission Check

**Before:** `get_presigned_url(params)` only checked S3 key prefix (users/, transcripts/, etc.) — any authenticated user could access any other user's media by constructing the key manually.

**After:** `get_presigned_url(params, caller)` now:
1. Extracts the target user folder from the S3 key path pattern
2. Calls `can_access_user_data(caller, target_user)` to verify RBAC permission
3. Admins/GMs bypass the check (as expected)
4. Workers, site_managers, PMs are restricted to their authorized scope
5. Also added `web_video/` to the allowed prefix list (was missing)

---

## 3. user_mapping.json (1 line)

S3 bucket reference in `_instructions`: `nottag-bs-manual-1` → `fieldsight-data-509194952652`

---

## 4. prompt_templates.json (1 line)

S3 bucket reference in `_instructions`: `realptt-downloads-fieldsight` → `fieldsight-data-509194952652`

---

## Deployment Notes

### First Deploy with New Resources

```bash
# Build
sam build

# Deploy (will prompt for new parameters)
sam deploy --guided
#   VadLayerArn:  paste your layer ARN, or leave empty to skip
#   DocxLayerArn: paste your layer ARN, or leave empty
#   Accept defaults for UsersTableName, TranscriptTableName, SiteName
```

### S3 Event Triggers (manual setup still required)

These S3 triggers cannot be managed by SAM because the bucket is external:

1. **VAD Lambda trigger** (if deployed):
   - Prefix: `users/` → Suffix: `.wav`, `.mp4`, `.m4a`
   - Target: `fieldsight-vad`

2. **Transcribe Lambda trigger** (existing):
   - Prefix: `audio_segments/` → Suffix: `.wav`
   - Target: `fieldsight-transcribe`

### Cognito Post-Deploy

After deploy, update `UserPoolClient` CallbackURLs with your actual CloudFront domain:
```bash
aws cognito-idp update-user-pool-client \
  --user-pool-id <from output CognitoUserPoolId> \
  --client-id <from output CognitoUserPoolClientId> \
  --callback-urls "https://your-cloudfront-domain.com/callback" \
  --logout-urls "https://your-cloudfront-domain.com/"
```

### Upload Updated Config Files

```bash
aws s3 cp user_mapping.json s3://fieldsight-data-509194952652/config/user_mapping.json
aws s3 cp prompt_templates.json s3://fieldsight-data-509194952652/config/prompt_templates.json
```
