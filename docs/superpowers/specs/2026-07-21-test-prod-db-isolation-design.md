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
   the pool (mitigation: use `+test`/fake-domain emails when invite-testing).
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
| test lambdas `PGDATABASE` | `fieldsight` → `fieldsight_test`, driven by a new template param set only for the test stage. |
| Cognito | Shared `q88pd6XXr`. The `users` table copied by `pg_dump` means testers resolve in the test DB with the same login. |
| S3 buckets | Already separate — no change. |
| Master secret / password | Same cluster = same secret. No new credential; the rotation trap surface is unchanged. |

## Components

### A. `PGDATABASE` override parameter (`src/template.yaml` + `samconfig.toml`)

Today every in-VPC function sets `PGDATABASE: !ImportValue "${DbStackName}-DbName"`
(= `fieldsight`) uniformly. Add:

- A parameter `PgDatabase` (default `""`).
- A condition `HasPgDatabaseOverride: !Not [!Equals [!Ref PgDatabase, ""]]`.
- Replace each `PGDATABASE` value with
  `!If [HasPgDatabaseOverride, !Ref PgDatabase, !ImportValue "${DbStackName}-DbName"]`.
- In `samconfig.toml` `[test.deploy.parameters]`, add
  `PgDatabase=fieldsight_test`; leave it unset for `[prod.deploy.parameters]`
  (prod keeps the imported `fieldsight`).

One parameter repoints **every** test in-VPC function uniformly (org-api,
rag-search, ingest, embed-report, migrate, org-seed, item-writer/matcher, voice
audit, etc.), because they all read the same env source.

**Durability (binding):** this override MUST live in the template + samconfig, not
a manual `update-function-configuration`, or the next test SAM deploy silently
reverts test to the shared `fieldsight` DB (same class as this session's
hot-patch reverts).

### B. Bootstrap the test database (one-time, in-VPC)

Aurora is VPC-private and the copy needs the PostgreSQL client binaries, which
the Lambda runtime does NOT ship. Two shortcuts are ruled out:

- `CREATE DATABASE fieldsight_test TEMPLATE fieldsight` — a pure-SQL clone, but
  Postgres requires **zero active connections** to the template database. The
  live prod lambdas hold connections, so this cannot run without downtime.
  Rejected.
- Running `pg_dump`/`psql` from a Lambda — no `pg_dump` binary in the runtime;
  bundling one is possible but heavier than the alternative below.

**Chosen mechanism: an ephemeral Fargate task in the VPC** (the repo already runs
Fargate — `fargate_downloader.py`, `FARGATE_SUBNET_IDS`/`FARGATE_VPC_ID`
secrets, `deploy-prod.yml`), using a `postgres`-client image, running:

```
psql "$CLUSTER/postgres" -c 'CREATE DATABASE fieldsight_test;'
pg_dump "$CLUSTER/fieldsight" | psql "$CLUSTER/fieldsight_test"
```

- `pg_dump` of the live `fieldsight` takes a consistent transactional snapshot —
  no prod downtime.
- Same master credentials for both databases (same cluster), so no new secret;
  the task reads the existing DB secret from Secrets Manager.
- Result: `fieldsight_test` is a full snapshot of `fieldsight` at copy time —
  schema, rows, and the `users` table (so testers keep their logins).
- Documented as **repeatable**: to refresh test from prod later, drop/recreate
  `fieldsight_test` and re-run the task (destructive to test only).

An acceptable alternative if Fargate proves fiddly: a temporary EC2 (or an
existing bastion) in the DB subnets with `postgresql-client` installed, running
the same three commands, then terminated. The plan picks one during
implementation; the design requires only "a pg-client host inside the VPC".

### C. Migration workflow (the payoff)

Once separated, migrations are **independent**:

- The **"additive-only" constraint is retired** — test may drop/rename/retype
  freely.
- Each stack's migrate lambda (`lambda_migrate.py`) connects to its own
  `PGDATABASE`: test → `fieldsight_test`, prod → `fieldsight`.
- No ongoing sync between the two databases (that is the isolation). Divergence
  is expected and fine.

### D. Rollback

- Point `[test.deploy.parameters]` `PgDatabase` back to empty (or `fieldsight`)
  and redeploy the test stack → test instantly returns to the shared database.
- `DROP DATABASE fieldsight_test` once the separation is confirmed unwanted.

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
