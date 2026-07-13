# Prod Isolation / Customer-Facing Environment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up an independent `fieldsight-prod` SAM stack deployed from `main` so customers use a stable environment, while `develop` keeps iterating `fieldsight-test` — both stacks sharing ONE Aurora, ONE RAG store, and ONE S3 lake, with tenant isolation by `company_id`/site.

**Architecture:** The existing SAM template already parameterizes everything by `Stage` (prefix mapping) — we re-point the `prod` stage at a NEW prefix `fieldsight-prod` (the legacy hand-built `fieldsight-*` lambdas stay untouched and become a frozen dev-site serving shim). The shared-lake double-pipeline problem is resolved by **bucket-scoped chain ownership**: the lake's S3 event notifications are flipped (one atomic `PutBucketNotificationConfiguration`) from today's `fieldsight-test-*` targets to `fieldsight-prod-*`; the test stack's identical chain stays wired ONLY to the test bucket. Tenancy is row-level in Aurora: the prod chain resolves company per-user via the identity directory; the test chain stays pinned to the internal company.

**Tech Stack:** AWS SAM/CloudFormation (`src/template.yaml`), GitHub Actions OIDC deploys, S3 event notifications, Aurora PG (psycopg, in-VPC), Cognito (shared pool `ap-southeast-2_q88pd6XXr`), AWS Amplify (UI, app `d2fssznicvuckr`).

## Global Constraints

- Account `509194952652`, region `ap-southeast-2`. This is the user's own SAM pipeline — NOT the company CDK prod (164088480050); never conflate (memory: two accounts/two architectures).
- **Never touch the legacy hand-built lambdas' infra** (`fieldsight-api`, gateway `khfj3p1fkb`, EventBridge Scheduler crons) except the explicit cutover steps in Task 10.
- **PGPASSWORD-MATCH gate before every stack deploy** (both stacks): the deploy-time secret snapshot must equal a live in-VPC lambda's `PGPASSWORD` (memory: DB password rotation trap; rotation disabled 2026-07-10, still verify).
- **Shared-Aurora schema rule:** while two code versions (develop/main) share one DB, migrations must be **additive-only** (new tables/columns/indexes; no drops, renames, or type changes) until both stacks run code that knows the change.
- Migration numbering: `0011` is reserved by `docs/superpowers/plans/2026-07-14-authority-flip.md`; this plan uses `0012` (gaps are harmless — `db/migrate.py` applies in filename order).
- Windows/AWS quirks: `MSYS_NO_PATHCONV=1` for path-like args; `cygpath -w` for `fileb://`; `node` (not python3) for local shell JSON (BUG-29); `export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1` before any local `cloudformation deploy` (BUG-35).
- Git hygiene: single-line Edit anchors (CRLF repo), never `git add -A` on pipeline develop. User merges all PRs.
- BUG-33/34 stand: S3 events on external buckets are wired by script, never by SAM `Events`; external resources (buckets, DynamoDB tables) enter the template as Parameters only.
- UI repo: no build step, no npm; `node --check` every touched JS; branch off `dev`.

---

## §0 Investigation record (file:line evidence — verified 2026-07-14, repo-side; AWS-side items re-verified in Task 0)

### 1. The SAM template IS already parameterized for a second stack — with three collision traps

- `src/template.yaml:40-44` — `Stage` parameter, `AllowedValues: [test, prod]`; `src/template.yaml:256-261` — `Mappings.StageConfig` maps `prod → Prefix: fieldsight`, `test → Prefix: fieldsight-test`. Every function/cluster/role/alarm name derives from it, e.g. `src/template.yaml:319`: `FunctionName: !Sub ["${P}-orchestrator", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]` — 20+ resources follow this exact pattern (`:371,398,436,467,528,567,603,641,658,697,780,827,872,943,992,1054,1103,1188,1219,1242,1315,1373,1387,1404,1449,1484,1665,1677,1696,1715`).
- Params that make a parallel stack point at shared data: `DataBucketName` (`:60-63`), `IngestBucketName` (`:65-76`, default = the lake `fieldsight-data-509194952652`), `DbStackName`/`DbSecretArn`/`DbSubnetIds` (`:218-241`), `OrgUserPoolId` (`:243-251`), `ClaudeApiKey`/`DashScopeApiKey` (`:134-144`), `VadLayerArn`/`DocxLayerArn` (`:157-169`), DynamoDB table params (`:172-195`).
- **Trap A — `Stage=prod` maps to prefix `fieldsight`**, which collides with the ~10 legacy hand-built lambdas (BUG-34: CFN cannot create `fieldsight-api` etc. — they exist outside any stack). Fix: re-point the `prod` mapping to `fieldsight-prod` (Task 1). The samconfig `[prod]`/`[default]` deploy envs (`samconfig.toml:28-62`, stack `fieldsight-pipeline`, `Stage=prod`) are vestigial — the deploy.yml header (`:5-10`) states prod was never SAM-deployed.
- **Trap B — `UserPoolDomain` is NOT stage-prefixed**: `src/template.yaml:1264-1268` `Domain: !Sub fieldsight-${AWS::AccountId}`. The test stack already owns `fieldsight-509194952652`; a second stack would fail creating the same domain. Fix in Task 1 (hosted UI is unused — the UI does SRP/USER_PASSWORD_AUTH with hardcoded pool `fieldsight-ui/scripts/auth/cognito.js:42`, so replacing the test domain is safe).
- **Trap C — `StorageBucketPolicy`** (`src/template.yaml:292-311`) does `PutBucketPolicy` on `DataBucketName`. For the prod stack that's the LAKE, which has a hand-managed policy — CFN will silently replace it, and stack deletion would DELETE it (breaking Transcribe writes). Fix: reconcile statements into the template (Task 5) + `DeletionPolicy: Retain` (Task 1).
- Conclusion: **`sam deploy --stack-name fieldsight-prod` works after a small template parameterization pass (Task 1)** — no structural rework.

### 2. THE CRUX — current lake wiring and why the double-pipeline resolves to an ownership flip

- Today's lake (`fieldsight-data-509194952652`) notifications are hand-managed and route the modern chain to **TEST** lambdas: `transcripts/*.json → fieldsight-test-extract-session`, `extractions/*.json → fieldsight-test-item-writer`, `reports/*daily_report.json → fieldsight-test-embed-report`, `embeddings/*vectors.json → fieldsight-test-ingest`, `match_requests/*.json → fieldsight-test-programme-matcher`, plus legacy front-pipeline entries `vad-on-users → fieldsight-vad` and `transcribe-on-segments → fieldsight-transcribe` (evidence: `docs/superpowers/plans/2026-07-14-authority-flip.md:116-121` expected-state block; `DEPLOYMENT-RUNBOOK.md:64`; `scripts/wire-s3-events.sh:84-91` comment). Re-verify live in Task 0.
- `scripts/wire-s3-events.sh` is already merge-not-clobber and stage-aware (`:24-37` derives `PREFIX` from stage; `:143-150` preserves all non-`fs-` entries and replaces only `fs-*` ids) — it just doesn't know a `fieldsight-prod` prefix yet, and preserves (rather than retires) the legacy non-`fs-` ids.
- **S3 makes the naive double-pipeline impossible to configure**: S3 rejects `PutBucketNotificationConfiguration` when two configurations have overlapping prefix+suffix for the same event type. Two stacks both wired on `transcripts/` + `.json` can never coexist on one bucket — so "every upload processed twice" cannot happen by S3-event wiring; the only choice is WHICH single chain owns each bucket.
- `deploy.yml:105-106` wires the TEST bucket (`fieldsight-data-test-509194952652`) chain automatically on every develop deploy — the test stack already has a complete, self-triggering chain on its own bucket.

### 3. Multi-tenant readiness — org/RAG are scoped; the PIPELINE write path is single-company

- **org-api is company-scoped throughout**: `src/lambda_org_api.py:134-135` rejects callers without `company_id`; every read/write goes through `caller["company_id"]` (`:251,264,340,367,422,441,501-508,635,671,711,769,789` — sites, members, observations, live-items, programme). Caller resolution is `users.get_user_by_sub` (DB row), NOT a pool claim.
- **RAG is site→company scoped, not global**: `report_chunks.site_id` is `NOT NULL REFERENCES sites` (`src/migrations/0004_report_chunks.sql:3`); retrieval filters `WHERE c.site_id = ANY(%(site_ids)s)` (`src/repositories/search_sql.py:19`); `lambda_rag_search.py:67-71` computes site_ids as `list_company_sites(conn, caller["company_id"])` for admin/gm (company-scoped "ALL"), memberships otherwise, deny-by-default on empty (`:82-83`). **No cross-tenant RAG leak.**
- **The pipeline write path is pinned to ONE company**: `src/lambda_ingest.py:76` `COMPANY_NAME = os.environ.get("COMPANY_NAME", "FieldSight")`; `:272` `companies.get_company_by_name(conn, COMPANY_NAME)`; identity resolution is `users.get_by_folder_name(conn, company_id, folder)` — company-scoped (`src/repositories/users.py:38-41`, unique index `(company_id, folder_name)` `src/migrations/0007_identity_directory.sql:8`). `src/lambda_item_writer.py:64,121,132` mirrors it. **Consequence: a customer's lake uploads would resolve `site=None` and be SKIPPED with zero Aurora writes (`lambda_ingest.py:280-284`, `lambda_item_writer.py:133-137`) — customers would be invisible until Task 2 fixes this.**
- **The LEGACY serving surface (`lambda_fieldsight_api.py`) is single-company by design**: role model from DynamoDB `USERS_TABLE` + `config/user_mapping.json` (`:81-124`); unknown Cognito subs default to `role: 'viewer'` (`:86`) which resolves to zero accessible users/sites (fail-closed); but **admin/gm bypass everything** — `get_dates:317-318` lists ALL user folders, `get_presigned_url:386` skips the ownership check entirely. Leak vector = ever granting a customer admin/gm in DynamoDB `fieldsight-users` or `user_mapping.json`. Guardrail: customers get NO records in either store (their identity lives only in Aurora + Cognito).
- `lambda_extract_session.py` tolerates unmapped users (uses `user_folder` directly; `user_mapping.json` only feeds `declared_site` matching with a warning fallback `:74-87`) — customer extraction works without mapping entries.

