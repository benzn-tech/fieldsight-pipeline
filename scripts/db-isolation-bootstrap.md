# fieldsight_test Bootstrap + Cutover Runbook

> **WARNING: this procedure operates on the SHARED PROD AURORA CLUSTER
> (`fieldsight-db-test-dbcluster-hywiixu8ihi9`, account `509194952652`,
> region `ap-southeast-2`). It requires an authenticated AWS session
> (`aws sts get-caller-identity` must succeed) before any step below is run.**
>
> This is the OPERATOR-ONLY Phase 2 of
> `docs/superpowers/plans/2026-07-21-test-prod-db-isolation.md`. Subagents
> MUST NOT execute any command in this file. Run it yourself, step by step,
> verifying each before moving to the next.

**Hard order (do not reverse):** the `fieldsight_test` database MUST be
created and fully populated (Steps 2–3 below) **BEFORE** the `PgDatabase`
switch is deployed (Step 4). Reversing this order makes all 12 in-VPC
functions fail with `FATAL: database "fieldsight_test" does not exist` and
reds the test pipeline (see plan Global Constraints, M-2).

Background / design: `docs/superpowers/specs/2026-07-21-test-prod-db-isolation-design.md`.
Full task breakdown: `docs/superpowers/plans/2026-07-21-test-prod-db-isolation.md` (Tasks 4–8).

---

## Step 1: Pre-flight checks (COMPLETED 2026-07-21 — all green)

Verified: AWS session (`509194952652`); ClusterArn + SecretArn resolve; cluster
VPC = `vpc-0791974a474386d1c`; `fieldsight-db-test-LambdaSG` = `sg-0749d42a6d5696729`
(all-egress `0.0.0.0/0`, and Aurora `DbSG` `sg-0845ec1fea44de4ad` trusts it on
5432); DB subnets `subnet-08b15b36113e542d4/-05fb05613cf529121/-082dd4480f7e20014`
are public (VPC main route table has `0.0.0.0/0 → igw-0f12e5a0ff97b4479`);
**PGPASSWORD-MATCH ✓** (rotation trap not live); AL2023 AMI resolvable via SSM
parameter; `fieldsight` exists, `fieldsight_test` does not.

Re-run the identity + PGPASSWORD-MATCH checks if resuming on a later day.

---

## Step 2+3: Create + copy via a temporary EC2 (over SSM)

Chosen over Fargate for interactive debuggability; ~1 cent, and prod's
`fieldsight` is never touched. **Exact, verified commands are in the plan —
`docs/superpowers/plans/2026-07-21-test-prod-db-isolation.md`, Task 5 (provision
the throwaway EC2: IAM role+profile with `AmazonSSMManagedInstanceCore` +
`secretsmanager:GetSecretValue`, launch a `t3.small` AL2023 in
`subnet-08b15b36113e542d4` with a public IP and SG `sg-0749d42a6d5696729`, wait
for SSM `Online`) and Task 6 (send one SSM `AWS-RunShellScript`:
`dnf install -y postgresql16 jq` → read `PGPASSWORD` from Secrets Manager →
`createdb fieldsight_test` → `pg_dump -d fieldsight -Fc` → `pg_restore
--exit-on-error` → row-count parity via Data API → **terminate the instance and
delete the role/profile**).**

Key invariants: one SG (`sg-0749d42a6d5696729`) satisfies BOTH DB-ingress and
internet-egress; credentials via `PGHOST`/`PGUSER`/`PGPASSWORD` env, never a URI
DSN (RDS-managed password has URI-reserved chars, `src/db/connection.py:36-40`);
`createdb` on the host (no separate Data-API `CREATE DATABASE` step needed);
`pg_restore` auto-rebuilds the pgvector `CREATE EXTENSION` + `report_chunks` HNSW
index. Leave NOTHING running after the copy.

---

## Step 4: Deploy the switch (test only)

1. Add the override to the workflow. In `.github/workflows/deploy.yml`,
   append to the `--parameter-overrides` list (`:72-81`):
   ```yaml
                 "PgDatabase=fieldsight_test" \
   ```
   Do NOT touch `deploy-prod.yml` — prod keeps the default.

2. Merge the Phase-1 template change + this workflow change + the doc
   corrections to `develop`. Push to `develop` → `deploy.yml` runs a full SAM
   deploy of `fieldsight-test`. The post-deploy migrate now targets
   `fieldsight_test` (already fully migrated by the Step 3 copy) → no-op,
   green.

3. Verify the test functions repointed:
   ```bash
   for fn in fieldsight-test-org-api fieldsight-test-rag-search fieldsight-test-ingest; do
     aws lambda get-function-configuration --function-name "$fn" \
       --query "Environment.Variables.PGDATABASE" --output text
   done
   ```
   Expected: all print `fieldsight_test`.

---

## Step 5: Boundary verification (both directions)

1. Write via test, confirm it lands in test-DB only. Do a harmless write
   through `fieldsight-test-org-api` (e.g. `PATCH /api/org/sites/{id}` with a
   name change on a test-company site). Then query BOTH databases (Data API)
   for that row's new value:
   - `fieldsight_test`: shows the new value.
   - `fieldsight`: shows the OLD value (unchanged).

2. Write via prod, confirm the reverse. Do the same harmless write through
   `fieldsight-prod-org-api` on a prod row; confirm it appears in
   `fieldsight` but NOT in `fieldsight_test`. This proves isolation holds
   both ways.

3. Login smoke: a tester (shared Cognito) logs into the test app; confirm
   `GET /api/org/me` resolves from `fieldsight_test` (the copied `users` row)
   — non-empty profile, no "caller not provisioned".

---

## Rollback

1. Remove the `"PgDatabase=fieldsight_test"` line from
   `.github/workflows/deploy.yml`'s `--parameter-overrides` and redeploy the
   test stack (push to `develop`) → the `!If` falls back to the imported
   `fieldsight` → test instantly returns to the shared database.

2. Once the separation is confirmed unwanted, drop the test database via
   Data API:
   ```bash
   aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
     --database postgres --sql 'DROP DATABASE fieldsight_test;'
   ```

---

## Reference

- Cluster: `fieldsight-db-test-dbcluster-hywiixu8ihi9`, Aurora PostgreSQL 16.4.
- Prod DB: `fieldsight` (never modified by this procedure). Test DB:
  `fieldsight_test`.
- The 12 in-VPC `PGDATABASE` consumers: Migrate, OrgApi, OrgSeed, Ingest,
  ItemWriter, SuggestionWriter, RagSearch, VoiceAudit, VoiceReaper,
  WsConnect, WsDisconnect, VoiceResolve (`src/template.yaml:800, 833, 977,
  1070, 1214, 1282, 1416, 1450, 1483, 1545, 1573, 1603`). `EmbedReport` and
  the matcher are non-VPC and are NOT in scope.
- Invite-testing on test uses `+test`/fake-domain emails only.
- Experimental / throwaway migrations must NEVER be merged to `main`
  (`deploy-prod.yml` auto-runs `main`'s migrations on prod).
