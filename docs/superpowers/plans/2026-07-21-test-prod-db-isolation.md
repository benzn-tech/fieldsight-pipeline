# Test/Prod DB Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **PHASE GATE:** Phase 1 (Tasks 1–3) is git-only, behavior-neutral, and subagent-safe. Phase 2 (Tasks 4–9) performs LIVE AWS operations on the shared prod cluster and MUST be run by the human operator with an authenticated AWS session — subagents MUST NOT execute Phase 2.

**Goal:** Move the `fieldsight-test-*` stack onto its own `fieldsight_test` database inside the existing Aurora cluster, leaving prod's `fieldsight` database untouched, so test can iterate (including destructive migrations) with zero risk to prod data.

**Architecture:** Add a `PgDatabase` CloudFormation parameter that overrides the imported DB name via an `!If`; pass `PgDatabase=fieldsight_test` only through `deploy.yml`'s `--parameter-overrides`. Bootstrap the new database with a one-time `pg_dump | pg_restore` from an in-VPC pg-16 client; create/drop the database itself via the cluster's Data API. Cognito stays shared.

**Tech Stack:** AWS SAM/CloudFormation (`src/template.yaml`), GitHub Actions (`.github/workflows/deploy.yml`), Aurora PostgreSQL 16.4 + Data API, ECS/Fargate (or temporary EC2) with `postgresql-16-client`, psycopg (`src/db/connection.py`).

## Global Constraints

- Account `509194952652`, region `ap-southeast-2`. This is the user's own SAM pipeline — NOT company CDK prod `164088480050`. Never conflate.
- Cluster: `fieldsight-db-test-dbcluster-hywiixu8ihi9`, Aurora **PostgreSQL 16.4**. Prod DB = `fieldsight`; new test DB = `fieldsight_test`.
- **Delivery channel (Blocker B-1):** `PgDatabase` takes effect ONLY via `.github/workflows/deploy.yml`'s `--parameter-overrides`. CLI `--parameter-overrides` REPLACES (not merges) `samconfig.toml`; a value placed only in samconfig is silently ignored → test keeps writing prod's DB.
- **Hard order (M-2):** the `fieldsight_test` database must exist and be populated (Phase 2 Task 5–6) BEFORE the switch is deployed (Task 7). Reversing this makes all 12 in-VPC functions `FATAL: database "fieldsight_test" does not exist` and reds the test pipeline.
- pg client must be **v16+** (below 16 refuses on server-version mismatch).
- Prod `fieldsight` database, the S3 buckets, and the shared master secret are NEVER modified.
- Experimental / throwaway migrations must NEVER be merged to `main` (deploy-prod.yml auto-runs `main`'s migrations on prod).
- Invite-testing on test uses `+test`/fake-domain emails only (a new-email invite creates a real shared-pool user and emails a real invite).
- The 12 in-VPC DB functions (the ONLY `PGDATABASE` consumers): Migrate, OrgApi, OrgSeed, Ingest, ItemWriter, SuggestionWriter, RagSearch, VoiceAudit, VoiceReaper, WsConnect, WsDisconnect, VoiceResolve (`src/template.yaml:800, 833, 977, 1070, 1214, 1282, 1416, 1450, 1483, 1545, 1573, 1603`). EmbedReport and matcher are non-VPC and are NOT in scope.

---

## Phase 1 — Git-side (subagent-safe, behavior-neutral)

Landing Phase 1 changes nothing at runtime: with no `PgDatabase` override passed, the `!If` falls back to the imported `fieldsight`, byte-identical to today.

### Task 1: Add the `PgDatabase` override parameter to the template

**Files:**
- Modify: `src/template.yaml` (Parameters block; Conditions block; the 12 `PGDATABASE` lines at `:800, 833, 977, 1070, 1214, 1282, 1416, 1450, 1483, 1545, 1573, 1603`)
- Test: `tests/unit/test_template_pgdatabase.py` (create)

**Interfaces:**
- Produces: CloudFormation parameter `PgDatabase` (default `""`), condition `HasPgDatabaseOverride`, and 12 `PGDATABASE: !If [HasPgDatabaseOverride, !Ref PgDatabase, !ImportValue ...]` values. Task 7 (Phase 2) consumes `PgDatabase` via `deploy.yml`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_template_pgdatabase.py
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "src" / "template.yaml"


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def test_pgdatabase_param_and_condition_declared():
    t = _text()
    assert re.search(r"^\s{2}PgDatabase:\s*$", t, re.M), "PgDatabase parameter missing"
    assert "HasPgDatabaseOverride:" in t, "HasPgDatabaseOverride condition missing"
    assert '!Not [!Equals [!Ref PgDatabase, ""]]' in t or \
           "!Not [!Equals [!Ref PgDatabase, '']]" in t, "condition body wrong"


def test_all_pgdatabase_values_are_guarded_by_the_condition():
    t = _text()
    # Every PGDATABASE must now be an !If over the override; none may remain a
    # bare !ImportValue (that would be an un-switched function).
    guarded = len(re.findall(r"PGDATABASE:\s*!If \[HasPgDatabaseOverride", t))
    bare = len(re.findall(r"PGDATABASE:\s*!ImportValue", t))
    assert guarded == 12, f"expected 12 guarded PGDATABASE, found {guarded}"
    assert bare == 0, f"found {bare} un-switched bare PGDATABASE !ImportValue"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_template_pgdatabase.py -v`
Expected: FAIL — `PgDatabase parameter missing` (param not added yet).

- [ ] **Step 3: Add the parameter and condition**

In `src/template.yaml`, under `Parameters:` (near `OrgUserPoolId` at `:246`), add:

```yaml
  PgDatabase:
    Type: String
    Default: ""
    Description: >
      Overrides the Aurora database name for THIS stack's in-VPC functions.
      Empty (default) => use the imported DbStackName-DbName (fieldsight),
      i.e. today's shared-DB behavior. Set to fieldsight_test (via deploy.yml
      --parameter-overrides, NOT samconfig) to isolate the test stack.