### 4. Serving APIs / the dev-site hybrid

- Dev Amplify site (`d2fssznicvuckr`, branch `dev`): `FS_BASEURL=https://khfj3p1fkb…/prod/api` → legacy prod `fieldsight-api` (timeline/dates/actions/media, reads the lake, non-VPC), `FS_ORG_BASEURL=https://wdsgobb7b0…` → TEST stack gateway (`/api/org/*` → `fieldsight-test-org-api`; `/api/ask`, `/api/search` → `fieldsight-test-api` → ask-agent → rag-search). Mechanism: `fieldsight-ui/amplify.yml:10-13` writes `window.FS_ENV = { baseUrl, orgBaseUrl, … }` into `dist/env.js` at Amplify build time from **per-branch Amplify environment variables**; consumed at `fieldsight-ui/scripts/api/index.js:75-86`.
- The test stack's `ApiFunction` reads `S3_BUCKET=DataBucketName` = the (report-empty) test bucket (`src/template.yaml:1322`), which is WHY the dev site keeps `FS_BASEURL` on the legacy prod api — timeline needs the lake.
- Cognito is already effectively shared: template authorizer accepts both the stack's own pool AND `OrgUserPoolId` (`src/template.yaml:1296-1307`); the UI pool id is hardcoded (`cognito.js:42`) so a new UI branch needs zero auth changes.

### 5. Deploy pipelines today

