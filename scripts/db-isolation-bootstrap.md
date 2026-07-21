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

## Step 1: Pre-flight checks

1. Confirm identity + region:
   ```bash
   aws sts get-caller-identity   # expect account 509194952652
   export AWS_DEFAULT_REGION=ap-southeast-2
   ```

2. Confirm the Fargate VPC == the DB VPC:
   ```bash
   echo "$FARGATE_VPC_ID"   # or read the GitHub secret
   ```
   Compare to `vpc-0791974a474386d1c` (`samconfig.toml:91`). If they differ,
   the copy task (Step 3) must run in the DB VPC's subnets, NOT
   `FARGATE_SUBNET_IDS`. Record the correct subnet + the exported SG
   `fieldsight-db-test-LambdaSG`.

3. Confirm pg client version on the chosen copy host (Fargate image
   `postgres:16` or EC2 with `postgresql-16-client`):
   ```bash
   pg_dump --version   # expect 16.x — below 16 refuses on server-version mismatch
   ```

4. Confirm PGPASSWORD-MATCH (rotation-trap guard) — the same check
   `deploy-prod.yml:45-54` uses: the DB secret's `password` must equal a live
   lambda's `PGPASSWORD`. If mismatched, resync before proceeding (memory:
   `fieldsight-db-password-rotation-trap`).

---

## Step 2: Create the `fieldsight_test` database (Data API — no VPC host needed)

1. Get the cluster ARN + secret ARN:
   ```bash
   CLUSTER_ARN=$(aws cloudformation list-exports --query "Exports[?Name=='fieldsight-db-test-ClusterArn'].Value" --output text)
   SECRET_ARN=$(aws cloudformation list-exports --query "Exports[?Name=='fieldsight-db-test-SecretArn'].Value" --output text)
   ```

2. Create the database. `CREATE DATABASE` cannot run inside a transaction;
   Data API auto-commits single statements (no explicit transaction id), so:
   ```bash
   aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
     --database postgres --sql 'CREATE DATABASE fieldsight_test;'
   ```
   Expected: `{"numberOfRecordsUpdated": 0}` (no error). If it errors with
   "cannot run inside a transaction block", fall back to `psql` from the
   copy host (Step 3) for this one statement.

3. Verify it exists:
   ```bash
   aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
     --database postgres --sql "SELECT datname FROM pg_database WHERE datname='fieldsight_test';"
   ```
   Expected: one row `fieldsight_test`.

---

## Step 3: Copy `fieldsight` → `fieldsight_test` (in-VPC pg-16 client)

1. Launch the copy host. Register a one-off ECS/Fargate task:
   - image `postgres:16`
   - security group `fieldsight-db-test-LambdaSG`
   - DB VPC subnets (the ones confirmed in Step 1.2)
   - task role with `secretsmanager:GetSecretValue` on `$SECRET_ARN`

   (OR a temporary EC2 in the DB subnets with `postgresql-16-client`.)

   Export on that host:
   ```bash
   export PGHOST=fieldsight-db-test-dbcluster-hywiixu8ihi9.cluster-ctugu28wme3y.ap-southeast-2.rds.amazonaws.com
   export PGUSER=postgres
   export PGPASSWORD='<from Secrets Manager — do NOT build a URI DSN; reserved chars, see src/db/connection.py:36-40>'
   ```

2. Dump prod (consistent snapshot, no downtime) + restore into test:
   ```bash
   pg_dump -d fieldsight -Fc -f /tmp/fieldsight.pgc
   pg_restore --dbname=fieldsight_test --no-owner --exit-on-error /tmp/fieldsight.pgc
   ```
   `--exit-on-error` (pg_restore) / `ON_ERROR_STOP=1` semantics so a partial
   failure is loud. pgvector `CREATE EXTENSION` + the `report_chunks` HNSW
   index rebuild happen automatically.

3. Verify the copy — row counts match on key tables (`users`, `topics`,
   `action_items`, `report_chunks`, `sites`), compared between the two
   databases (via Data API against each `--database`):
   ```bash
   for t in users topics action_items report_chunks sites; do
     for db in fieldsight fieldsight_test; do
       aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
         --database "$db" --sql "SELECT count(*) FROM $t;" --query 'records[0][0].longValue' --output text
     done
   done
   ```
   Expected: each table's two counts equal. Tear down the copy host afterward.

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