```

Under `Conditions:` (near `HasOrgPool` at `:331`), add:

```yaml
  HasPgDatabaseOverride: !Not [!Equals [!Ref PgDatabase, ""]]
```

- [ ] **Step 4: Replace all 12 `PGDATABASE` values**

At each of the 12 lines (`:800, 833, 977, 1070, 1214, 1282, 1416, 1450, 1483, 1545, 1573, 1603`), the current shape is:

```yaml
          PGDATABASE: !ImportValue
            Fn::Sub: "${DbStackName}-DbName"
```

Replace each with:

```yaml
          PGDATABASE: !If
            - HasPgDatabaseOverride
            - !Ref PgDatabase
            - !ImportValue
              Fn::Sub: "${DbStackName}-DbName"
```

(Work bottom-up — highest line number first — so earlier edits don't shift later line numbers.)

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_template_pgdatabase.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add src/template.yaml tests/unit/test_template_pgdatabase.py
git commit -m "feat(prod-isolation): PgDatabase param overrides in-VPC PGDATABASE (default = shared fieldsight, no behavior change)"
```

### Task 2: Commit the bootstrap + cutover runbook as a repo artifact

**Files:**
- Create: `scripts/db-isolation-bootstrap.md`

**Interfaces:**
- Produces: the exact operator procedure Phase 2 follows. No code consumes it; it is the runbook of record.

- [ ] **Step 1: Write the runbook**

Create `scripts/db-isolation-bootstrap.md` with the verbatim Phase-2 procedure below (pre-flight, Data API `CREATE DATABASE`, the pg-16 copy task, the switch, verification, rollback). Copy the commands from Tasks 4–8 of this plan into it so the operator has a single self-contained page. Include the explicit warning that this touches the shared prod cluster and must run with an authenticated session.

- [ ] **Step 2: Verify it renders and is self-contained**

Run: `grep -c "aws rds-data execute-statement\|pg_dump\|pg_restore\|parameter-overrides" scripts/db-isolation-bootstrap.md`
Expected: ≥ 4 (all key commands present).

- [ ] **Step 3: Commit**

```bash
git add scripts/db-isolation-bootstrap.md
git commit -m "docs(prod-isolation): operator runbook for fieldsight_test bootstrap + cutover"
```

### Task 3: Stage the documentation corrections (do NOT flip wording yet)

**Files:**
- Modify: `CLAUDE.md` (BUG-38 / test-prod topology note)
- Modify: `.github/workflows/deploy-prod.yml:6-8, 107` (header + "shared schema_migrations" comment)