- `develop` → `.github/workflows/deploy.yml` → full SAM deploy of `fieldsight-test` with explicit overrides (`:70-88`), migrate invoke (`:90-103`), test-bucket event wiring (`:105-106`), CORS/lifecycle, smoke test.
- `main` → `.github/workflows/deploy-prod-code.yml` → `scripts/deploy-lambda-code.sh fieldsight` — code-only zip update of the 9 legacy lambdas (`deploy-lambda-code.sh:27-37`), approval-gated by GitHub environment `production` (`deploy-prod-code.yml:33`). **`main` is ~50 PRs behind `develop`** (main HEAD `e7e0f37`, develop HEAD `09fea24` #51) — a naive first promotion would push 3 months of drifted code onto the hand-built lambdas whose env/layers/infra never moved with it. This workflow must be neutered before the first promotion (Task 4).
- UI repo: default branch `main` is an ancestor of `dev` (verified `git merge-base --is-ancestor`) — first UI promotion is a clean PR/fast-forward. Amplify has only the `dev` branch.

### 6. Data model / migration baseline

- All existing Aurora data already belongs to company `FieldSight` (`lambda_ingest.py:76` default; org-seed created it). **The internal/test company = the existing company. Zero data migration.** Customers = new `companies` rows starting empty (`repositories/companies.py:4-9` has `create_company`; org-api has no company-creation route — onboarding uses SQL via Aurora Data API / a seed invoke, Task 12).

---

## §1 The CRUX resolved — shared lake, two stacks, ONE chain per bucket

**Decision: bucket-scoped chain ownership (a hardened variant of option (b)), executed as an atomic flip of the lake's notification config from `fieldsight-test-*` to `fieldsight-prod-*` targets.**

- **The prod stack owns the lake.** After Task 8, every lake S3 event (users/ → vad, audio_segments/ → transcribe, transcripts/ → extract-session, reports/ → embed-report, embeddings/ → ingest, extractions/ → item-writer, match_requests/ → matcher) targets `fieldsight-prod-*` functions deployed from `main`. Customers and the internal company's real field recordings all flow through STABLE main code — internal usage becomes true dogfooding at customer parity.
- **The test stack owns the test bucket** (already true: `deploy.yml:105-106`). Developers iterate pipeline code by copying lake fixtures into `fieldsight-data-test-509194952652` (or uploading via the dev site / test org-api presign, which targets the test bucket) — the full develop-deployed chain runs there, writing to the SHARED Aurora but pinned to the internal company (`MULTI_TENANT_RESOLUTION` stays false on test, Task 2). A develop bug can therefore corrupt at most internal-company rows — never a customer's.
- **Why every upload is processed exactly once:** each bucket has exactly one owning chain, and S3 itself rejects overlapping prefix+suffix configurations for the same event type — the "both stacks wired on the lake" failure mode is unconfigurable, not merely discouraged. The flip is one `PutBucketNotificationConfiguration` call (atomic; rollback = re-PUT the backed-up JSON).
- **Why not option (a) per-tenant prefixes:** S3 filters are literal prefixes with no wildcards; the tenant would have to be the FIRST path segment of all seven stage prefixes, which means re-laying-out every S3 path and every parser (`lambda_ingest.py:79-80` REPORT_KEY_RE/EMBEDDINGS_KEY_RE, `lambda_extract_session.py:95-116`, `lambda_transcribe` output paths, `lambda_fieldsight_api` timeline/media paths, UI deep links), PLUS one notification config per tenant per stage (7×N against the 100-config bucket cap) with no catch-all possible (overlap rule). Maximum churn, no additional isolation over row-level tenancy. Rejected.
- **Why not option (b) as literally stated (test-owned shared chain — today's state):** iterating extract/ingest/matcher on develop would rewrite customer Aurora rows — exactly the isolation violation the user wants to end.
- **Why not (c) separate lakes:** unnecessary. The shared lake works because tenancy is enforced at the Aurora boundary (identity bridge → company/site rows) and at every read path (org ACL, RAG site filter). The one thing that does NOT shard cleanly is the *legacy* serving surface (`fieldsight-api` role model) — resolved by keeping customers off it entirely (fail-closed viewer + no DynamoDB/user_mapping records), not by splitting the lake.
- **Honest residual:** a fixture copied to the test bucket produces rows with the same `source_s3_key` string as its lake original, so the test chain's delete-then-insert (`lambda_ingest.py:288-297`) REPLACES the prod-written rows for that internal report. Blast radius: internal company only; behavior: last-writer-wins replace, not duplication. Documented in the fixture workflow (Task 9).

## §2 Design decisions

- **D1 — Stage mapping:** `StageConfig.prod.Prefix` becomes `fieldsight-prod`. The legacy `fieldsight-*` lambdas are never CFN-managed; they freeze (Task 4 neuters their deploy) and shrink to a dev-site serving shim (`fieldsight-api` + `fieldsight-ask-agent` + `fieldsight-meeting-minutes` stay in service for `khfj3p1fkb`; vad/transcribe/orchestrator/report-generator go idle after Task 10).
- **D2 — Exact parameter differences (test vs prod stack):**

| Parameter | `fieldsight-test` (deploy.yml) | `fieldsight-prod` (deploy-prod.yml, new) |
|---|---|---|
| `Stage` | `test` | `prod` |
| stack name | `fieldsight-test` | `fieldsight-prod` |
| `DataBucketName` | `fieldsight-data-test-509194952652` | `fieldsight-data-509194952652` (lake) |
| `IngestBucketName` | `fieldsight-data-test-509194952652` (**changed by Task 9**; today defaults to the lake) | `fieldsight-data-509194952652` (default) |
| `EnableSchedules` | `false` | `false` until Task 10 cutover, then `true` (repo variable `PROD_ENABLE_SCHEDULES`) |
| `ItemsTableName`/`ReportsTableName`/`AuditTableName` | `fieldsight-test-*` | `fieldsight-items` / `fieldsight-reports` / `fieldsight-audit` (legacy tables — check-off audit continuity) |
| `UsersTableName`/`TranscriptTableName` | defaults (`fieldsight-users`/`fieldsight-transcripts`) — pre-existing sharing quirk, unchanged | defaults (same tables) |
| `VadLayerArn`/`DocxLayerArn` | empty (test has NO vad / no Word gen) | legacy layer ARNs (captured in Task 0, repo variables) |
| `MultiTenantResolution` (new, Task 2) | `false` (pinned to internal company) | `true` |
| `DbStackName`/`DbSubnetIds`/`DbSecretArn`/`OrgUserPoolId` | shared values | identical (same Aurora, same pool `ap-southeast-2_q88pd6XXr`) |
| `RealPTTAccount/Password`, `ClaudeApiKey`, `DashScopeApiKey`, `FargateSubnetIds/VpcId` | GH secrets | same GH secrets |

- **D3 — Gateway mapping (resolves the dev-site tangle; dev changes NOTHING):**

| Amplify branch | `FS_BASEURL` (timeline/dates/actions/media/ask/search fallback) | `FS_ORG_BASEURL` (org/live-items/ask/search/programme) |
|---|---|---|
| `dev` (internal) | `https://khfj3p1fkb.execute-api.ap-southeast-2.amazonaws.com/prod/api` — **unchanged** (legacy prod api keeps serving lake timeline/media until authority-flip + org media presigns retire it) | `https://wdsgobb7b0.execute-api.ap-southeast-2.amazonaws.com/prod/api` — **unchanged** (test stack) |
| `main` (customers, new) | `https://<PROD_API_ID>.execute-api.ap-southeast-2.amazonaws.com/prod/api` (fieldsight-prod gateway) | same prod gateway (it serves `/api/{proxy+}` AND `/api/org/*`) |

  Customers never touch `khfj3p1fkb` or the test stack. Internal users never depend on the prod stack.
- **D4 — Cognito: shared pool.** One pool (`ap-southeast-2_q88pd6XXr`) for internal + customers; company is resolved from the `users` DB row by sub (`lambda_org_api.py`, `lambda_rag_search.py:61-68`), never from a token claim, so no pool/claim changes. The prod stack's template-created `fieldsight-prod-users` pool is an unused artifact (harmless; parameterizing pool creation is YAGNI). UI auth is unchanged (`cognito.js:42`).
- **D5 — Promotion flow:** backend `develop → main` PR (user merges) → `deploy-prod.yml` (SAM `fieldsight-prod`, gated by the existing `production` GitHub environment approval). UI `dev → main` PR → Amplify `main` branch auto-builds. Cadence: promote after a TEST soak; the PGPASSWORD-MATCH gate runs inside the prod workflow. `deploy-prod-code.yml` loses its push trigger BEFORE the first promotion (carried in the same merge — GitHub evaluates workflow files at the pushed commit) and is deleted in Task 14.
- **D6 — Migration (#8): none.** Existing Aurora/lake data = the internal company (`FieldSight`), as-is. Customers start empty via the onboarding runbook (Task 12). Optional cosmetic rename of the company is deferred (renaming breaks the test chain's `COMPANY_NAME` pin unless env is updated in lockstep — do not do it casually).
- **D7 — Customer v1 scope (stated plainly):** customers get the org-scoped surfaces — org pages, `/live-items` dashboard (extraction topics), `/ask` + `/api/search` (RAG, company-scoped), programme, recordings upload via org-api presign. They do NOT get in v1: `daily_report.json` documents (report-generator identity is `user_mapping.json`-driven — needs the recording↔site attribution design, memory item, before customer reports), media deep-links via `/api/media/presigned-url` (403 fail-closed for viewer-role unknowns; follow-up = org-api presigned GET with Aurora ACL), weekly/monthly/Word outputs. Their VAD→transcribe→extraction chain runs fully (extract-session tolerates unmapped folders), so the dashboard-first surface is live for them — aligned with the dashboard-first direction (memory).
- **D8 — Coordination with `2026-07-14-authority-flip.md`:** its Task 0 expects the lake chain on `fieldsight-test-*` — TRUE until this plan's Task 8, after which its target for the ingest-defer flag becomes the PROD stack (the flag is a template param carried by promotion, so no rework — but its live-verification expectations invert). Recommended order: land the authority-flip backend work on `develop` BEFORE this plan's Task 6 first promotion, so the prod stack is born with the `/api/org/timeline` shim + `AUTHORITY_FLIP` param; then the customer `main` branch can set `FS_TIMELINE_SOURCE=org` from day one. Not a hard dependency — this plan works without it (customer timeline simply stays empty-but-graceful until the shim ships).
- **D9 — Rollback posture (strong):** every phase is independently reversible; the prod stack is ADDITIVE (creating it touches nothing existing). Lake-flip rollback = re-PUT the backed-up notification JSON (or `wire-s3-events.sh <lake> test … --apply`). Prod-stack teardown ≠ casual `delete-stack`: `StorageBucketPolicy` has `DeletionPolicy: Retain` (Task 1) so deletion can't strip the lake policy. A bad prod code deploy = re-run `deploy-prod.yml` from the previous main SHA (`workflow_dispatch`). Nothing in this plan writes to test-stack resources or dev-site config.
- **D10 — Programme quirk (internal company):** internal programmes live in the TEST bucket (`programmes/{site_id}/programme.json`, written by test org-api); the prod matcher reads the LAKE (`PROGRAMME_BUCKET=DataBucketName`, `src/template.yaml:1119-1124`). Task 13 does a one-time copy so internal impact-matching works on the prod chain; dev-site programme edits keep landing in the test bucket (documented drift — re-sync when it matters; customers are unaffected, their programmes are written by prod org-api straight to the lake).

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `src/template.yaml` | Modify | `prod → fieldsight-prod` prefix; stage-prefixed UserPoolDomain; `DeletionPolicy: Retain` on StorageBucketPolicy; `MultiTenantResolution` param → env on Ingest/ItemWriter; stale "prod is not SAM" comments |
| `samconfig.toml` | Modify | `[prod]` env → stack `fieldsight-prod` with correct overrides; remove the foot-gun `[default.deploy.parameters]` |
| `src/migrations/0012_folder_name_global_unique.sql` | Create | global unique index on `users.folder_name` (loud failure instead of silent cross-tenant misattribution) |
| `src/repositories/users.py` | Modify | `get_by_folder_name_global(conn, folder_name)` |
| `src/repositories/companies.py` | Modify | `get_company_by_id(conn, company_id)` |
| `src/lambda_ingest.py` | Modify | `resolve_company(conn, user_folder)` — global folder lookup when `MULTI_TENANT_RESOLUTION=true`, else `COMPANY_NAME` pin; used by `ingest_report` |
| `src/lambda_item_writer.py` | Modify | use `lambda_ingest.resolve_company` |
| `tests/unit/test_lambda_ingest.py`, `tests/unit/test_lambda_item_writer.py` | Modify | TDD for resolve_company |
| `scripts/wire-s3-events.sh` | Modify | `prod → fieldsight-prod` prefix; `RETIRE_IDS` env to drop named legacy entries |
| `.github/workflows/deploy-prod.yml` | Create | main → SAM `fieldsight-prod` + PGPASSWORD gate + migrate + gated lake wiring + smoke test |
| `.github/workflows/deploy-prod-code.yml` | Modify (Task 4) then Delete (Task 14) | drop push trigger; later remove entirely |
| `.github/workflows/deploy.yml` | Modify (Task 9) | pass `IngestBucketName=fieldsight-data-test-509194952652` |
| `docs/CUSTOMER-ONBOARDING.md` | Create | company/site/admin/field-user provisioning runbook + ACL verification checklist |
| `DEPLOYMENT-RUNBOOK.md`, `CLAUDE.md` | Modify | two-stack reality, promotion flow, rollback drill |

---

### Task 0 — Live-infrastructure verification gate (read-only; STOP on any mismatch)

The planning session's AWS session was expired; these documented facts MUST be re-verified live before anything lands. Run `aws login` first. All read-only.

- [ ] **Step 1 — lake notifications match the documented state.**
```bash
export MSYS_NO_PATHCONV=1
aws s3api get-bucket-notification-configuration --bucket fieldsight-data-509194952652 --region ap-southeast-2 --output json > /tmp/lake-notif-baseline.json
cat /tmp/lake-notif-baseline.json
```
Expected: `fs-extract-transcripts/fs-write-extractions/fs-embed-report/fs-ingest-report/fs-programme-match` → `fieldsight-test-*` ARNs; `vad-on-users`/`transcribe-on-segments` (or similarly named non-`fs-` ids) → legacy `fieldsight-vad`/`fieldsight-transcribe`. **Record the EXACT ids of the two legacy entries** (Task 8's `RETIRE_IDS` needs them verbatim). If the modern chain routes anywhere else, STOP — re-derive §1.
- [ ] **Step 2 — legacy layer ARNs (prod stack needs them).**
```bash
aws lambda get-function-configuration --function-name fieldsight-vad --region ap-southeast-2 --query 'Layers[].Arn'
aws lambda get-function-configuration --function-name fieldsight-report-generator --region ap-southeast-2 --query 'Layers[].Arn'
```
Record as GitHub repo variables `PROD_VAD_LAYER_ARN` / `PROD_DOCX_LAYER_ARN` (set in Task 4 Step 4).
- [ ] **Step 3 — lake bucket policy + CORS (clobber-risk inventory).**
```bash
aws s3api get-bucket-policy --bucket fieldsight-data-509194952652 --region ap-southeast-2 --output text | node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(d),null,2)))"
aws s3api get-bucket-cors --bucket fieldsight-data-509194952652 --region ap-southeast-2 || echo "NO CORS configured"
```
Save both outputs into the Task 5 worksheet. If the policy contains statements beyond the template's two Transcribe statements (`src/template.yaml:299-311`), they must be merged into the template in Task 5.
- [ ] **Step 4 — legacy schedule inventory (Task 10's cutover list).**
```bash
aws scheduler list-schedules --region ap-southeast-2 --query 'Schedules[].{Name:Name,Group:GroupName,State:State}' --output table
aws events list-rules --region ap-southeast-2 --query "Rules[?contains(Name,'fieldsight') || contains(Name,'sitesync')].{Name:Name,State:State,Cron:ScheduleExpression}" --output table
```
Record every ENABLED cron that triggers `fieldsight-orchestrator`, `fieldsight-report-generator`, `fieldsight-fargate-trigger` (BUG-32: check non-default groups, e.g. `sitesync`).
- [ ] **Step 5 — Amplify app account + dev branch env baseline.**
```bash
aws amplify get-app --app-id d2fssznicvuckr --region ap-southeast-2 --query 'app.{name:name,defaultDomain:defaultDomain}' || echo "NOT on 509194952652 — locate the owning account before Task 11"
aws amplify get-branch --app-id d2fssznicvuckr --branch-name dev --region ap-southeast-2 --query 'branch.environmentVariables'
```
Record dev's exact env map (Task 11 mirrors the flags, swapping only the URLs). If the app lives on another account, Task 11 runs with that account's credentials — everything else in this plan stays on 509194952652.
- [ ] **Step 6 — PGPASSWORD MATCH (the rotation trap).**
```bash
DB_SECRET_ARN=$(aws cloudformation list-exports --region ap-southeast-2 --query "Exports[?Name=='fieldsight-db-test-SecretArn'].Value" --output text)
SECRET=$(aws secretsmanager get-secret-value --secret-id "$DB_SECRET_ARN" --region ap-southeast-2 --query SecretString --output text | node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>console.log(JSON.parse(d).password))")
LIVE=$(aws lambda get-function-configuration --function-name fieldsight-test-rag-search --region ap-southeast-2 --query 'Environment.Variables.PGPASSWORD' --output text)
[ "$SECRET" = "$LIVE" ] && echo MATCH || echo "MISMATCH — STOP, resync per memory fieldsight-db-password-rotation-trap"
```
- [ ] **Step 7 — DynamoDB tables the prod stack will reference exist.**
```bash
for t in fieldsight-items fieldsight-reports fieldsight-audit fieldsight-users fieldsight-transcripts; do aws dynamodb describe-table --table-name $t --region ap-southeast-2 --query 'Table.TableStatus' --output text 2>/dev/null || echo "$t MISSING"; done
```
Expected: five × `ACTIVE`. A missing table is fine only if the corresponding param gets an existing substitute — resolve before Task 6.

### Task 1 — Template re-stage: `prod` = `fieldsight-prod` (repo-only; no AWS changes)

**Files:**
- Modify: `src/template.yaml:256-261` (Mappings), `:1264-1268` (UserPoolDomain), `:292-296` (StorageBucketPolicy), `:39-44` (Stage description), Parameters block (add `MultiTenantResolution`), `IngestFunction`/`ItemWriterFunction` env blocks (`:884-894`, `:1004-1014`), stale comments (`:22-28` W1001 note, `:44`)
- Modify: `samconfig.toml:28-62`

**Interfaces:**
- Produces: `Stage=prod` ⇒ every resource named `fieldsight-prod-*`; parameter `MultiTenantResolution` (String `'true'|'false'`, default `'false'`) ⇒ env `MULTI_TENANT_RESOLUTION` on Ingest + ItemWriter (consumed by Task 2 code); sam config env `prod` ⇒ stack `fieldsight-prod`.

- [ ] **Step 1: Re-point the prod prefix.** In `src/template.yaml` replace the Mappings block:
```yaml
Mappings:
  StageConfig:
    prod:
      Prefix: fieldsight-prod
    test:
      Prefix: fieldsight-test
```
Update the `Stage` parameter description (`:44`) to: `Deployment stage. prod deploys fieldsight-prod-*; test deploys fieldsight-test-*. Legacy hand-built fieldsight-* lambdas are NOT CFN-managed.`
- [ ] **Step 2: Stage-prefix the Cognito domain.** Replace `Domain: !Sub fieldsight-${AWS::AccountId}` with:
```yaml
      Domain: !Sub ["${P}-${AWS::AccountId}", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
```
(Note in the PR description: next test deploy REPLACES the test hosted-UI domain `fieldsight-509194952652` → `fieldsight-test-509194952652`; hosted UI is unused — UI does SRP with hardcoded pool, `fieldsight-ui/scripts/auth/cognito.js:42`.)
- [ ] **Step 3: Protect the lake bucket policy.** On `StorageBucketPolicy` add, directly under `Type:`:
```yaml
    DeletionPolicy: Retain
```
- [ ] **Step 4: Add the `MultiTenantResolution` parameter** (after `OrgUserPoolId`):
```yaml
  MultiTenantResolution:
    Type: String
    Default: 'false'
    AllowedValues: ['true', 'false']
    Description: >-
      When true, ingest/item-writer resolve the owning company per user folder
      via the identity directory (users.folder_name, globally unique). When
      false, all pipeline writes are pinned to COMPANY_NAME's company — the
      TEST stack keeps false so develop-code runs can never write customer rows.
```
and add to BOTH `IngestFunction` and `ItemWriterFunction` `Environment.Variables`:
```yaml
          MULTI_TENANT_RESOLUTION: !Ref MultiTenantResolution
```
- [ ] **Step 5: Comment hygiene.** Update the template header W1001 note (`:22-28`) and `TranscribeFunction`/`VadFunction` NOTE blocks: prod IS now SAM-deployed as `fieldsight-prod` (the *legacy hand-built* lambdas remain outside CFN). Keep it to comment text — zero behavior.
- [ ] **Step 6: Rewrite samconfig.** Replace `[default.deploy.parameters]` and `[prod.deploy.parameters]` (`samconfig.toml:28-62`) with a single prod env (delete the default-deploy foot-gun; keep `[default.build.parameters]`):
```toml
# ------------------------------------------------------------
# PROD — fieldsight-prod stack (customer-facing). CI-deployed by
# .github/workflows/deploy-prod.yml on main; manual deploys should
# go through that workflow (approval gate), not local `sam deploy`.
# ------------------------------------------------------------
[prod.build.parameters]
template_file = "src/template.yaml"
base_dir = "."
use_container = false

[prod.deploy.parameters]
stack_name = "fieldsight-prod"
region = "ap-southeast-2"
capabilities = "CAPABILITY_NAMED_IAM"
confirm_changeset = false
resolve_s3 = true
```
(All parameter_overrides come from the workflow — same pattern as test.)
- [ ] **Step 7: Validate.**
Run: `sam validate --template-file src/template.yaml --lint --region ap-southeast-2`
Expected: `is a valid SAM Template` with no new lint errors.
- [ ] **Step 8: Commit.**
```bash
git add src/template.yaml samconfig.toml
git commit -m "feat(prod-isolation): Stage=prod deploys fieldsight-prod-* (legacy names stay unmanaged); stage-prefixed Cognito domain; Retain lake bucket policy; MultiTenantResolution param"
```

### Task 2 — Multi-tenant identity bridge (prod chain resolves company per user folder)

**Files:**
- Create: `src/migrations/0012_folder_name_global_unique.sql`
- Modify: `src/repositories/users.py`, `src/repositories/companies.py`, `src/lambda_ingest.py:74-76,270-286`, `src/lambda_item_writer.py:121-139`
- Test: `tests/unit/test_lambda_ingest.py`, `tests/unit/test_lambda_item_writer.py`

**Interfaces:**
- Produces: `users.get_by_folder_name_global(conn, folder_name) -> dict | None`; `companies.get_company_by_id(conn, company_id) -> dict | None`; `lambda_ingest.resolve_company(conn, user_folder) -> dict | None` (row with `id`/`name`, or None only when the COMPANY_NAME fallback is missing too — callers keep the existing RuntimeError guard).
- Consumes: `MULTI_TENANT_RESOLUTION` env (Task 1).

- [ ] **Step 1: Migration.** Create `src/migrations/0012_folder_name_global_unique.sql`:
```sql
-- 0012: folder_name becomes GLOBALLY unique (was unique per company, 0007).
-- The shared-lake pipeline routes S3 objects to a tenant by folder_name alone
-- (users/{folder}/..., reports/{date}/{folder}/...) — two companies claiming
-- one folder would silently cross-attribute data. Fail loudly at onboarding
-- instead. Additive-only (shared-Aurora rule); safe on current data (single
-- company today).
CREATE UNIQUE INDEX idx_users_folder_global ON users (folder_name) WHERE folder_name IS NOT NULL;
```
- [ ] **Step 2: Write the failing tests.** In `tests/unit/test_lambda_ingest.py` (house style: monkeypatch module globals + repo fns, `FakeConn`):
```python
class TestResolveCompany:
    def test_pinned_when_multi_tenant_off(self, monkeypatch):
        monkeypatch.setattr(ing, "MULTI_TENANT", False)
        monkeypatch.setattr(ing.users, "get_by_folder_name_global",
                            lambda conn, f: (_ for _ in ()).throw(AssertionError("must not be called")))
        monkeypatch.setattr(ing.companies, "get_company_by_name",
                            lambda conn, name: {"id": "internal-co", "name": name})
        assert ing.resolve_company(FakeConn(), "Cust_User")["id"] == "internal-co"

    def test_global_folder_lookup_when_on(self, monkeypatch):
        monkeypatch.setattr(ing, "MULTI_TENANT", True)
        monkeypatch.setattr(ing.users, "get_by_folder_name_global",
                            lambda conn, f: {"id": "u1", "company_id": "cust-co", "folder_name": f})
        monkeypatch.setattr(ing.companies, "get_company_by_id",
                            lambda conn, cid: {"id": cid, "name": "Pilot Co"})
        assert ing.resolve_company(FakeConn(), "Cust_User")["id"] == "cust-co"

    def test_falls_back_to_pin_on_unknown_folder(self, monkeypatch):
        monkeypatch.setattr(ing, "MULTI_TENANT", True)
        monkeypatch.setattr(ing.users, "get_by_folder_name_global", lambda conn, f: None)
        monkeypatch.setattr(ing.companies, "get_company_by_name",
                            lambda conn, name: {"id": "internal-co", "name": name})
        assert ing.resolve_company(FakeConn(), "Legacy_Device")["id"] == "internal-co"
```
- [ ] **Step 3: Run to verify failure.**
Run: `python -m pytest tests/unit/test_lambda_ingest.py::TestResolveCompany -v`
Expected: FAIL — `AttributeError: module 'lambda_ingest' has no attribute 'resolve_company'` (and `MULTI_TENANT`).
- [ ] **Step 4: Implement.** `src/repositories/users.py` append:
```python
def get_by_folder_name_global(conn, folder_name) -> dict | None:
    """Cross-company folder lookup for the shared-lake pipeline (0012 makes
    folder_name globally unique, so at most one row)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE folder_name=%s",
        (folder_name,),
    ).fetchone()
```
`src/repositories/companies.py` append:
```python
def get_company_by_id(conn, company_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, name, industry, created_at FROM companies WHERE id=%s",
        (company_id,),
    ).fetchone()
```
`src/lambda_ingest.py` — below `COMPANY_NAME` (`:76`) add:
```python
MULTI_TENANT = os.environ.get("MULTI_TENANT_RESOLUTION", "false") == "true"


def resolve_company(conn, user_folder):
    """Owning company for a lake object. MULTI_TENANT (prod stack): the
    identity directory routes by globally-unique folder_name; unknown folders
    fall back to the COMPANY_NAME pin (internal). Pinned (test stack):
    develop-code runs can never write another company's rows."""
    if MULTI_TENANT:
        row = users.get_by_folder_name_global(conn, user_folder)
        if row and row["company_id"]:
            company = companies.get_company_by_id(conn, row["company_id"])
            if company is not None:
                return company
    return companies.get_company_by_name(conn, COMPANY_NAME)
```
In `ingest_report` replace `company = companies.get_company_by_name(conn, COMPANY_NAME)` (`:272`) with `company = resolve_company(conn, user_folder)` (keep the existing `if company is None: raise RuntimeError` guard verbatim). In `src/lambda_item_writer.py:121` replace `company = companies.get_company_by_name(conn, COMPANY_NAME)` with `company = lambda_ingest.resolve_company(conn, user_folder)` (module already imports `lambda_ingest`; keep its None guard).
- [ ] **Step 5: Run tests.**
Run: `python -m pytest tests/unit/test_lambda_ingest.py tests/unit/test_lambda_item_writer.py -v`
Expected: new tests PASS. NOTE a known breakage to fix here: existing item-writer tests that monkeypatch `item_writer.companies.get_company_by_name` now miss — the call goes through `lambda_ingest.resolve_company`, which reads `lambda_ingest.companies`. Re-target those monkeypatches to `lambda_ingest.companies.get_company_by_name` (or monkeypatch `lambda_ingest.resolve_company` directly). `MULTI_TENANT` defaults false in tests, so the pinned path's behavior is otherwise unchanged.
- [ ] **Step 6: Commit.**
```bash
git add src/migrations/0012_folder_name_global_unique.sql src/repositories/users.py src/repositories/companies.py src/lambda_ingest.py src/lambda_item_writer.py tests/unit/test_lambda_ingest.py tests/unit/test_lambda_item_writer.py
git commit -m "feat(prod-isolation): per-tenant company resolution on the shared lake (MULTI_TENANT_RESOLUTION), globally-unique folder_name (0012)"
```

### Task 3 — wire-s3-events.sh: prod-stack prefix + legacy-entry retirement

**Files:**
- Modify: `scripts/wire-s3-events.sh:24-37` (prefix derivation), `:143-150` (merge jq)

**Interfaces:**
- Produces: `wire-s3-events.sh <bucket> prod <region> [--apply]` targets `fieldsight-prod-*`; env `RETIRE_IDS="id1,id2"` drops those non-`fs-` entries in the same atomic PUT (Task 8 consumes this).

- [ ] **Step 1: Prefix derivation.** Replace `PREFIX="fieldsight"; [ "$STAGE" = "test" ] && PREFIX="fieldsight-test"` (`:30`) with:
```bash
case "$STAGE" in
  test) PREFIX="fieldsight-test" ;;
  prod) PREFIX="fieldsight-prod" ;;
  *) echo "unknown stage '$STAGE' (test|prod)"; exit 1 ;;
esac
```
- [ ] **Step 2: Retirement support.** Replace the MERGED jq (`:144-150`) with:
```bash
# RETIRE_IDS: comma-separated non-"fs-" notification Ids to DROP in this same
# atomic PUT (the legacy hand-named lake entries, e.g. vad-on-users). Default
# empty = preserve all non-fs entries, exactly as before.
RETIRE_JSON=$(printf '%s' "${RETIRE_IDS:-}" | jq -R 'split(",") | map(select(length>0))')
MERGED=$(jq -n --argjson cur "$CURRENT" --argjson des "$DESIRED" --argjson retire "$RETIRE_JSON" '
  ($cur.LambdaFunctionConfigurations // []) as $lam
  | { LambdaFunctionConfigurations: (($lam | map(select((.Id | startswith("fs-") | not) and (.Id as $i | $retire | index($i) | not)))) + $des) }
  + ( if $cur.TopicConfigurations    then {TopicConfigurations:    $cur.TopicConfigurations}    else {} end )
  + ( if $cur.QueueConfigurations    then {QueueConfigurations:    $cur.QueueConfigurations}    else {} end )
  + ( if $cur.EventBridgeConfiguration then {EventBridgeConfiguration: $cur.EventBridgeConfiguration} else {} end )
')
```
- [ ] **Step 3: Verify no behavior change for test (dry-run).**
Run: `bash scripts/wire-s3-events.sh fieldsight-data-test-509194952652 test ap-southeast-2`
Expected: DESIRED list identical to CURRENT `fs-*` set (byte-for-byte same ARNs), `DRY-RUN` footer, exit 0.
- [ ] **Step 4: Commit.**
```bash
git add scripts/wire-s3-events.sh
git commit -m "feat(prod-isolation): wire-s3-events targets fieldsight-prod-* for stage prod; RETIRE_IDS drops named legacy entries atomically"
```

### Task 4 — deploy-prod.yml (main → SAM fieldsight-prod) + neuter deploy-prod-code.yml

**Files:**
- Create: `.github/workflows/deploy-prod.yml`
- Modify: `.github/workflows/deploy-prod-code.yml:13-20` (drop `push:`)

**Interfaces:**
- Consumes: repo variables `PROD_VAD_LAYER_ARN`, `PROD_DOCX_LAYER_ARN` (Task 0 Step 2), `PROD_ENABLE_SCHEDULES` (default `'false'`; flipped in Task 10), `PROD_WIRE_LAKE` (default `'false'`; flipped in Task 8); existing secrets (`AWS_ROLE_ARN`, `REALPTT_*`, `CLAUDE_API_KEY`, `DASHSCOPE_API_KEY`, `FARGATE_*`); GitHub environment `production` (already exists with required reviewers).
- Produces: on push to `main`, the `fieldsight-prod` stack (approval-gated), migrated, smoke-tested; lake events wired only when `PROD_WIRE_LAKE=='true'`.

- [ ] **Step 1: Neuter the legacy code deploy.** In `.github/workflows/deploy-prod-code.yml` replace the `on:` block with:
```yaml
on:
  workflow_dispatch:        # legacy lambdas are FROZEN — manual escape hatch only.
                            # Retired fully once the fieldsight-prod stack serves customers (prod-isolation plan Task 14).
```
(GitHub evaluates workflow files at the pushed commit, so the first promotion merge carrying this change will NOT trigger the old workflow.)
- [ ] **Step 2: Create `.github/workflows/deploy-prod.yml`:**
```yaml
name: Deploy FieldSight PROD (SAM)

# ============================================================
# main → fieldsight-prod stack (customer-facing) via full SAM deploy.
# Approval-gated by the `production` GitHub environment.
# Shares Aurora / RAG / the S3 lake with fieldsight-test — tenant
# isolation is row-level (company_id/site). See
# docs/superpowers/plans/2026-07-14-prod-isolation.md.
# ============================================================

on:
  workflow_dispatch:
  push:
    branches: [main]
    paths-ignore:
      - '**/*.md'
      - 'docs/**'
      - 'ui/**'
      - 'frontend/**'

permissions:
  id-token: write
  contents: read

env:
  AWS_REGION: ap-southeast-2

jobs:
  deploy-prod:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    environment: production       # required-reviewer approval gate
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}
      - uses: aws-actions/setup-sam@v2
        with: { use-installer: true }
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }

      - name: PGPASSWORD-MATCH gate (rotation trap — do not deploy on mismatch)
        run: |
          set -e
          DB_SECRET_ARN="$(aws cloudformation list-exports --query "Exports[?Name=='fieldsight-db-test-SecretArn'].Value" --output text)"
          SECRET="$(aws secretsmanager get-secret-value --secret-id "$DB_SECRET_ARN" --query SecretString --output text | jq -r .password)"
          LIVE="$(aws lambda get-function-configuration --function-name fieldsight-test-rag-search --query 'Environment.Variables.PGPASSWORD' --output text 2>/dev/null || echo NONE)"
          if [ "$LIVE" != "NONE" ] && [ "$SECRET" != "$LIVE" ]; then
            echo "::error::DB secret != live PGPASSWORD — the rotation trap is live. Resync before deploying (memory: fieldsight-db-password-rotation-trap)."; exit 1
          fi
          echo "MATCH"

      - name: SAM validate
        run: sam validate --template-file src/template.yaml --lint --region ${{ env.AWS_REGION }}

      - name: SAM build
        run: sam build --template-file src/template.yaml --base-dir .

      - name: SAM deploy (PROD stack)
        env:
          REALPTT_ACCOUNT: ${{ secrets.REALPTT_ACCOUNT }}
          REALPTT_PASSWORD: ${{ secrets.REALPTT_PASSWORD }}
          CLAUDE_API_KEY: ${{ secrets.CLAUDE_API_KEY }}
          DASHSCOPE_API_KEY: ${{ secrets.DASHSCOPE_API_KEY }}
          FARGATE_SUBNET_IDS: ${{ secrets.FARGATE_SUBNET_IDS }}
          FARGATE_VPC_ID: ${{ secrets.FARGATE_VPC_ID }}
        run: |
          VPC="$(echo "$FARGATE_VPC_ID" | xargs)"
          SUBNETS="$(echo "$FARGATE_SUBNET_IDS" | tr ',' ' ' | xargs | tr ' ' ',')"
          DB_SECRET_ARN="$(aws cloudformation list-exports --query "Exports[?Name=='fieldsight-db-test-SecretArn'].Value" --output text)"
          sam deploy --config-env prod \
            --no-confirm-changeset --no-fail-on-empty-changeset \
            --parameter-overrides \
              "Stage=prod" \
              "DataBucketName=fieldsight-data-509194952652" \
              "IngestBucketName=fieldsight-data-509194952652" \
              "EnableSchedules=${{ vars.PROD_ENABLE_SCHEDULES || 'false' }}" \
              "ItemsTableName=fieldsight-items" \
              "ReportsTableName=fieldsight-reports" \
              "AuditTableName=fieldsight-audit" \
              "MultiTenantResolution=true" \
              "VadLayerArn=${{ vars.PROD_VAD_LAYER_ARN }}" \
              "DocxLayerArn=${{ vars.PROD_DOCX_LAYER_ARN }}" \
              "RealPTTAccount=$REALPTT_ACCOUNT" \
              "RealPTTPassword=$REALPTT_PASSWORD" \
              "ClaudeApiKey=$CLAUDE_API_KEY" \
              "DashScopeApiKey=$DASHSCOPE_API_KEY" \
              "FargateSubnetIds=$SUBNETS" \
              "FargateVpcId=$VPC" \
              "DbStackName=fieldsight-db-test" \
              "DbSubnetIds=subnet-082dd4480f7e20014,subnet-08b15b36113e542d4,subnet-05fb05613cf529121" \
              "DbSecretArn=$DB_SECRET_ARN" \
              "OrgUserPoolId=ap-southeast-2_q88pd6XXr"

      - name: Apply DB migrations (fieldsight-prod-migrate — idempotent, shared schema_migrations)
        run: |
          set -e
          RESP="$(aws lambda invoke --function-name fieldsight-prod-migrate \
            --cli-binary-format raw-in-base64-out --payload '{}' \
            /tmp/migrate-out.json --region ${{ env.AWS_REGION }})"
          echo "invoke meta: $RESP"; cat /tmp/migrate-out.json; echo
          if echo "$RESP" | grep -q '"FunctionError"'; then
            echo "::error::MigrateFunction raised — see payload above"; exit 1
          fi

      - name: Wire LAKE S3 events (dry-run until PROD_WIRE_LAKE=true — Task 8 flips it)
        env:
          RETIRE_IDS: ${{ vars.PROD_RETIRE_IDS }}
        run: |
          if [ "${{ vars.PROD_WIRE_LAKE }}" = "true" ]; then
            bash scripts/wire-s3-events.sh fieldsight-data-509194952652 prod ${{ env.AWS_REGION }} --apply
          else
            bash scripts/wire-s3-events.sh fieldsight-data-509194952652 prod ${{ env.AWS_REGION }}
          fi

      - name: Smoke test /api/health (PROD stack)
        run: bash scripts/smoke-test.sh fieldsight-prod ${{ env.AWS_REGION }}
```
- [ ] **Step 3: Sanity-check both workflow files.**
Run: `node -e "console.log('yaml parse skipped')" && sam validate --template-file src/template.yaml --lint --region ap-southeast-2` and eyeball the YAML indentation (GitHub will lint on push; there is no local actions linter in this repo).
- [ ] **Step 4: Set the repo variables (GitHub → Settings → Variables → Actions), using Task 0 Step 2 values:** `PROD_VAD_LAYER_ARN`, `PROD_DOCX_LAYER_ARN`, `PROD_ENABLE_SCHEDULES=false`, `PROD_WIRE_LAKE=false`, `PROD_RETIRE_IDS=` (empty for now; Task 8 sets it).
- [ ] **Step 5: Commit.**
```bash
git add .github/workflows/deploy-prod.yml .github/workflows/deploy-prod-code.yml
git commit -m "feat(prod-isolation): main deploys fieldsight-prod via SAM (approval-gated, PGPASSWORD gate); legacy code-deploy loses its push trigger"
```

### Task 5 — Lake bucket-policy/CORS reconciliation (pre-promotion gate)

**Files:**
- Modify: `src/template.yaml:292-311` (StorageBucketPolicy) — only if Task 0 Step 3 found extra statements.

- [ ] **Step 1:** Diff the live lake policy (Task 0 Step 3 output) against the template's two Transcribe statements (`src/template.yaml:299-311`). If the live policy contains ANY additional statements, add them verbatim to the template's `PolicyDocument.Statement` list (CFN will own the merged policy; `PutBucketPolicy` replaces wholesale).
- [ ] **Step 2:** CORS: the prod stack does NOT auto-apply CORS to the lake (deploy-prod.yml deliberately omits `wire-bucket-cors.sh` — it REPLACES the whole config, `scripts/wire-bucket-cors.sh:4-5`). If Task 0 found no lake CORS, run it ONCE manually (customer browser uploads for org-assets need it; the rule already allows `https://*.amplifyapp.com`):
```bash
bash scripts/wire-bucket-cors.sh fieldsight-data-509194952652 ap-southeast-2
```
If Task 0 found an existing lake CORS config, merge its rules into a manual `put-bucket-cors` instead — do NOT run the replace-script blind.
- [ ] **Step 3:** Commit any template change:
```bash
git add src/template.yaml
git commit -m "chore(prod-isolation): carry live lake bucket-policy statements into StorageBucketPolicy before CFN takes ownership"
```

### Task 6 — First promotion: develop → main → fieldsight-prod stack exists

- [ ] **Step 1: Pre-flight.** Re-run Task 0 Step 6 (PGPASSWORD MATCH). Confirm Tasks 1-5 are merged into `develop` and the test deploy is green (the develop push after each merge auto-deploys `fieldsight-test`; confirm the template changes — notably the Cognito domain replacement — deployed cleanly there FIRST).
- [ ] **Step 2: Open the promotion PR** `develop → main` (title: `release: first fieldsight-prod promotion`). Confirm in the PR's file list that `.github/workflows/deploy-prod-code.yml` shows the neutered `on:` block and `.github/workflows/deploy-prod.yml` is added.
- [ ] **Step 3: User merges → approve the `production` environment gate** when `deploy-prod.yml` requests it.
- [ ] **Step 4: Verify the new stack (all read-only):**
```bash
aws cloudformation describe-stacks --stack-name fieldsight-prod --region ap-southeast-2 --query 'Stacks[0].StackStatus'
# expect CREATE_COMPLETE
aws cloudformation describe-stacks --stack-name fieldsight-prod --region ap-southeast-2 --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text
# RECORD this URL — it is FS_BASEURL/FS_ORG_BASEURL for the customer UI (Task 11)
aws lambda get-function-configuration --function-name fieldsight-prod-ingest --region ap-southeast-2 --query 'Environment.Variables.{S3:S3_BUCKET,MT:MULTI_TENANT_RESOLUTION}'
# expect S3=fieldsight-data-509194952652, MT=true
aws s3api get-bucket-notification-configuration --bucket fieldsight-data-509194952652 --region ap-southeast-2 | jq -c '.LambdaFunctionConfigurations[].LambdaFunctionArn'
# expect UNCHANGED (still fieldsight-test-* + legacy) — PROD_WIRE_LAKE is false
aws s3api get-bucket-policy --bucket fieldsight-data-509194952652 --region ap-southeast-2 --output text | jq '.Statement | length'
# expect: same statement count as the Task 5 reconciled set
```
- [ ] **Step 5: Verify nothing existing broke:** dev site loads (timeline + org pages); `bash scripts/smoke-test.sh fieldsight-test ap-southeast-2` passes; legacy `curl https://khfj3p1fkb.execute-api.ap-southeast-2.amazonaws.com/prod/api/health` returns 200.

### Task 7 — Pre-verify the prod chain by synthetic invokes (lake events still on test)

Every prod-chain lambda is exercised against REAL lake objects via direct invoke BEFORE any event flips. Writes are confined to the internal company (same `source_s3_key` ⇒ replace semantics, `lambda_ingest.py:288-297`). Run before 05:00 NZDT if verifying extraction topics (memory: same-day wipe until authority-flip Task 9).

- [ ] **Step 1:** Pick a recent internal report day `<D>`/`<U>` that already has lake artifacts (`aws s3 ls s3://fieldsight-data-509194952652/reports/<D>/`).
- [ ] **Step 2:** Record pre-state row counts (Aurora Data API or via a `fieldsight-test-rag-search`-style probe):
```bash
aws rds-data execute-statement --resource-arn <CLUSTER_ARN> --secret-arn <DB_SECRET_ARN> --database fieldsight \
  --sql "SELECT count(*) FROM topics WHERE source_s3_key='reports/<D>/<U>/daily_report.json'" --region ap-southeast-2
```
- [ ] **Step 3:** Invoke the prod chain end-to-end via its native manual payloads:
```bash
aws lambda invoke --function-name fieldsight-prod-embed-report --region ap-southeast-2 \
  --cli-binary-format raw-in-base64-out --payload '{"date":"<D>","user":"<U>"}' /tmp/embed.json && cat /tmp/embed.json
aws lambda invoke --function-name fieldsight-prod-ingest --region ap-southeast-2 \
  --cli-binary-format raw-in-base64-out --payload '{"date":"<D>","user":"<U>"}' /tmp/ingest.json && cat /tmp/ingest.json
```
Expected: no `FunctionError`; ingest reports processed (not skipped); post-state row count from Step 2 identical (replace, not duplicate).
- [ ] **Step 4:** Synthetic S3-event invoke for extract-session + item-writer against one real transcript key (S3 event JSON with `bucket.name=fieldsight-data-509194952652`, `object.key=<a transcripts/.../*.json key>`); verify CloudWatch logs show a clean run (`aws logs tail /aws/lambda/fieldsight-prod-extract-session --since 5m`).
- [ ] **Step 5:** Verify vad/transcribe cold-readiness (no invoke — they'd re-emit segments): `aws lambda get-function-configuration --function-name fieldsight-prod-vad --query '{Layers:Layers[].Arn,Env:Environment.Variables.SILERO_MODEL_S3_KEY}'` — expect the layer ARN + `models/silero_vad.onnx` (BUG-02).

### Task 8 — THE FLIP: lake S3 events → fieldsight-prod-* (atomic, rehearsed rollback)

- [ ] **Step 1: Backup.**
```bash
aws s3api get-bucket-notification-configuration --bucket fieldsight-data-509194952652 --region ap-southeast-2 --output json > /tmp/lake-notif-preflip-$(date +%s).json
```
Keep this file — it IS the rollback.
- [ ] **Step 2: Dry-run.**
```bash
RETIRE_IDS="<legacy-vad-id>,<legacy-transcribe-id>" bash scripts/wire-s3-events.sh fieldsight-data-509194952652 prod ap-southeast-2
```
(ids verbatim from Task 0 Step 1). Expected DESIRED: 9 `fs-*` entries ALL pointing at `fieldsight-prod-*` (vad-wav, vad-mp4, transcribe-wav, embed-report, ingest-report, extract-transcripts, write-extractions, programme-match), legacy ids ABSENT, any other non-fs entries preserved.
- [ ] **Step 3: Apply.** Same command + `--apply`. (S3 would reject any accidental overlap with a remaining legacy entry — the retire list makes the PUT internally consistent; a rejection here means the RETIRE_IDS were wrong: nothing was changed, fix and re-run.)
- [ ] **Step 4: Set repo variables** `PROD_WIRE_LAKE=true`, `PROD_RETIRE_IDS=<legacy-vad-id>,<legacy-transcribe-id>` so future prod deploys keep the wiring idempotently.
- [ ] **Step 5: Live E2E through the prod chain.** Re-trigger one existing internal recording:
```bash
aws s3 cp s3://fieldsight-data-509194952652/<users/.../file.mp4> s3://fieldsight-data-509194952652/<same-key> --metadata-directive REPLACE --region ap-southeast-2
```
Then follow the chain: `aws logs tail /aws/lambda/fieldsight-prod-vad --since 10m --follow` → segments in `audio_segments/` → `fieldsight-prod-transcribe` job → transcript JSON → `fieldsight-prod-extract-session` → `extractions/` → `fieldsight-prod-item-writer` → Aurora topic visible in `/live-items` on the dev site (before 05:00 NZDT).
- [ ] **Step 6: Prove single-processing.** `aws logs tail /aws/lambda/fieldsight-test-vad --since 15m` (function doesn't exist — expect no log group) and `/aws/lambda/fieldsight-test-extract-session`, `/aws/lambda/fieldsight-vad` — expect ZERO invocations since the flip. Exactly one chain fired.
- [ ] **Rollback (if anything above fails):**
```bash
aws s3api put-bucket-notification-configuration --bucket fieldsight-data-509194952652 --notification-configuration file://$(cygpath -w /tmp/lake-notif-preflip-<ts>.json) --region ap-southeast-2
```
plus `PROD_WIRE_LAKE=false`. The lake is back on the test chain + legacy front pipeline in one call.

### Task 9 — Test stack becomes fully test-bucket-scoped

**Files:**
- Modify: `.github/workflows/deploy.yml:70-88` (add one override)

- [ ] **Step 1:** In deploy.yml's `sam deploy` overrides add:
```yaml
              "IngestBucketName=fieldsight-data-test-509194952652" \
```
(Effect: test extract/embed/ingest/item-writer/matcher read+write ONLY the test bucket; manual lake backfills now run against `fieldsight-prod-*` functions — the owning chain.)
- [ ] **Step 2:** Document the fixture-iteration workflow in `DEPLOYMENT-RUNBOOK.md` (new section): copy a lake object into the test bucket (`aws s3 cp s3://fieldsight-data-509194952652/<key> s3://fieldsight-data-test-509194952652/<key>`) → the develop-deployed chain runs it end-to-end → Aurora rows for that `source_s3_key` are REPLACED under the internal company (never a customer's — `MULTI_TENANT_RESOLUTION=false` pin).
- [ ] **Step 3:** PR → merge → confirm the auto test deploy, then:
```bash
aws lambda get-function-configuration --function-name fieldsight-test-ingest --region ap-southeast-2 --query 'Environment.Variables.S3_BUCKET'
# expect fieldsight-data-test-509194952652
```
- [ ] **Step 4: Commit** (with Step 2's doc):
```bash
git add .github/workflows/deploy.yml DEPLOYMENT-RUNBOOK.md
git commit -m "feat(prod-isolation): test stack pipeline is test-bucket-scoped (lake is owned by fieldsight-prod)"
```

### Task 10 — Schedules cutover: legacy crons off, prod-stack schedules on (single owner)

Until now the legacy `fieldsight-orchestrator`/`fieldsight-report-generator`/`fieldsight-fargate-trigger` crons still run (front-pipeline downloads + nightly reports). Hand over in one evening window (before 20:00 NZDT = UTC 07:00 nightly kick).

- [ ] **Step 1:** Disable every ENABLED legacy cron from Task 0 Step 4 (EventBridge Scheduler and/or CloudWatch Events; BUG-32 group awareness):
```bash
aws scheduler update-schedule --name <NAME> --group-name <GROUP> --state DISABLED --region ap-southeast-2 --schedule-expression '<existing>' --flexible-time-window Mode=OFF --target '<existing target json>'   # scheduler requires full re-spec; capture with get-schedule first
# or for classic rules:
aws events disable-rule --name <RULE> --region ap-southeast-2
```
Record each disabled name for rollback.
- [ ] **Step 2:** Set repo variable `PROD_ENABLE_SCHEDULES=true`, then re-run `deploy-prod.yml` via `workflow_dispatch` (main HEAD). Approve the gate.
- [ ] **Step 3:** Verify the prod stack's schedules are ENABLED and the legacy ones DISABLED:
```bash
aws events list-rules --region ap-southeast-2 --query "Rules[?contains(Name,'fieldsight-prod')].{Name:Name,State:State}" --output table
```
- [ ] **Step 4 (next morning):** exactly ONE download sweep ran (`/aws/lambda/fieldsight-prod-orchestrator` logs; `/aws/lambda/fieldsight-orchestrator` silent); exactly ONE `daily_report.json` per user (S3 `LastModified` singletons); embed→ingest fired on the prod chain; dev-site timeline shows the day normally.
- [ ] **Rollback:** re-enable the recorded legacy crons + set `PROD_ENABLE_SCHEDULES=false` + redeploy — the legacy front pipeline resumes; lake events (Task 8) still point at prod-* extraction, which is fine (it processes whatever lands in the lake).

### Task 11 — Customer UI: Amplify `main` branch + prod gateway env

UI repo work + Amplify console/CLI (account per Task 0 Step 5).

- [ ] **Step 1:** UI promotion PR `dev → main` in `fieldsight-ui` (main is an ancestor — clean). User merges.
- [ ] **Step 2:** Create + configure the branch (use `<PROD_API>` from Task 6 Step 4; mirror dev's flag values from Task 0 Step 5, mocks OFF):
```bash
aws amplify create-branch --app-id d2fssznicvuckr --branch-name main --region ap-southeast-2 --description "customer-facing (fieldsight-prod stack)"
aws amplify update-branch --app-id d2fssznicvuckr --branch-name main --region ap-southeast-2 --environment-variables FS_BASEURL=<PROD_API>,FS_ORG_BASEURL=<PROD_API>,FS_USEMOCKS=false,FS_WRITEMOCKS=false,FS_ORGWRITES=true
aws amplify start-job --app-id d2fssznicvuckr --branch-name main --job-type RELEASE --region ap-southeast-2
```
(If the authority-flip UI flag has shipped, also set `FS_TIMELINE_SOURCE=org` so customer timeline reads the org shim — D8.)
- [ ] **Step 3:** Verify `https://main.d2fssznicvuckr.amplifyapp.com`: `view-source` env.js carries the prod URLs; login with an INTERNAL admin (shared pool) → org pages, `/live-items`, ask, search all live against the prod stack; browser devtools shows zero requests to `khfj3p1fkb` or `wdsgobb7b0`.
- [ ] **Step 4:** Confirm the dev site is untouched (dev.d2fssznicvuckr.amplifyapp.com still on legacy+test gateways).

### Task 12 — Internal-company confirmation + customer onboarding runbook + live ACL verification

**Files:**
- Create: `docs/CUSTOMER-ONBOARDING.md`

- [ ] **Step 1:** Write `docs/CUSTOMER-ONBOARDING.md` covering, with exact commands: (1) create the company row (Aurora Data API `INSERT INTO companies (name) VALUES (…) RETURNING id`); (2) create its first site (`INSERT INTO sites (company_id, name, slug) …` — slug unique per company, 0007); (3) invite the customer admin via org-api `POST /api/org/members` AS a user of that company (bootstrap: first admin row via SQL — `users(company_id, email, global_role='admin', cognito_sub=<sub>)` after `admin-create-user` in pool `ap-southeast-2_q88pd6XXr`); (4) field recorders: `INSERT INTO users (company_id, folder_name, kind='field_only', global_role='worker', first_name, last_name, email='')` + membership rows — folder_name must be globally unique (0012 enforces; a collision = onboarding error, pick another device folder); (5) the HARD RULE: customers get NO entries in DynamoDB `fieldsight-users` or `config/user_mapping.json` (legacy-surface leak vector, §0.3); (6) known v1 gaps (D7) verbatim so sales/support expectations are set.
- [ ] **Step 2:** Execute the runbook once for a pilot/dummy company (`Pilot Co`) end-to-end.
- [ ] **Step 3:** Live cross-tenant verification (log in as the Pilot Co admin on the `main` site): `/live-items` → empty (not internal data); ask → "no relevant records"-style grounded empty (rag-search sees only Pilot Co's zero sites); `/api/search` → empty; org pages → only Pilot Co; `/api/timeline` (legacy shape via FS_BASEURL) → 404/`available_users: []`, NOT internal reports; `/api/media/presigned-url?key=users/<internal-user>/...` → 403. Then the inverse: internal admin on the dev site sees NO Pilot Co rows in `/live-items` (company-scoped ACL).
- [ ] **Step 4:** Upload one test recording via the Pilot Co flow (org-api presign → lake `users/<folder>/…`) → prod chain → verify the Aurora topic lands under Pilot Co's site (Task 2's global folder resolution) and appears in the Pilot admin's `/live-items` — and does NOT appear for internal users.
- [ ] **Step 5: Commit.**
```bash
git add docs/CUSTOMER-ONBOARDING.md
git commit -m "docs(prod-isolation): customer onboarding runbook + verified cross-tenant ACL checklist"
```

### Task 13 — One-time internal programme copy (test bucket → lake)

- [ ] **Step 1:** `aws s3 sync s3://fieldsight-data-test-509194952652/programmes/ s3://fieldsight-data-509194952652/programmes/ --region ap-southeast-2`
- [ ] **Step 2:** Verify `fieldsight-prod-programme-matcher` can read one internal programme (`aws s3 ls s3://fieldsight-data-509194952652/programmes/`), and note the D10 drift caveat in `DEPLOYMENT-RUNBOOK.md` (dev-site programme edits land in the test bucket; re-sync when internal impact-matching matters).

### Task 14 — Retirement + docs (deploy-prod-code.yml deleted; two-stack reality documented)

**Files:**
- Delete: `.github/workflows/deploy-prod-code.yml`
- Modify: `DEPLOYMENT-RUNBOOK.md`, `CLAUDE.md` (pipeline), `docs/superpowers/plans/2026-07-14-authority-flip.md` (its Task 0 expectations note)

- [ ] **Step 1:** After ≥1 week of prod-stack soak (Tasks 8+10 stable): `git rm .github/workflows/deploy-prod-code.yml` (the frozen legacy lambdas remain deployed — `fieldsight-api`/`fieldsight-ask-agent`/`fieldsight-meeting-minutes` still serve the dev site via `khfj3p1fkb`; emergency code pushes would use `scripts/deploy-lambda-code.sh` by hand).
- [ ] **Step 2:** `DEPLOYMENT-RUNBOOK.md`: rewrite the environments section — three tiers: `fieldsight-test` (develop, test bucket, internal-pinned), `fieldsight-prod` (main, lake owner, multi-tenant), legacy `fieldsight-*` (frozen dev-site shim; retirement blocked on authority-flip + org media presigns). Include: promotion checklist (PGPASSWORD gate, additive-migration rule, approve `production` gate), the lake-flip rollback drill (Task 8), the schedules rollback (Task 10), and the fixture workflow (Task 9).
- [ ] **Step 3:** `CLAUDE.md`: update the architecture header (two stacks + frozen legacy; lake ownership; customer tenancy rules) and add the "customers never enter DynamoDB users / user_mapping.json" rule beside BUG-25.
- [ ] **Step 4:** Annotate `2026-07-14-authority-flip.md` Task 0 Step 1: post-prod-isolation, the lake chain routes to `fieldsight-prod-*`; its ingest-flag work targets whichever stack owns the lake at execution time (env param exists on both).
- [ ] **Step 5: Commit.**
```bash
git add -u
git commit -m "chore(prod-isolation): retire legacy code-deploy workflow; document two-stack promotion + rollback drills"
```

---

## Risks (ranked)

1. **Lake event flip drops a stage entry** (a typo'd RETIRE id or missing fs- entry ⇒ a pipeline stage silently stops for ALL tenants). Mitigations: S3's atomic PUT + overlap rejection (a half-double config is unconfigurable), dry-run diff (Task 8 Step 2), pre-flip backup JSON = one-call rollback, live E2E re-trigger immediately after (Step 5). Note the inverse risk (double-processing) is structurally impossible at the S3 layer.
2. **First promotion triggering the OLD deploy-prod-code.yml** would push ~50 PRs of drifted code onto the hand-built lambdas (env/layer mismatch ⇒ dev-site timeline dies). Mitigation: Task 4 Step 1 neuters the trigger IN the promotion merge itself; Task 6 Step 2 verifies it in the PR diff.
3. **Cross-tenant leak via the legacy surface**: any customer identity added to DynamoDB `fieldsight-users` or `user_mapping.json` with admin/gm bypasses all scoping (`lambda_fieldsight_api.py:317,386`). Mitigation: hard onboarding rule (Task 12 Step 1.5) + live 403 verification (Task 12 Step 3). RAG and org-api are verified company-scoped (§0.3) — no code change needed there.
4. **Shared-Aurora schema drift** between develop-deployed and main-deployed code. Mitigation: additive-only migration rule (Global Constraints), prompt promotions, migrations applied idempotently by both stacks' migrate lambdas.
5. **PGPASSWORD snapshot now ×2 stacks** — a secret change bricks BOTH stacks' in-VPC lambdas with "no records found" symptoms. Mitigation: MATCH gate in both workflows + Task 0; rotation stays disabled.
6. **Double nightly processing during the Task 8→10 window** (legacy report cron + prod chain both alive): by design legacy report-generator keeps producing `daily_report.json` (single writer — prod stack schedules stay off), and the prod chain consumes it; the only true double-run risk is enabling `PROD_ENABLE_SCHEDULES` before disabling legacy crons — Task 10 orders it explicitly.
7. **Customer pipeline data misattribution**: folder_name routing sends a customer's data to the wrong company if folders collide. Mitigation: 0012 global unique index fails onboarding loudly; unknown folders fall back to the INTERNAL company (never a customer).
8. **CFN clobbering the lake bucket policy / CORS**: reconciled before ownership (Task 5), `DeletionPolicy: Retain` (Task 1), CORS replace-script never run blind against the lake.
9. **Cognito domain replacement on the test stack** (Task 1 Step 2): hosted UI is unused (SRP, hardcoded pool) — verified low impact, but the test deploy after merging Task 1 is the checkpoint (Task 6 Step 1).

## Estimate

15 tasks across 5 phases (A: Tasks 1-5 repo groundwork ≈ 1-1.5 days; B: Task 6 first promotion ≈ 0.5 day incl. verification; C: Tasks 7-10 lake flip + cutover ≈ 1 day active + 1 overnight soak; D: Tasks 11-13 customer UI + onboarding ≈ 1 day; E: Task 14 after ≥1 week soak). Roughly 4-5 active working days plus soak windows.
