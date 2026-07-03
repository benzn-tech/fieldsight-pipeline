# Phase 2B — Aurora Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Stand up the real PostgreSQL for the Phase 2A data layer: Aurora Serverless v2 (pgvector-capable PG16) in a dedicated CloudFormation stack, wire `lambda_migrate` into the TEST app stack, run migrations, and prove the 2A schema live.

**Verified account facts (2026-07-04):** only the DEFAULT VPC exists (`vpc-0791974a474386d1c`, 172.31.0.0/16, 3 public subnets 2a/2b/2c, NO NAT gateway). TEST stack (`fieldsight-test`) deploys green with 9 functions incl. `fieldsight-test-api`. 2A code (migrations 0001-0004, `db/`, `repositories/`, `lambda_migrate.py`) is merged on develop, CI-tested against pgvector/pg16.

## Design decisions (rationale recorded)

- **Separate stack `fieldsight-db-test`** (new template `infra/db-template.yaml` + samconfig env `[db-test]`): DB lifecycle must not share blast radius with app-stack deploys. App stack references cluster endpoint/secret via CloudFormation exports.
- **Default VPC placement**: Aurora `PubliclyAccessible=false` + a dedicated SG (`fieldsight-db-sg`, ingress 5432 ONLY from `fieldsight-lambda-sg`). "Public" subnets are irrelevant when the instance has no public IP and SG is closed; building private subnets + NAT (~$35/mo) buys nothing at this stage. Revisit at prod cutover.
- **NO RDS Proxy yet**: minimum proxy cost > value at current concurrency (a handful of Lambdas). Add when Phase 3/4 raise connection pressure.
- **Data API ENABLED on the cluster**: coexists with psycopg; gives (a) the user console Query Editor troubleshooting (break-glass, per the no-platform-admin decision), (b) CLI verification without VPC access. psycopg remains the app path per 2A.
- **Scale-to-zero**: ServerlessV2 `MinCapacity: 0`, `MaxCapacity: 2`, engine `aurora-postgresql` 16.x (min-0 requires 16.3+). SecondsUntilAutoPause default.
- **Credentials**: `ManageMasterUserPassword: true` (RDS-managed secret). `lambda_migrate` gets `DATABASE_URL` assembled at deploy time via dynamic references — no plaintext in template/repo.
- **Migrate Lambda VPC needs**: it only talks to the DB (no internet), so default-VPC subnets + SG suffice; no NAT/endpoints needed. It bundles `psycopg[binary]` via a requirements layer or function-level `Metadata: BuildProperties` — NOTE: `src/requirements.txt` currently lacks psycopg; the migrate function must get psycopg WITHOUT bloating the other 9 functions (use a dedicated function dir or a small Lambda layer — Task 2 decides after reading how sam build treats per-function requirements with shared CodeUri; if shared CodeUri forces one requirements.txt for all, split lambda_migrate into `src_db/` with its own requirements.txt).

## Tasks

### Task 1 — `infra/db-template.yaml` + samconfig `[db-test]`
CloudFormation (plain, no SAM transform needed): `AWS::RDS::DBCluster` (aurora-postgresql, EngineMode provisioned + ServerlessV2ScalingConfiguration {MinCapacity:0, MaxCapacity:2}, EnableHttpEndpoint:true, ManageMasterUserPassword:true, DBSubnetGroup over the 3 default subnets, VpcSecurityGroupIds:[DbSG]), one `AWS::RDS::DBInstance` (db.serverless), `AWS::EC2::SecurityGroup` DbSG (ingress 5432 from LambdaSG), `AWS::EC2::SecurityGroup` LambdaSG (no ingress; egress all). Outputs+Exports: `FieldsightDbTest-ClusterEndpoint`, `-ClusterArn`, `-SecretArn`, `-DbSG`, `-LambdaSG`, `-DbName`. Params: Stage (test), DBName (fieldsight). samconfig `[db-test]` env → stack `fieldsight-db-test`, region ap-southeast-2, CAPABILITY_IAM. cfn-lint clean.

### Task 2 — wire `lambda_migrate` into `src/template.yaml` (test-safe)
New `MigrateFunction` (`${Prefix}-migrate`): handler `lambda_migrate.lambda_handler`, VpcConfig {SubnetIds param DbSubnetIds, SecurityGroupIds [ImportValue LambdaSG]}, env `DATABASE_URL` assembled from `{{resolve:secretsmanager:<SecretArn>:SecretString:password}}` + endpoint import (format `postgresql://postgres:<pw>@<endpoint>:5432/fieldsight`), timeout 120s, NO events (manual invoke). Gate behind new Condition `HasDb` (param `DbStackName` default '' — empty → function skipped, so prod deploys unaffected). Solve the psycopg packaging question (see design note). deploy.yml test job passes `DbStackName=fieldsight-db-test` + subnet ids (new secrets/vars). cfn-lint + sam validate clean; PR to develop; CI deploy green.

### Task 3 — provision + migrate + verify (controller, AWS)
`sam deploy --config-env db-test` (creates cluster; ~10 min). Then redeploy app stack (picks up MigrateFunction), `aws lambda invoke fieldsight-test-migrate` → expect `{"applied":["0001_...","0002_...","0003_...","0004_..."]}`. Verify via Data API: `aws rds-data execute-statement --sql "select version(); select extname from pg_extension"` shows pgvector; `select count(*) from information_schema.tables where table_schema='public'` = 10 (9 tables + schema_migrations). Re-invoke migrate → `{"applied":[]}` (idempotent). Record endpoints/ARNs in ledger + DEPLOYMENT-RUNBOOK.

### Task 4 — docs + roadmap tick
RUNBOOK: db-test stack section (deploy/rotate/pause facts, Query Editor how-to for user break-glass). Roadmap: mark Phase 2 供应 done for test. Ledger.

**Rollback**: `aws cloudformation delete-stack fieldsight-db-test` removes everything (no data yet). Cost when idle ≈ storage only (<$1/mo at empty scale, min ACU 0).