**Interfaces:**
- Produces: doc text that becomes accurate only after cutover. Land it in the SAME PR/branch as the Task 7 switch so docs and reality flip together (a reviewer gate, per M-3).

- [ ] **Step 1: Add a forward-looking note (not a reversal) to CLAUDE.md**

Append to the BUG-38 / topology section a sentence: "As of the db-isolation cutover (plan `docs/superpowers/plans/2026-07-21-test-prod-db-isolation.md`), the test stack targets a SEPARATE database `fieldsight_test` on the same cluster via the `PgDatabase` param; prod stays on `fieldsight`. Do NOT 'fix' this apparent split — it is intentional."

- [ ] **Step 2: Annotate the deploy-prod.yml comments**

At `deploy-prod.yml:6-8` and `:107`, add a trailing note: "# NOTE: as of db-isolation cutover, ONLY prod uses `fieldsight`; test uses `fieldsight_test`. schema_migrations is per-database."

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md .github/workflows/deploy-prod.yml
git commit -m "docs(prod-isolation): note the fieldsight_test split so it isn't reverted"
```

---

## Phase 2 — Live cutover (OPERATOR ONLY — authenticated AWS session required)

> Subagents STOP here. Every step below runs real AWS commands against the shared prod cluster. Run them yourself, in order, verifying each before the next. Re-authenticate first (`! aws sso login` / your usual flow) — the session from planning has expired.

### Task 4: Pre-flight checks

- [ ] **Step 1: Confirm identity + region**

Run: `aws sts get-caller-identity` → expect account `509194952652`. `export AWS_DEFAULT_REGION=ap-southeast-2`.

- [ ] **Step 2: Confirm the Fargate VPC == the DB VPC**

Run: `echo "$FARGATE_VPC_ID"` (or read the GitHub secret) and compare to `vpc-0791974a474386d1c` (`samconfig.toml:91`). If they differ, the copy task must run in the DB VPC's subnets, NOT `FARGATE_SUBNET_IDS`. Record the correct subnet + the exported SG `fieldsight-db-test-LambdaSG`.

- [ ] **Step 3: Confirm pg client version**

On the chosen copy host (Fargate image `postgres:16` or EC2 with `postgresql-16-client`): `pg_dump --version` → expect `16.x`.

- [ ] **Step 4: Confirm PGPASSWORD-MATCH (rotation-trap guard)**

Run the same check `deploy-prod.yml:45-54` uses: the DB secret's `password` must equal a live lambda's `PGPASSWORD`. If mismatched, resync before proceeding (memory: `fieldsight-db-password-rotation-trap`).

### Task 5: Create the `fieldsight_test` database (Data API — no VPC host)

- [ ] **Step 1: Get the cluster ARN + secret ARN**

Run:
```bash
CLUSTER_ARN=$(aws cloudformation list-exports --query "Exports[?Name=='fieldsight-db-test-ClusterArn'].Value" --output text)
SECRET_ARN=$(aws cloudformation list-exports --query "Exports[?Name=='fieldsight-db-test-SecretArn'].Value" --output text)
```

- [ ] **Step 2: Create the database**

`CREATE DATABASE` cannot run inside a transaction; Data API auto-commits single statements (no explicit transaction id), so:
```bash
aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
  --database postgres --sql 'CREATE DATABASE fieldsight_test;'
```
Expected: `{"numberOfRecordsUpdated": 0}` (no error). If it errors with "cannot run inside a transaction block", fall back to `psql` from the copy host (Task 6) for this one statement.

- [ ] **Step 3: Verify it exists**

```bash
aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
  --database postgres --sql "SELECT datname FROM pg_database WHERE datname='fieldsight_test';"
