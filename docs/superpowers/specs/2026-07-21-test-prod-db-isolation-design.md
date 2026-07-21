# Test/Prod Database Isolation — Design

**Date:** 2026-07-21
**Repo:** `fieldsight-pipeline` (account 509194952652, the user's own SAM pipeline — NOT the company CDK prod 164088480050)
**Status:** Approved design, pending spec review → implementation plan

## Problem

`fieldsight-test-*` and `fieldsight-prod-*` are separate SAM-deployed Lambda
stacks, but they share **one Aurora database** (`fieldsight` on cluster
`fieldsight-db-test-dbcluster-hywiixu8ihi9`) and **one Cognito pool**
(`ap-southeast-2_q88pd6XXr`). S3 buckets are already separate
(`fieldsight-data-test-509194952652` vs `fieldsight-data-509194952652`).

Consequences of the shared database, with live customers now on prod:

- A **test schema migration alters prod's schema** — the current mitigation is a
  standing "additive-only migrations" discipline constraint
  (`docs/superpowers/plans/2026-07-14-prod-isolation.md`), which blocks any
  drop/rename/retype and taxes every test change.
- **Cross-company / `platform_admin` operations** and any non-company-scoped
  write from test hit the same rows prod reads.
- A **destructive mistake** on the shared database (bad DELETE/DROP) reaches
  customer data.

The goal is to let test iterate aggressively — including non-additive schema
changes and risky data operations — with **zero risk to prod data**, at low
cost and effort.

## Decisions (approved)

1. **Isolation depth: separate logical database on the SAME Aurora cluster**
   (option B), not a separate cluster (option C). Rationale: isolates the actual
   risk (schema + data + independent migrations) at ~zero new infrastructure
   cost and with no cold-start penalty (prod keeps the cluster warm). Does NOT
   isolate cluster-level blast radius (capacity, maintenance, a cluster-wide
   `DROP`) — accepted at current scale; upgrade to a separate cluster later if
   that changes.
2. **Cognito: stays shared** (`q88pd6XXr`). Verified this pool is a template
   **parameter** (`OrgUserPoolId`, `src/template.yaml:246`, referenced via
   `!Ref`), NOT a resource the test stack manages — the template's own
   `AWS::Cognito::UserPool` (`src/template.yaml:1788`) is an unused
   stage-prefixed artifact. So test deploys cannot reconfigure the real pool;
   the only test→Cognito write is `admin_create_user` on invite. Authorization
   (company/role/membership) lives in the DB, which IS separated, so a shared
   `sub` is inert in prod without a prod `users` row. Revisit only if test needs
   to exercise auth-flow/pool-policy changes, or heavy invite testing pollutes
   the pool. **Hard rule (Fable m-4), not optional:** inviting a NEW email from
   test runs `admin_create_user` with `DesiredDeliveryMediums=["EMAIL"]`
   (`src/lambda_org_api.py:880-894`) — it really emails a real invite and creates
   a real pool user, and a later prod invite of that email reuses test's account
   and temp password. **Always use `+test`/fake-domain emails when invite-testing
   on test.** (Re-inviting an *existing* email is read-only —
   `UsernameExistsException → admin_get_user` — so it never disturbs prod users.)
3. **Test data: one-time `pg_dump` copy** of the current `fieldsight` database
   into the new `fieldsight_test`. Test begins as a snapshot of today's data,
   then diverges. Accepted tradeoff: real customer data is copied into test
   (privacy); anonymization of PII columns can be added to the dump later if
   needed — out of scope for v1.

## Architecture

| Component | Change |
|---|---|
| Aurora cluster | Unchanged. A second database `fieldsight_test` is created inside it. |
| prod `fieldsight` DB | **Nothing.** Never moved, never touched — zero prod risk. |
| test lambdas `PGDATABASE` | `fieldsight` → `fieldsight_test`, via a new `PgDatabase` template param passed only in `deploy.yml`'s `--parameter-overrides` (see §A). |
| Cognito | Shared `q88pd6XXr`. The `users` table copied by `pg_dump` means testers resolve in the test DB with the same login. |
| S3 buckets | Already separate — no change. |
| Master secret / password | Same cluster = same secret. No new credential; the rotation trap surface is unchanged. |

## Components

### A. `PGDATABASE` override parameter (`src/template.yaml` + `deploy.yml`)

Exactly **12** in-VPC functions set `PGDATABASE: !ImportValue "${DbStackName}-DbName"`
(= `fieldsight`), all byte-identical (`src/template.yaml:800, 833, 977, 1070,
1214, 1282, 1416, 1450, 1483, 1545, 1573, 1603` = Migrate / OrgApi / OrgSeed /
Ingest / ItemWriter / SuggestionWriter / RagSearch / VoiceAudit / VoiceReaper /
WsConnect / WsDisconnect / VoiceResolve). Verified these are the only DB clients:
the same 12 lambda entries import `db.connection`, and no function uses a
`DATABASE_URL` / `DB_SECRET_ARN` env path. **`EmbedReportFunction` and the
matcher are non-VPC and do NOT connect to the DB** — they are not in scope.

Change:

- Add a parameter `PgDatabase` (default `""`).
- Add condition `HasPgDatabaseOverride: !Not [!Equals [!Ref PgDatabase, ""]]`.
- Replace each of the 12 `PGDATABASE` values with
  `!If [HasPgDatabaseOverride, !Ref PgDatabase, !ImportValue "${DbStackName}-DbName"]`.

**CRITICAL — delivery channel (Fable B-1):** the value MUST be added to the
**`--parameter-overrides` list in `.github/workflows/deploy.yml`**
(`deploy.yml:72-81`, append `"PgDatabase=fieldsight_test"`), NOT to
`samconfig.toml`. The CI runs `sam deploy --config-env test --parameter-overrides
"Stage=test" ...`, and CLI `--parameter-overrides` **replaces** (does not merge)
the samconfig parameter list (`samconfig.toml:4-6` documents this; its test list
is already stale). Putting `PgDatabase` only in samconfig would leave the param
at default `""` → the `!If` falls back to the imported `fieldsight` → **test
silently keeps writing prod's database while everyone believes it is isolated.**
`deploy-prod.yml` gets nothing (prod keeps the default `fieldsight`). samconfig
may be updated too for doc parity, but the workflow is the only effective channel.

**Durability (binding):** the override lives in the template + workflow, never a
manual `update-function-configuration`, or the next test SAM deploy reverts test
to the shared DB.

`src/db/connection.py:32` has a `DB_NAME` default `"fieldsight"` fallback used
only if a function ever connects via `DB_SECRET_ARN` (none do today); any future
such function would NOT be covered by `PgDatabase` and must be handled then.

### B. Bootstrap the test database (one-time) — MUST run before A

**Ordering (Fable M-2, hard prerequisite):** the database must exist and be
populated **before** the `PgDatabase=fieldsight_test` override is deployed. If A
ships first, all 12 in-VPC functions fail with `FATAL: database "fieldsight_test"
does not exist`, and `deploy.yml:93-106` runs migrate post-deploy and `exit 1`s
on the error — the test pipeline stays red until the DB is created. The
implementation plan encodes B-before-A as a task dependency.

Aurora is VPC-private and dump/restore needs the PostgreSQL **16** client
(cluster is Aurora PostgreSQL **16.4**, `infra/db-template.yaml:168`; a client
below 16 refuses with a server-version mismatch). Two shortcuts are ruled out:

- `CREATE DATABASE fieldsight_test TEMPLATE fieldsight` — pure-SQL clone, but
  Postgres requires **zero active connections** to the template DB; live prod
  lambdas hold connections, so this needs downtime. Rejected for the copy.
- `pg_dump`/`psql` from a Lambda — no client binary in the runtime.

**The two `CREATE DATABASE` / (rollback) `DROP DATABASE` DDL statements need NO
VPC host** — the cluster has the Data API enabled (`EnableHttpEndpoint: true`,
`infra/db-template.yaml:179`; `ClusterArn` exported `:214`), so run them from the
operator machine via `aws rds-data execute-statement` (verify Data API's
transaction handling for `CREATE DATABASE`; fall back to `psql` if it wraps it in
a txn, which `CREATE DATABASE` forbids).

**The dump/restore body runs in a one-off ECS/Fargate task (Fable M-1 — a NEW
task definition, not the existing downloader):**

- The existing `FargateDownloaderTask` (`src/template.yaml:2013-2043`,
  `python:3.11-slim` + S3 script) and its `FargateSecurityGroup`
  (`:2001-2008`, egress-only) and `FargateTaskRole` (`:1968-1996`, S3-only) are
  **not reusable**: Aurora's `DbSG` allows 5432 only from the exported
  `fieldsight-db-test-LambdaSG` (`infra/db-template.yaml:78-83, 232-236`), and
  the copy needs `secretsmanager:GetSecretValue`.
- Create a **new one-off task def**: a `postgres:16` (or newer) image, attached to
  the **`fieldsight-db-test-LambdaSG`** security group, running in the **DB VPC**
  subnets (`vpc-0791974a474386d1c` per `samconfig.toml:91`; the `FARGATE_VPC_ID`
  secret must be confirmed to equal this VPC before use), with a task role
  granting `secretsmanager:GetSecretValue` on the DB secret. Only the ECS cluster
  and the run-one-off-task pattern are reused.
- Command:
  ```
  # credentials via PGHOST/PGPASSWORD env (NOT a URI DSN — the RDS-managed
  # password contains URI-reserved chars; see src/db/connection.py:36-40)
  pg_dump  "$fieldsight"     -Fc -f /tmp/dump.pgc          # or | psql
  pg_restore --dbname="$fieldsight_test" --no-owner /tmp/dump.pgc   # ON_ERROR_STOP semantics
  ```
  Use `-Fc` + `pg_restore` (or `psql -v ON_ERROR_STOP=1`) so a partial failure
  does not pass silently.
- `pg_dump` of live `fieldsight` is a consistent transactional snapshot — no prod
  downtime. Same cluster = same master secret (read from Secrets Manager); no new
  credential. `pg_restore` recreates the `report_chunks` HNSW index
  (`src/migrations/0004_report_chunks.sql:15-16`) and emits `CREATE EXTENSION`
  for pgvector (master can install) — no manual step.
- Result: `fieldsight_test` = full snapshot of `fieldsight` (schema, rows, the
  `users` table so testers keep their logins, and the `schema_migrations` state).
- **Repeatable**: to refresh test later, `DROP DATABASE fieldsight_test`
  (Data API) + re-run the task (destructive to test only).

### C. Migration workflow (the payoff)

Migration **state** is per-database: `schema_migrations` lives in the DB
(`src/db/migrate.py:14-20`) and is copied by `pg_dump`, so `fieldsight_test`
starts "all applied" and the two databases advance independently thereafter.

The precise claim (Fable M-3 — do NOT overstate "freely"):

- Test may **experiment** with destructive migrations (drop/rename/retype)
  against `fieldsight_test` with zero effect on prod. The "additive-only
  discipline" no longer constrains *experimentation*.
- **BUT `deploy-prod.yml:107-116` auto-runs `src/migrations/` from `main` on the
  prod DB after every prod deploy.** So any migration file **merged to `main`**
  still executes on `fieldsight`. Isolation protects prod from *test deploys*,
  not from *merged migration files*. Rule: **experimental / throwaway migrations
  must never be merged to `main`; a migration that reaches `main` is a
  deliberate, reviewed prod change.**
- Each stack's migrate lambda (`lambda_migrate.py`) connects to its own
  `PGDATABASE`: test → `fieldsight_test`, prod → `fieldsight`. No ongoing sync
  (that is the isolation).

**Documentation to correct on cutover (prevents a future session from "fixing"
the split back):** `deploy-prod.yml:6-8` ("Shares Aurora … with fieldsight-test")
and `:107` ("shared schema_migrations"), `CLAUDE.md` BUG-38, and the
`fieldsight-test-prod-org-api-topology` memory all describe the shared-DB state
and become misleading once test moves to `fieldsight_test`.

### D. Rollback

- Remove `PgDatabase=fieldsight_test` from `deploy.yml`'s `--parameter-overrides`
  (per B-1, the workflow — not samconfig — is the effective channel) and redeploy
  the test stack → the `!If` falls back to the imported `fieldsight` → test
  instantly returns to the shared database.
- `DROP DATABASE fieldsight_test` (via Data API) once the separation is confirmed
  unwanted.

## Risks / caveats

1. **Privacy** — real customer data is copied into `fieldsight_test` (accepted).
   PII anonymization during the dump is a future option, not v1.
2. **Shared-cluster blast radius** — B does NOT isolate cluster-level events
   (capacity/latency contention, maintenance windows, a cluster-wide `DROP`).
   Low risk at current scale; option C (separate cluster) is the upgrade path.
3. **Session/auth** — the cutover runs real AWS operations; the operator must be
   authenticated (`aws` session) before running bootstrap or deploys.
4. **Legacy shim** — the frozen legacy `fieldsight-*` (non-stage-prefixed)
   lambdas that serve the dev site are out of scope; they use
   DynamoDB/user_mapping, not this Aurora database. Only the SAM `fieldsight-test-*`
   stack repoints.

## Testing / verification

- **Boundary proof (both directions):** after cutover, perform a write via
  `fieldsight-test-org-api` and confirm the row appears in `fieldsight_test` but
  NOT in `fieldsight`; perform a write via `fieldsight-prod-org-api` and confirm
  the reverse. This proves the isolation holds both ways.
- **Login smoke:** a tester (shared Cognito) logs into the test app and resolves
  a caller identity from `fieldsight_test` (the copied `users` row).
- **Migration independence:** apply a trivial non-additive migration to
  `fieldsight_test` and confirm `fieldsight` is unaffected.
- **Deploy-durability:** trigger a test SAM deploy and re-confirm test functions
  still point at `fieldsight_test` (param survived the deploy).

## Out of scope

- Separate Aurora cluster for test (option C) — future upgrade if cluster-level
  isolation is needed.
- Separate Cognito pool for test — only if auth-flow testing or invite pollution
  demands it.
- PII anonymization of the test copy.
- Any change to prod's `fieldsight` database, the S3 buckets, or the shared
  master secret.