```
Expected: one row `fieldsight_test`.

### Task 6: Copy `fieldsight` → `fieldsight_test` (in-VPC pg-16 client)

- [ ] **Step 1: Launch the copy host**

Register a one-off ECS/Fargate task (image `postgres:16`; SG `fieldsight-db-test-LambdaSG`; DB VPC subnets; task role with `secretsmanager:GetSecretValue` on `$SECRET_ARN`), OR a temporary EC2 in the DB subnets with `postgresql-16-client`. Export on that host:
```bash
export PGHOST=fieldsight-db-test-dbcluster-hywiixu8ihi9.cluster-ctugu28wme3y.ap-southeast-2.rds.amazonaws.com
export PGUSER=postgres
export PGPASSWORD='<from Secrets Manager — do NOT build a URI DSN; reserved chars, see src/db/connection.py:36-40>'
```

- [ ] **Step 2: Dump prod (consistent snapshot, no downtime) + restore into test**

```bash
pg_dump -d fieldsight -Fc -f /tmp/fieldsight.pgc
pg_restore --dbname=fieldsight_test --no-owner --exit-on-error /tmp/fieldsight.pgc
```
`--exit-on-error` (pg_restore) / `ON_ERROR_STOP=1` semantics so a partial failure is loud. pgvector `CREATE EXTENSION` + the `report_chunks` HNSW index rebuild happen automatically.

- [ ] **Step 3: Verify the copy — row counts match on key tables**

For `users`, `topics`, `action_items`, `report_chunks`, `sites`, compare counts between the two databases (via Data API against each `--database`):
```bash
for t in users topics action_items report_chunks sites; do
  for db in fieldsight fieldsight_test; do
    aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
      --database "$db" --sql "SELECT count(*) FROM $t;" --query 'records[0][0].longValue' --output text
  done
done
```
Expected: each table's two counts equal. Tear down the copy host afterward.

### Task 7: Deploy the switch (test only)

- [ ] **Step 1: Add the override to the workflow**

In `.github/workflows/deploy.yml`, append to the `--parameter-overrides` list (`:72-81`):
```yaml
              "PgDatabase=fieldsight_test" \
```
(Do NOT touch `deploy-prod.yml` — prod keeps the default.)

- [ ] **Step 2: Merge Phase-1 template + this workflow change + the Task-3 doc flips to `develop`**

Push to `develop` → `deploy.yml` runs a full SAM deploy of `fieldsight-test`. The post-deploy migrate now targets `fieldsight_test` (already fully migrated by the copy) → no-op, green.

- [ ] **Step 3: Verify the test functions repointed**

```bash
for fn in fieldsight-test-org-api fieldsight-test-rag-search fieldsight-test-ingest; do
  aws lambda get-function-configuration --function-name "$fn" \
    --query "Environment.Variables.PGDATABASE" --output text
done
```
Expected: all print `fieldsight_test`.

### Task 8: Boundary verification (both directions)

- [ ] **Step 1: Write via test, confirm it lands in test-DB only**

Do a harmless write through `fieldsight-test-org-api` (e.g. `PATCH /api/org/sites/{id}` with a name change on a test-company site). Then query BOTH databases (Data API) for that row's new value:
- `fieldsight_test`: shows the new value.
- `fieldsight`: shows the OLD value (unchanged).

- [ ] **Step 2: Write via prod, confirm the reverse**

Do the same harmless write through `fieldsight-prod-org-api` on a prod row; confirm it appears in `fieldsight` but NOT in `fieldsight_test`. This proves isolation holds both ways.

- [ ] **Step 3: Login smoke**

A tester (shared Cognito) logs into the test app; confirm `GET /api/org/me` resolves from `fieldsight_test` (the copied `users` row) — non-empty profile, no "caller not provisioned".

### Task 9: Finalize docs + memory

- [ ] **Step 1: Flip any remaining shared-DB wording**

Confirm the Task-3 notes are now accurate (test on `fieldsight_test`). Update the `fieldsight-test-prod-org-api-topology` memory to state test now runs a SEPARATE database.

- [ ] **Step 2: Mark the spec + plan done**

Set the spec status to "Implemented" and check off this plan's boxes.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "docs(prod-isolation): mark fieldsight_test cutover complete"
```

---

## Self-Review

- **Spec coverage:** §A→Task 1 (+ Task 7 delivery); §B→Tasks 5–6 (Data API DDL + pg-16 copy, ordered before switch); §C→Task 3 + Task 9 (migration-doc corrections); §D→Task 7 rollback note in runbook (Task 2); caveats (privacy, blast radius, +test emails, durability)→Global Constraints + runbook. Verification section→Task 8. All covered.
- **Ordering:** Global Constraints + the Phase gate + Task 6-before-7 encode B-before-A.
- **Placeholders:** none — every code/command step shows exact content; the only `<...>` is the secret value the operator supplies at runtime (Task 6), which is correct not to hardcode.
- **Type consistency:** `PgDatabase` / `HasPgDatabaseOverride` / `fieldsight_test` used identically across Tasks 1, 7, 8.
