# Phase 3: Org Write API (OrgApiFunction) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real write backend for projects/members/roles/profile/images: a new in-VPC `OrgApiFunction` on the TEST stack serving `/api/org/*` against Aurora, plus VPC endpoints, a seed Lambda backfilling Cognito users + user_mapping sites into Aurora.

**Architecture:** One in-VPC Lambda (psycopg direct to Aurora `fieldsight-db-test`, deploy-time PG* credential injection per BUG-36) routed via the existing TEST `FieldSightApi` at `/api/org/{proxy+}` with the Cognito authorizer extended to ALSO accept the prod user pool `ap-southeast-2_q88pd6XXr` (UI tokens). Cognito admin-create goes through a new cognito-idp VPC interface endpoint; S3 reads (seed) through a free S3 gateway endpoint; presigning is offline. Never touches the hand-built prod gateway `khfj3p1fkb`.

**Tech Stack:** SAM (src/template.yaml canonical), plain CFN (infra/db-template.yaml), Python 3.11 Lambda, psycopg3 (no ORM), pytest (unit local, integration via pgvector container in CI).

**Companion plan (NOT here):** UI wiring (`FS_ORG_BASEURL`, `FS_ORGWRITES` gate, team/settings/sites pages) is a separate plan in the fieldsight-ui repo, written AFTER this backend is live (needs the real gateway URL + verified contract).

**Deviation from handoff §5 (justified):** adds `GET /api/org/members` to the endpoint set — the UI team page and the admin fan-out replacement (handoff §8) both need a member list; role PATCH is unusable without one.

## Global Constraints

- Account: user's own **509194952652** (ap-southeast-2) ONLY. Company account 164088480050 is untouchable.
- Prod resources (API GW `khfj3p1fkb`, `fieldsight-*` Lambdas, CloudFront) must NOT be touched; all deploys go to the `fieldsight-test` stack via CI (develop branch) — never `sam deploy` against prod.
- Repo is on Windows with `autocrlf=true` and mixed line endings: use single-line Edit anchors; NEVER `git add -A` or `git add .` — always add explicit file paths. Untracked local files (`assets/`, `benchmark/`, `claw-code/`, `scripts/aws-*.sh`, `src/requirements.txt`, user's roadmap notes) must never be staged or deleted.
- Lambda runtime = python3.11; CI Python = 3.11 (must match: PsycopgLayer BuildMethod). Local dev python is 3.14 with psycopg installed — unit + handler tests run locally; integration tests need `TEST_DATABASE_URL` (skip locally, run in CI's pgvector container).
- Repositories are psycopg-native, no ORM. **Repositories never commit** — the caller owns the transaction (`with get_connection() as conn:` commits on clean exit, closes on block exit).
- ACL is deny-by-default. Roles vocabulary: global `admin|gm|pm|site_manager|worker`; scope `admin/gm` = company-level (from Phase 2A decisions). Server-side role validation on every member write (anti-privilege-escalation).
- `{{resolve:secretsmanager:...}}` only composes with Parameters, never ImportValue (BUG-36); cfn-lint E1051 fires even on the literal pattern in comments — never write the resolve literal in YAML comments.
- Commits: conventional style (`feat(3):`, `fix(3):`, `docs:`), frequent, one per green TDD cycle.
- Run tests with `python -m pytest tests/unit -v` (integration auto-skips without TEST_DATABASE_URL).
- cfn-lint / `sam validate --lint` must stay clean on both templates.

---

### Task 1: VPC endpoints in the DB stack (+ lint coverage)

**Files:**
- Modify: `infra/db-template.yaml` (Parameters block after `SubnetIds`; Resources block after `DbSG`)
- Modify: `.github/workflows/ci.yml:25` (lint line)

**Interfaces:**
- Produces: cognito-idp interface endpoint + S3 gateway endpoint in the default VPC, reachable by any Lambda attached to the exported `fieldsight-db-test-LambdaSG`. No new exports (endpoints are ambient network infra; nothing imports them).
- Consumed by: Task 9's OrgApiFunction (cognito-idp calls) and Task 10's seed function (S3 + cognito-idp calls) at runtime.

- [ ] **Step 1: Add RouteTableIds parameter**

In `infra/db-template.yaml`, after the `SubnetIds` parameter block (line 32 ends `never hardcode account-specific subnet ids in this template.`), insert:

```yaml
  RouteTableIds:
    Type: CommaDelimitedList
    Description: >
      Route table ids for the S3 gateway endpoint (the default VPC's main
      route table). Gateway endpoints attach to route tables, not subnets.
      Pass via parameter-overrides — never hardcode.
```

- [ ] **Step 2: Add endpoint resources**

After the `DbSG` resource (line 71 `Value: !Sub fieldsight-db-sg-${Stage}`), insert:

```yaml
  # ----------------------------------------------------------
  # VPC Endpoints (Phase 3)
  # In-VPC Lambdas have no NAT: any AWS API call without an endpoint
  # black-holes until timeout with zero logs (BUG-36). OrgApiFunction
  # needs cognito-idp (admin-create/list users); the seed function also
  # reads S3 config. Interface endpoint ~$8/month; gateway endpoint free.
  # ----------------------------------------------------------
  # Endpoint-side SG: ingress 443 ONLY from LambdaSG.
  EndpointSG:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: !Sub fieldsight-endpoint-sg (${Stage}) - ingress 443 from LambdaSG only
      VpcId: !Ref VpcId
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 443
          ToPort: 443
          SourceSecurityGroupId: !GetAtt LambdaSG.GroupId
          Description: HTTPS from FieldSight in-VPC Lambdas (interface endpoints)
      Tags:
        - Key: Name
          Value: !Sub fieldsight-endpoint-sg-${Stage}

  CognitoIdpEndpoint:
    Type: AWS::EC2::VPCEndpoint
    Properties:
      VpcEndpointType: Interface
      ServiceName: !Sub com.amazonaws.${AWS::Region}.cognito-idp
      VpcId: !Ref VpcId
      SubnetIds: !Ref SubnetIds
      SecurityGroupIds:
        - !GetAtt EndpointSG.GroupId
      PrivateDnsEnabled: true

  S3GatewayEndpoint:
    Type: AWS::EC2::VPCEndpoint
    Properties:
      VpcEndpointType: Gateway
      ServiceName: !Sub com.amazonaws.${AWS::Region}.s3
      VpcId: !Ref VpcId
      RouteTableIds: !Ref RouteTableIds
```

- [ ] **Step 3: Extend CI lint to the db template**

In `.github/workflows/ci.yml`, replace the line `        run: cfn-lint src/template.yaml` with:

```yaml
        run: cfn-lint src/template.yaml infra/db-template.yaml
```

- [ ] **Step 4: Lint locally**

Run: `cfn-lint infra/db-template.yaml` (pip-install cfn-lint first if absent: `python -m pip install cfn-lint`)
Expected: exit 0, no findings.

- [ ] **Step 5: Commit**

```bash
git add infra/db-template.yaml .github/workflows/ci.yml
git commit -m "feat(3): cognito-idp interface + S3 gateway VPC endpoints in db stack"
```

- [ ] **Step 6: Deploy the db stack (manual — needs a live AWS session)**

If credentials are expired, ask the user to run `! aws login` (or their SSO command). Then (Git Bash):

```bash
export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1   # BUG-35
VPC_ID=$(aws ec2 describe-subnets --subnet-ids subnet-082dd4480f7e20014 \
  --query 'Subnets[0].VpcId' --output text --region ap-southeast-2)
RT_ID=$(aws ec2 describe-route-tables \
  --filters Name=vpc-id,Values=$VPC_ID Name=association.main,Values=true \
  --query 'RouteTables[0].RouteTableId' --output text --region ap-southeast-2)
aws cloudformation deploy \
  --stack-name fieldsight-db-test \
  --template-file infra/db-template.yaml \
  --region ap-southeast-2 \
  --parameter-overrides \
    Stage=test \
    "VpcId=$VPC_ID" \
    "SubnetIds=subnet-082dd4480f7e20014,subnet-08b15b36113e542d4,subnet-05fb05613cf529121" \
    "EndpointSubnetIds=subnet-082dd4480f7e20014" \
    "RouteTableIds=$RT_ID"
```

Note: `--parameter-overrides` on an existing stack must re-supply ALL previous parameters (VpcId/SubnetIds were set in Phase 2B; the values above are the same ones from deploy.yml). If the CLI errors on a missing previous parameter, add `DBName=fieldsight`.

- [ ] **Step 7: Verify endpoints exist**

Run:
```bash
aws ec2 describe-vpc-endpoints \
  --filters Name=vpc-id,Values=$VPC_ID \
  --query 'VpcEndpoints[].{Svc:ServiceName,State:State,Type:VpcEndpointType}' \
  --output table --region ap-southeast-2
```
Expected: one `cognito-idp` Interface endpoint (State `available` — may take ~2 min) and one `s3` Gateway endpoint `available`.

---

### Task 2: users repository — list / explicit role set / profile update

**Files:**
- Modify: `src/repositories/users.py`
- Test: `tests/integration/test_core_repositories.py` (append new test functions)

**Interfaces:**
- Consumes: existing `users.upsert_user(conn, cognito_sub, email, company_id=None, first_name=None, last_name=None, global_role=None) -> dict`, `users.get_user_by_sub(conn, cognito_sub) -> dict | None`; `companies.create_company(conn, name, industry=None) -> dict`.
- Produces (used by Tasks 4/6/10):
  - `users.list_company_users(conn, company_id) -> list[dict]` (rows with the `_COLS` fields, ordered by created_at)
  - `users.set_global_role(conn, cognito_sub, company_id, global_role) -> dict | None` (explicit SET — no COALESCE; returns None when no row matches sub AND company; company guard prevents cross-tenant role writes)
  - `users.update_profile(conn, cognito_sub, first_name=None, last_name=None, avatar_s3_key=None) -> dict | None` (None = leave unchanged; returns None if user unknown)

- [ ] **Step 1: Write failing integration tests**

Append to `tests/integration/test_core_repositories.py` (it already imports `pytest`, marks `integration`, and uses the rolled-back `db` fixture; match its existing import style — add `list_company_users, set_global_role, update_profile` to the existing `from repositories.users import ...` line or add a new import line):

```python
@pytest.mark.integration
def test_list_company_users_scoped_to_company(db):
    c1 = create_company(db, "ListCo A")
    c2 = create_company(db, "ListCo B")
    u1 = upsert_user(db, "sub-lc-1", "a@x.nz", company_id=c1["id"])
    upsert_user(db, "sub-lc-2", "b@x.nz", company_id=c2["id"])
    rows = list_company_users(db, c1["id"])
    subs = [r["cognito_sub"] for r in rows]
    assert "sub-lc-1" in subs and "sub-lc-2" not in subs


@pytest.mark.integration
def test_set_global_role_explicit_and_company_guarded(db):
    c1 = create_company(db, "RoleCo A")
    c2 = create_company(db, "RoleCo B")
    upsert_user(db, "sub-rl-1", "r@x.nz", company_id=c1["id"], global_role="worker")
    # explicit set works within the company
    row = set_global_role(db, "sub-rl-1", c1["id"], "pm")
    assert row["global_role"] == "pm"
    # cross-company set returns None and does not change the row
    assert set_global_role(db, "sub-rl-1", c2["id"], "admin") is None
    assert get_user_by_sub(db, "sub-rl-1")["global_role"] == "pm"


@pytest.mark.integration
def test_update_profile_none_preserving(db):
    c1 = create_company(db, "ProfCo")
    upsert_user(db, "sub-pf-1", "p@x.nz", company_id=c1["id"],
                first_name="Old", last_name="Name")
    row = update_profile(db, "sub-pf-1", first_name="New",
                         avatar_s3_key="org-assets/avatars/sub-pf-1/a.jpg")
    assert row["first_name"] == "New"
    assert row["last_name"] == "Name"  # None = unchanged
    assert row["avatar_s3_key"] == "org-assets/avatars/sub-pf-1/a.jpg"
    assert update_profile(db, "sub-does-not-exist", first_name="X") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/integration/test_core_repositories.py -v -k "list_company_users or set_global_role or update_profile"`
Expected locally: SKIPPED (no TEST_DATABASE_URL) — then verify collection errors are absent with `python -m pytest tests/ --collect-only -q` (ImportError for the new names = the expected failure signal locally). In CI these run for real.

- [ ] **Step 3: Implement**

Append to `src/repositories/users.py`:

```python
def list_company_users(conn, company_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE company_id=%s ORDER BY created_at",
        (company_id,),
    ).fetchall()


def set_global_role(conn, cognito_sub, company_id, global_role) -> dict | None:
    """Explicit role SET (admin action). Company-guarded: refuses to touch a
    row outside the caller's company (cross-tenant write = returns None)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET global_role=%s "
        f"WHERE cognito_sub=%s AND company_id=%s RETURNING {_COLS}",
        (global_role, cognito_sub, company_id),
    ).fetchone()


def update_profile(conn, cognito_sub, first_name=None, last_name=None,
                   avatar_s3_key=None) -> dict | None:
    """Self-service profile update. None = leave unchanged (same semantics
    as upsert_user). Role/company are NOT touchable here by design."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET "
        f"  first_name=COALESCE(%(first)s, first_name), "
        f"  last_name=COALESCE(%(last)s, last_name), "
        f"  avatar_s3_key=COALESCE(%(avatar)s, avatar_s3_key) "
        f"WHERE cognito_sub=%(sub)s RETURNING {_COLS}",
        {"sub": cognito_sub, "first": first_name, "last": last_name,
         "avatar": avatar_s3_key},
    ).fetchone()
```

- [ ] **Step 4: Verify collection + unit suite green**

Run: `python -m pytest tests/ --collect-only -q` → no errors. Run: `python -m pytest tests/unit -v` → all PASS (unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/repositories/users.py tests/integration/test_core_repositories.py
git commit -m "feat(3): users repo — list_company_users, set_global_role (company-guarded), update_profile"
```

---

### Task 3: memberships / sites / companies repository additions

**Files:**
- Modify: `src/repositories/memberships.py`
- Modify: `src/repositories/sites.py`
- Modify: `src/repositories/companies.py`
- Test: `tests/integration/test_memberships_acl.py` (append)

**Interfaces:**
- Consumes: existing `add_membership(conn, user_id, site_id, role)`, `create_site(conn, company_id, name, ...)`, `create_company(conn, name)`, `upsert_user(...)`; schema `memberships UNIQUE (user_id, site_id)`.
- Produces (used by Tasks 4/5/6/7/10):
  - `memberships.ensure_membership(conn, user_id, site_id, role) -> dict` (idempotent upsert on the UNIQUE pair; re-run updates role)
  - `memberships.list_company_memberships(conn, company_id) -> list[dict]` (each row: `user_id, cognito_sub, site_id, role`)
  - `sites.list_sites_by_ids(conn, site_ids) -> list[dict]` (empty list for empty input — no SQL with empty ANY)
  - `sites.get_company_site_by_name(conn, company_id, name) -> dict | None`
  - `companies.get_company_by_name(conn, name) -> dict | None`

- [ ] **Step 1: Write failing integration tests**

Append to `tests/integration/test_memberships_acl.py` (match its existing imports; add the new names):

```python
@pytest.mark.integration
def test_ensure_membership_idempotent_role_update(db):
    c = create_company(db, "EnsureCo")
    u = upsert_user(db, "sub-en-1", "e@x.nz", company_id=c["id"])
    s = create_site(db, c["id"], "Ensure Site")
    m1 = ensure_membership(db, u["id"], s["id"], "worker")
    m2 = ensure_membership(db, u["id"], s["id"], "site_manager")  # re-run: no raise
    assert m1["id"] == m2["id"]
    assert m2["role"] == "site_manager"


@pytest.mark.integration
def test_list_company_memberships_scoped(db):
    c1 = create_company(db, "MemCo A")
    c2 = create_company(db, "MemCo B")
    u1 = upsert_user(db, "sub-me-1", "m1@x.nz", company_id=c1["id"])
    u2 = upsert_user(db, "sub-me-2", "m2@x.nz", company_id=c2["id"])
    s1 = create_site(db, c1["id"], "Mem Site A")
    s2 = create_site(db, c2["id"], "Mem Site B")
    ensure_membership(db, u1["id"], s1["id"], "worker")
    ensure_membership(db, u2["id"], s2["id"], "worker")
    rows = list_company_memberships(db, c1["id"])
    assert [r["cognito_sub"] for r in rows] == ["sub-me-1"]
    assert rows[0]["site_id"] == s1["id"]


@pytest.mark.integration
def test_sites_by_ids_and_by_name(db):
    c = create_company(db, "SiteLookupCo")
    s1 = create_site(db, c["id"], "Lookup One")
    create_site(db, c["id"], "Lookup Two")
    assert sites_list_by_ids(db, []) == []
    got = sites_list_by_ids(db, [s1["id"]])
    assert [g["name"] for g in got] == ["Lookup One"]
    assert get_company_site_by_name(db, c["id"], "Lookup Two")["name"] == "Lookup Two"
    assert get_company_site_by_name(db, c["id"], "Nope") is None


@pytest.mark.integration
def test_get_company_by_name(db):
    create_company(db, "FindMe Ltd")
    assert get_company_by_name(db, "FindMe Ltd")["name"] == "FindMe Ltd"
    assert get_company_by_name(db, "Ghost Co") is None
```

(Import as `from repositories.sites import list_sites_by_ids as sites_list_by_ids, get_company_site_by_name` plus `from repositories.companies import create_company, get_company_by_name` and `from repositories.memberships import ensure_membership, list_company_memberships` — merge into the file's existing import block.)

- [ ] **Step 2: Verify collection failure locally**

Run: `python -m pytest tests/ --collect-only -q`
Expected: ImportError naming the missing functions.

- [ ] **Step 3: Implement**

Append to `src/repositories/memberships.py` (and extend its `__all__` list with `"ensure_membership", "list_company_memberships"`):

```python
def ensure_membership(conn, user_id, site_id, role) -> dict:
    """Idempotent add: re-running updates the role instead of raising on
    the (user_id, site_id) UNIQUE constraint. Used by seed + member create."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO memberships (user_id, site_id, role) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, site_id) DO UPDATE SET role=EXCLUDED.role "
        "RETURNING id, user_id, site_id, role, created_at",
        (user_id, site_id, role),
    ).fetchone()


def list_company_memberships(conn, company_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT m.user_id, u.cognito_sub, m.site_id, m.role "
        "FROM memberships m "
        "JOIN users u ON u.id = m.user_id "
        "JOIN sites s ON s.id = m.site_id "
        "WHERE s.company_id = %s "
        "ORDER BY u.created_at, m.created_at",
        (company_id,),
    ).fetchall()
```

Append to `src/repositories/sites.py`:

```python
def list_sites_by_ids(conn, site_ids) -> list[dict]:
    if not site_ids:
        return []  # ANY('{}') is valid SQL but skip the round-trip
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE id = ANY(%s) ORDER BY created_at",
        (list(site_ids),),
    ).fetchall()


def get_company_site_by_name(conn, company_id, name) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE company_id=%s AND name=%s",
        (company_id, name),
    ).fetchone()
```

Append to `src/repositories/companies.py`:

```python
def get_company_by_name(conn, name) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, name, industry, created_at FROM companies WHERE name=%s",
        (name,),
    ).fetchone()
```

- [ ] **Step 4: Verify collection + unit suite green**

Run: `python -m pytest tests/ --collect-only -q` then `python -m pytest tests/unit -v`
Expected: no collection errors; unit suite PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/memberships.py src/repositories/sites.py src/repositories/companies.py tests/integration/test_memberships_acl.py
git commit -m "feat(3): repo additions — ensure_membership, company member/site lookups"
```

---

### Task 4: lambda_org_api skeleton — identity, router, GET/PATCH /me

**Files:**
- Create: `src/lambda_org_api.py`
- Create: `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `db.connection.get_connection`, `repositories.users.get_user_by_sub / update_profile`, `repositories.memberships.accessible_site_ids`.
- Produces (later tasks add routes to this file):
  - `lambda_handler(event, context) -> dict` (API GW proxy response)
  - helpers `ok(body, status=200)`, `error(message, status=400)`, `parse_body(event) -> dict | None`
  - `dispatch(conn, event, method, route)` — route strings are the path AFTER the `/api/org` prefix (e.g. `/me`, `/sites`)
  - module constants `ALLOWED_GLOBAL_ROLES = {"admin", "gm", "pm", "site_manager", "worker"}`, `ALLOWED_MEMBERSHIP_ROLES = {"pm", "site_manager", "worker"}`, `ORG_ASSETS_PREFIX`, `S3_BUCKET`, `COGNITO_USER_POOL_ID`, `PRESIGNED_URL_EXPIRY = 900`
  - lazy client accessors `s3()` / `cognito()` (module-level `_s3_client` / `_cognito_client` for test injection)
  - caller dict = the users-table row (`id, cognito_sub, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at`)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_lambda_org_api.py`:

```python
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


def make_event(method, path, sub="sub-1", body=None, params=None):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": params,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}},
    }


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-04",
}


@pytest.fixture
def wired(monkeypatch):
    """Wire a FakeConn and a default admin caller; tests override as needed."""
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    return monkeypatch


def body_of(res):
    return json.loads(res["body"])


def test_unknown_caller_403(wired):
    res = org.lambda_handler(make_event("GET", "/api/org/me", sub="sub-ghost"), None)
    assert res["statusCode"] == 403


def test_caller_without_company_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "company_id": None})
    res = org.lambda_handler(make_event("GET", "/api/org/me"), None)
    assert res["statusCode"] == 403


def test_get_me_returns_profile_and_sites(wired):
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda conn, uid, role: ["s-uuid-1", "s-uuid-2"])
    res = org.lambda_handler(make_event("GET", "/api/org/me"), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["cognito_sub"] == "sub-1"
    assert b["global_role"] == "admin"
    assert b["site_ids"] == ["s-uuid-1", "s-uuid-2"]
    assert res["headers"]["Access-Control-Allow-Origin"] == "*"


def test_patch_me_updates_profile_fields_only(wired):
    seen = {}

    def fake_update(conn, sub, first_name=None, last_name=None, avatar_s3_key=None):
        seen.update(sub=sub, first=first_name, last=last_name, avatar=avatar_s3_key)
        return {**CALLER, "first_name": first_name or CALLER["first_name"]}

    wired.setattr(org.users, "update_profile", fake_update)
    wired.setattr(org.memberships, "accessible_site_ids", lambda *a: [])
    res = org.lambda_handler(make_event("PATCH", "/api/org/me", body={
        "first_name": "Grace", "global_role": "admin"}), None)
    assert res["statusCode"] == 200
    assert seen["first"] == "Grace"
    assert seen["avatar"] is None  # role key ignored, not smuggled anywhere


def test_patch_me_rejects_foreign_avatar_key(wired):
    res = org.lambda_handler(make_event("PATCH", "/api/org/me", body={
        "avatar_s3_key": "reports/2026-03-02/evil.json"}), None)
    assert res["statusCode"] == 400


def test_unknown_route_404(wired):
    res = org.lambda_handler(make_event("GET", "/api/org/nope"), None)
    assert res["statusCode"] == 404


def test_malformed_json_400(wired):
    ev = make_event("PATCH", "/api/org/me")
    ev["body"] = "{not json"
    res = org.lambda_handler(ev, None)
    assert res["statusCode"] == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v`
Expected: FAIL/ERROR — `ModuleNotFoundError: lambda_org_api` (or importorskip skip if psycopg missing; locally psycopg exists so it must FAIL).

- [ ] **Step 3: Implement the skeleton**

Create `src/lambda_org_api.py`:

```python
"""
Lambda: fieldsight-org-api v1.0 — Org write backend (Phase 3)

In-VPC (psycopg direct to Aurora). Routed at /api/org/{proxy+} on the TEST
FieldSightApi with a Cognito authorizer that also trusts the prod user pool,
so the UI's raw idToken works unchanged.

Routes (this file grows by task; see docs/superpowers/plans/2026-07-04-phase-3-org-api.md):
  GET   /api/org/me                       → caller profile + accessible site ids
  PATCH /api/org/me                       → update first/last name, avatar key
  GET   /api/org/sites                    → sites visible to caller (ACL)
  POST  /api/org/sites                    → create site (admin/gm)
  GET   /api/org/members                  → company members + memberships (admin/gm)
  POST  /api/org/members                  → cognito admin-create + upsert + memberships (admin)
  PATCH /api/org/members/{sub}/role       → explicit global role set (admin)
  POST  /api/org/upload-url               → presigned PUT for avatar / site icon
  GET   /api/org/asset-url?key=…          → presigned GET for an org asset

Credentials: PG* env vars injected at deploy time (BUG-36 — no runtime
Secrets Manager call from a NAT-less VPC). Cognito calls need the
cognito-idp VPC interface endpoint (db stack).
"""
import json
import logging
import os
import re
import uuid

import boto3

from db.connection import get_connection
from repositories import memberships, sites, users
from repositories.acl import resolve_scope

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
ORG_ASSETS_PREFIX = os.environ.get("ORG_ASSETS_PREFIX", "org-assets/")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
PRESIGNED_URL_EXPIRY = 900

ALLOWED_GLOBAL_ROLES = {"admin", "gm", "pm", "site_manager", "worker"}
ALLOWED_MEMBERSHIP_ROLES = {"pm", "site_manager", "worker"}

_s3_client = None
_cognito_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def cognito():
    global _cognito_client
    if _cognito_client is None:
        _cognito_client = boto3.client("cognito-idp")
    return _cognito_client


def ok(body, status=200):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def error(message, status=400):
    return ok({"error": message}, status)


def parse_body(event):
    """Return the parsed JSON body dict, or None on malformed JSON."""
    raw = event.get("body") or "{}"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def lambda_handler(event, context):
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    m = re.match(r"^/api/org(/.*)?$", path)
    route = (m.group(1) or "/") if m else path
    try:
        # psycopg3 `with conn:` commits on clean exit, rolls back on
        # exception, and closes the connection when the block ends.
        with get_connection() as conn:
            return dispatch(conn, event, method, route)
    except Exception:
        logger.exception("org api unhandled error")
        return error("internal error", 500)


def dispatch(conn, event, method, route):
    claims = (event.get("requestContext", {}) or {}).get("authorizer", {}).get("claims", {})
    sub = claims.get("sub", "")
    caller = users.get_user_by_sub(conn, sub) if sub else None
    if caller is None:
        return error("caller not provisioned in org database (run seed?)", 403)
    if not caller["company_id"]:
        return error("caller has no company", 403)

    if route == "/me":
        if method == "GET":
            return get_me(conn, caller)
        if method == "PATCH":
            return patch_me(conn, caller, parse_body(event))

    return error("not found", 404)


# ----------------------------------------------------------
# /me
# ----------------------------------------------------------
def get_me(conn, caller):
    site_ids = memberships.accessible_site_ids(
        conn, caller["id"], caller["global_role"])
    return ok({**caller, "site_ids": site_ids,
               "scope": resolve_scope(caller["global_role"])})


def patch_me(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    avatar = body.get("avatar_s3_key")
    if avatar is not None and not str(avatar).startswith(ORG_ASSETS_PREFIX):
        return error(f"avatar_s3_key must start with {ORG_ASSETS_PREFIX}", 400)
    row = users.update_profile(
        conn, caller["cognito_sub"],
        first_name=body.get("first_name"),
        last_name=body.get("last_name"),
        avatar_s3_key=avatar,
    )
    if row is None:
        return error("user not found", 404)
    return ok(row)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v`
Expected: all 7 PASS. Then `python -m pytest tests/unit -v` → whole unit suite PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3): org api skeleton — identity resolution, router, GET/PATCH /me"
```

---

### Task 5: /sites — list (ACL) and create (admin/gm)

**Files:**
- Modify: `src/lambda_org_api.py`
- Test: `tests/unit/test_lambda_org_api.py` (append)

**Interfaces:**
- Consumes: Task 3's `sites.list_company_sites / list_sites_by_ids / create_site`, `memberships.accessible_site_ids`, `resolve_scope`.
- Produces: `GET /api/org/sites` → `{"sites": [...]}`; `POST /api/org/sites` body `{name, location?, client?, industry?, icon_s3_key?}` → 201 site row. admin/gm only for POST; icon key must be under `org-assets/`.

- [ ] **Step 1: Write failing tests** (append to `tests/unit/test_lambda_org_api.py`)

```python
def test_list_sites_admin_gets_company_sites(wired):
    wired.setattr(org.sites, "list_company_sites",
                  lambda conn, cid: [{"id": "s-1", "name": "Alpha"}])
    res = org.lambda_handler(make_event("GET", "/api/org/sites"), None)
    assert res["statusCode"] == 200
    assert body_of(res)["sites"] == [{"id": "s-1", "name": "Alpha"}]


def test_list_sites_worker_gets_membership_sites(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda conn, uid, role: ["s-2"])
    wired.setattr(org.sites, "list_sites_by_ids",
                  lambda conn, ids: [{"id": i, "name": "Beta"} for i in ids])
    res = org.lambda_handler(make_event("GET", "/api/org/sites"), None)
    assert body_of(res)["sites"] == [{"id": "s-2", "name": "Beta"}]


def test_create_site_admin_ok(wired):
    created = {}

    def fake_create(conn, company_id, name, location=None, client=None,
                    industry=None, icon_s3_key=None):
        created.update(company_id=company_id, name=name, location=location)
        return {"id": "s-new", "company_id": company_id, "name": name}

    wired.setattr(org.sites, "create_site", fake_create)
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "New Site", "location": "Chch"}), None)
    assert res["statusCode"] == 201
    assert created == {"company_id": "c-uuid-1", "name": "New Site", "location": "Chch"}


def test_create_site_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event("POST", "/api/org/sites",
                                        body={"name": "X"}), None)
    assert res["statusCode"] == 403


def test_create_site_requires_name(wired):
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={}), None)
    assert res["statusCode"] == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k sites`
Expected: FAIL — 404 responses (routes missing).

- [ ] **Step 3: Implement**

In `src/lambda_org_api.py` `dispatch()`, after the `/me` block, insert:

```python
    if route == "/sites":
        if method == "GET":
            return list_org_sites(conn, caller)
        if method == "POST":
            return create_org_site(conn, caller, parse_body(event))
```

After `patch_me`, append:

```python
# ----------------------------------------------------------
# /sites
# ----------------------------------------------------------
def list_org_sites(conn, caller):
    if resolve_scope(caller["global_role"]) == "ALL":
        rows = sites.list_company_sites(conn, caller["company_id"])
    else:
        ids = memberships.accessible_site_ids(
            conn, caller["id"], caller["global_role"])
        rows = sites.list_sites_by_ids(conn, ids)
    return ok({"sites": rows})


def create_org_site(conn, caller, body):
    if caller["global_role"] not in ("admin", "gm"):
        return error("admin or gm role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    name = (body.get("name") or "").strip()
    if not name:
        return error("name is required", 400)
    icon = body.get("icon_s3_key")
    if icon is not None and not str(icon).startswith(ORG_ASSETS_PREFIX):
        return error(f"icon_s3_key must start with {ORG_ASSETS_PREFIX}", 400)
    row = sites.create_site(
        conn, caller["company_id"], name,
        location=body.get("location"), client=body.get("client"),
        industry=body.get("industry"), icon_s3_key=icon,
    )
    return ok(row, 201)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3): org api /sites — ACL-scoped list, admin/gm create"
```

---

### Task 6: /members list + /members/{sub}/role PATCH

**Files:**
- Modify: `src/lambda_org_api.py`
- Test: `tests/unit/test_lambda_org_api.py` (append)

**Interfaces:**
- Consumes: `users.list_company_users`, `users.set_global_role`, `memberships.list_company_memberships`.
- Produces: `GET /api/org/members` (admin/gm) → `{"members": [user-row + "memberships": [{site_id, role}]]}`; `PATCH /api/org/members/{sub}/role` (admin) body `{"global_role": "..."}` → updated row. Role whitelist enforced server-side; company guard via `set_global_role`'s WHERE.

- [ ] **Step 1: Write failing tests** (append)

```python
def test_list_members_joins_memberships(wired):
    wired.setattr(org.users, "list_company_users", lambda conn, cid: [
        {"id": "u-1", "cognito_sub": "sub-1", "email": "a@x.nz"},
        {"id": "u-2", "cognito_sub": "sub-2", "email": "b@x.nz"},
    ])
    wired.setattr(org.memberships, "list_company_memberships", lambda conn, cid: [
        {"user_id": "u-1", "cognito_sub": "sub-1", "site_id": "s-1", "role": "worker"},
    ])
    res = org.lambda_handler(make_event("GET", "/api/org/members"), None)
    assert res["statusCode"] == 200
    members = body_of(res)["members"]
    assert members[0]["memberships"] == [{"site_id": "s-1", "role": "worker"}]
    assert members[1]["memberships"] == []


def test_list_members_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event("GET", "/api/org/members"), None)
    assert res["statusCode"] == 403


def test_patch_role_admin_ok(wired):
    seen = {}

    def fake_set(conn, sub, company_id, role):
        seen.update(sub=sub, company_id=company_id, role=role)
        return {**CALLER, "cognito_sub": sub, "global_role": role}

    wired.setattr(org.users, "set_global_role", fake_set)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": "pm"}), None)
    assert res["statusCode"] == 200
    assert seen == {"sub": "sub-2", "company_id": "c-uuid-1", "role": "pm"}


def test_patch_role_rejects_unknown_role(wired):
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": "root"}), None)
    assert res["statusCode"] == 400


def test_patch_role_gm_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "gm"})
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": "pm"}), None)
    assert res["statusCode"] == 403


def test_patch_role_unknown_target_404(wired):
    wired.setattr(org.users, "set_global_role", lambda *a: None)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-ghost/role", body={"global_role": "pm"}), None)
    assert res["statusCode"] == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k "members or role"`
Expected: FAIL with 404s.

- [ ] **Step 3: Implement**

In `dispatch()`, after the `/sites` block, insert:

```python
    if route == "/members":
        if method == "GET":
            return list_members(conn, caller)
        if method == "POST":
            return create_member(conn, caller, parse_body(event))
    m = re.match(r"^/members/([^/]+)/role$", route)
    if m and method == "PATCH":
        return patch_member_role(conn, caller, m.group(1), parse_body(event))
```

(`create_member` arrives in Task 7 — add a stub now so the dispatch block is final:)

```python
def create_member(conn, caller, body):
    return error("not implemented", 501)
```

Append the handlers:

```python
# ----------------------------------------------------------
# /members
# ----------------------------------------------------------
def list_members(conn, caller):
    if resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    rows = users.list_company_users(conn, caller["company_id"])
    per_user = {}
    for mem in memberships.list_company_memberships(conn, caller["company_id"]):
        per_user.setdefault(mem["user_id"], []).append(
            {"site_id": mem["site_id"], "role": mem["role"]})
    for row in rows:
        row["memberships"] = per_user.get(row["id"], [])
    return ok({"members": rows})


def patch_member_role(conn, caller, target_sub, body):
    if caller["global_role"] != "admin":
        return error("admin role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    role = body.get("global_role")
    if role not in ALLOWED_GLOBAL_ROLES:
        return error(f"global_role must be one of {sorted(ALLOWED_GLOBAL_ROLES)}", 400)
    row = users.set_global_role(conn, target_sub, caller["company_id"], role)
    if row is None:
        return error("member not found in your company", 404)
    return ok(row)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v`
Expected: all PASS (the 501 stub is never asserted on).

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3): org api /members list + explicit role PATCH (admin, company-guarded)"
```

---

### Task 7: POST /members — Cognito admin-create + upsert + memberships

**Files:**
- Modify: `src/lambda_org_api.py` (replace the Task 6 stub)
- Test: `tests/unit/test_lambda_org_api.py` (append)

**Interfaces:**
- Consumes: `cognito()` client (`admin_create_user`, `admin_get_user`), `users.upsert_user`, `memberships.ensure_membership`, `sites.get_site`.
- Produces: `POST /api/org/members` (admin only) body `{email, first_name?, last_name?, global_role? (default worker), memberships?: [{site_id, role}]}` → 201 `{user, memberships}`. Idempotent on re-run (existing Cognito user → `admin_get_user` for sub; `upsert_user` + `ensure_membership` are upserts). Cognito sends the standard email invite with a temporary password.

- [ ] **Step 1: Write failing tests** (append)

```python
class FakeCognito:
    def __init__(self, exists=False):
        self.exists = exists
        self.created = []

    def admin_create_user(self, **kw):
        if self.exists:
            raise self.exceptions.UsernameExistsException(
                {"Error": {"Code": "UsernameExistsException", "Message": "exists"}},
                "AdminCreateUser")
        self.created.append(kw)
        return {"User": {"Attributes": [
            {"Name": "sub", "Value": "sub-new"},
            {"Name": "email", "Value": kw["Username"]},
        ]}}

    def admin_get_user(self, **kw):
        return {"UserAttributes": [{"Name": "sub", "Value": "sub-existing"}]}

    class exceptions:
        class UsernameExistsException(Exception):
            def __init__(self, *a, **k):
                super().__init__("exists")


@pytest.fixture
def member_wired(wired):
    fake = FakeCognito()
    wired.setattr(org, "_cognito_client", fake)
    wired.setattr(org.users, "upsert_user",
                  lambda conn, sub, email, **kw: {
                      "id": "u-new", "cognito_sub": sub, "email": email, **kw})
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-uuid-1"})
    wired.setattr(org.memberships, "ensure_membership",
                  lambda conn, uid, sid, role: {
                      "user_id": uid, "site_id": sid, "role": role})
    return wired, fake


def test_create_member_creates_and_enrolls(member_wired):
    wired, fake = member_wired
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "new@x.nz", "first_name": "New", "global_role": "site_manager",
        "memberships": [{"site_id": "s-1", "role": "site_manager"}],
    }), None)
    assert res["statusCode"] == 201
    b = body_of(res)
    assert b["user"]["cognito_sub"] == "sub-new"
    assert b["memberships"] == [{"user_id": "u-new", "site_id": "s-1",
                                 "role": "site_manager"}]
    assert fake.created[0]["Username"] == "new@x.nz"
    assert fake.created[0]["UserPoolId"] == org.COGNITO_USER_POOL_ID


def test_create_member_existing_cognito_user_is_idempotent(member_wired):
    wired, fake = member_wired
    fake.exists = True
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "old@x.nz"}), None)
    assert res["statusCode"] == 201
    assert body_of(res)["user"]["cognito_sub"] == "sub-existing"


def test_create_member_rejects_bad_global_role(member_wired):
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "global_role": "superuser"}), None)
    assert res["statusCode"] == 400


def test_create_member_rejects_foreign_site(member_wired):
    wired, fake = member_wired
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "OTHER-company"})
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "memberships": [{"site_id": "s-9", "role": "worker"}],
    }), None)
    assert res["statusCode"] == 403


def test_create_member_rejects_bad_membership_role(member_wired):
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "memberships": [{"site_id": "s-1", "role": "admin"}],
    }), None)
    assert res["statusCode"] == 400


def test_create_member_non_admin_403(member_wired):
    wired, fake = member_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "gm"})
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz"}), None)
    assert res["statusCode"] == 403
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k create_member`
Expected: FAIL — 501 from the stub.

- [ ] **Step 3: Implement (replace the Task 6 `create_member` stub entirely)**

```python
def create_member(conn, caller, body):
    """Admin-only. Creates the Cognito login (email invite w/ temp password),
    the Aurora profile, and site memberships. Idempotent: an existing Cognito
    user is looked up instead of failing, and the DB writes are upserts —
    safe to retry after a partial failure (Cognito ok, DB rolled back)."""
    if caller["global_role"] != "admin":
        return error("admin role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return error("valid email is required", 400)
    global_role = body.get("global_role") or "worker"
    if global_role not in ALLOWED_GLOBAL_ROLES:
        return error(f"global_role must be one of {sorted(ALLOWED_GLOBAL_ROLES)}", 400)
    wanted = body.get("memberships") or []
    for mem in wanted:
        if not isinstance(mem, dict) or not mem.get("site_id"):
            return error("each membership needs a site_id", 400)
        if mem.get("role") not in ALLOWED_MEMBERSHIP_ROLES:
            return error(
                f"membership role must be one of {sorted(ALLOWED_MEMBERSHIP_ROLES)}", 400)
        site = sites.get_site(conn, mem["site_id"])
        if site is None or site["company_id"] != caller["company_id"]:
            return error("site not found in your company", 403)

    client = cognito()
    display_name = " ".join(
        p for p in (body.get("first_name"), body.get("last_name")) if p) or email
    try:
        resp = client.admin_create_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": display_name},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        attrs = resp["User"]["Attributes"]
    except client.exceptions.UsernameExistsException:
        resp = client.admin_get_user(UserPoolId=COGNITO_USER_POOL_ID, Username=email)
        attrs = resp["UserAttributes"]
    sub = next(a["Value"] for a in attrs if a["Name"] == "sub")

    user = users.upsert_user(
        conn, sub, email,
        company_id=caller["company_id"],
        first_name=body.get("first_name"),
        last_name=body.get("last_name"),
        global_role=global_role,
    )
    created = [memberships.ensure_membership(conn, user["id"], mem["site_id"],
                                             mem["role"]) for mem in wanted]
    return ok({"user": user, "memberships": created}, 201)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3): org api POST /members — cognito invite + upsert + memberships, idempotent"
```

---

### Task 8: /upload-url + /asset-url presigning

**Files:**
- Modify: `src/lambda_org_api.py`
- Test: `tests/unit/test_lambda_org_api.py` (append)

**Interfaces:**
- Consumes: `s3()` client `generate_presigned_url` (offline signing — no network needed), `sites.get_site`, `uuid`.
- Produces: `POST /api/org/upload-url` body `{kind: "avatar"|"site_icon", content_type}` → `{url, key, expires_in}`; `GET /api/org/asset-url?key=org-assets/...` → `{url, expires_in}`. Content types limited to `image/jpeg|png|webp`; keys always server-generated and OWNER-SCOPED: avatar → `org-assets/avatars/{caller_sub}/…`, site_icon → `org-assets/site-icons/{caller_sub}/…` (admin/gm only; NO site_id — icons upload BEFORE the site row exists in the UI create-modal flow, then `POST /sites` carries the returned `icon_s3_key`).
- AMENDED 2026-07-04 (review-driven): original plan required site_id for site_icon — dead-end, since no PATCH /sites exists and creation needs the icon first. This task also folds in the reviewers' Minor batch: (a) `patch_me` avatar key must be caller-scoped (`org-assets/avatars/{caller_sub}/`), (b) `create_org_site` casts name via `str()`, (c) `patch_member_role` + `create_member` role checks get `isinstance(…, str)` guards (non-string JSON values must 400, not 500).

- [ ] **Step 1: Write failing tests** (append)

```python
class FakeS3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params, "expires": ExpiresIn}
        return "https://s3.example/" + Params["Key"]


@pytest.fixture
def presign_wired(wired):
    fake = FakeS3()
    wired.setattr(org, "_s3_client", fake)
    return wired, fake


def test_upload_url_avatar(presign_wired):
    wired, fake = presign_wired
    res = org.lambda_handler(make_event("POST", "/api/org/upload-url", body={
        "kind": "avatar", "content_type": "image/png"}), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["key"].startswith("org-assets/avatars/sub-1/")
    assert b["key"].endswith(".png")
    assert fake.last["op"] == "put_object"
    assert fake.last["params"]["ContentType"] == "image/png"


def test_upload_url_site_icon_admin_gets_owner_scoped_key(presign_wired):
    wired, fake = presign_wired
    res = org.lambda_handler(make_event("POST", "/api/org/upload-url", body={
        "kind": "site_icon", "content_type": "image/webp"}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["key"].startswith("org-assets/site-icons/sub-1/")


def test_upload_url_site_icon_worker_403(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event("POST", "/api/org/upload-url", body={
        "kind": "site_icon", "content_type": "image/png"}), None)
    assert res["statusCode"] == 403


def test_upload_url_rejects_content_type(presign_wired):
    res = org.lambda_handler(make_event("POST", "/api/org/upload-url", body={
        "kind": "avatar", "content_type": "application/x-sh"}), None)
    assert res["statusCode"] == 400


def test_asset_url_prefix_guard(presign_wired):
    res = org.lambda_handler(make_event(
        "GET", "/api/org/asset-url", params={"key": "reports/2026/secret.json"}), None)
    assert res["statusCode"] == 400
    res2 = org.lambda_handler(make_event(
        "GET", "/api/org/asset-url",
        params={"key": "org-assets/avatars/sub-1/a.png"}), None)
    assert res2["statusCode"] == 200
    assert body_of(res2)["url"].endswith("a.png")


def test_patch_me_avatar_must_be_caller_scoped(wired):
    res = org.lambda_handler(make_event("PATCH", "/api/org/me", body={
        "avatar_s3_key": "org-assets/avatars/sub-OTHER/x.png"}), None)
    assert res["statusCode"] == 400


def test_non_string_inputs_get_400_not_500(wired):
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": ["admin"]}), None)
    assert res["statusCode"] == 400
    res2 = org.lambda_handler(make_event(
        "POST", "/api/org/sites", body={"name": 123}), None)
    assert res2["statusCode"] == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -v -k "upload_url or asset_url"`
Expected: FAIL with 404s.

- [ ] **Step 3: Implement**

In `dispatch()`, before the final `return error("not found", 404)`, insert:

```python
    if route == "/upload-url" and method == "POST":
        return create_upload_url(conn, caller, parse_body(event))
    if route == "/asset-url" and method == "GET":
        return get_asset_url(event)
```

Append the handlers:

```python
# ----------------------------------------------------------
# assets (presigned PUT/GET; signing is offline — no VPC egress needed)
# ----------------------------------------------------------
ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def create_upload_url(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    content_type = body.get("content_type")
    ext = ALLOWED_IMAGE_TYPES.get(content_type) if isinstance(content_type, str) else None
    if ext is None:
        return error(f"content_type must be one of {sorted(ALLOWED_IMAGE_TYPES)}", 400)
    kind = body.get("kind")
    if kind == "avatar":
        key = f"{ORG_ASSETS_PREFIX}avatars/{caller['cognito_sub']}/{uuid.uuid4().hex}.{ext}"
    elif kind == "site_icon":
        # Icons are uploaded BEFORE the site row exists (the UI create-modal
        # picks the image during creation), so keys scope by uploader sub,
        # not site id; POST /sites then stores the returned key.
        if caller["global_role"] not in ("admin", "gm"):
            return error("admin or gm role required", 403)
        key = f"{ORG_ASSETS_PREFIX}site-icons/{caller['cognito_sub']}/{uuid.uuid4().hex}.{ext}"
    else:
        return error("kind must be avatar or site_icon", 400)
    url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"url": url, "key": key, "expires_in": PRESIGNED_URL_EXPIRY})


def get_asset_url(event):
    key = (event.get("queryStringParameters") or {}).get("key", "")
    if not key.startswith(ORG_ASSETS_PREFIX):
        return error(f"key must start with {ORG_ASSETS_PREFIX}", 400)
    url = s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"url": url, "expires_in": PRESIGNED_URL_EXPIRY})
```

- [ ] **Step 3b: Reviewer-batch polish of earlier handlers (same file)**

Three surgical changes in `src/lambda_org_api.py`:

(1) In `patch_me`, replace the avatar guard lines

```python
    avatar = body.get("avatar_s3_key")
    if avatar is not None and not str(avatar).startswith(ORG_ASSETS_PREFIX):
        return error(f"avatar_s3_key must start with {ORG_ASSETS_PREFIX}", 400)
```

with an owner-scoped guard:

```python
    avatar = body.get("avatar_s3_key")
    own_prefix = f"{ORG_ASSETS_PREFIX}avatars/{caller['cognito_sub']}/"
    if avatar is not None and (
            not isinstance(avatar, str) or not avatar.startswith(own_prefix)):
        return error(f"avatar_s3_key must start with {own_prefix}", 400)
```

(2) In `create_org_site`, change `    name = (body.get("name") or "").strip()` to `    name = str(body.get("name") or "").strip()` (non-string names must 400 via the empty-check, not crash to 500).

(3) In `patch_member_role`, change `    if role not in ALLOWED_GLOBAL_ROLES:` to `    if not isinstance(role, str) or role not in ALLOWED_GLOBAL_ROLES:`. In `create_member`, change `    if global_role not in ALLOWED_GLOBAL_ROLES:` to `    if not isinstance(global_role, str) or global_role not in ALLOWED_GLOBAL_ROLES:` and change `        if mem.get("role") not in ALLOWED_MEMBERSHIP_ROLES:` to `        if not isinstance(mem.get("role"), str) or mem.get("role") not in ALLOWED_MEMBERSHIP_ROLES:` (unhashable JSON values raise TypeError → 500 without these).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(3): org api presigned upload-url / asset-url with prefix + type guards"
```

---

### Task 9: SAM wiring — OrgApiFunction, dual-pool authorizer, deploy.yml, bucket CORS

**Files:**
- Modify: `src/template.yaml` (Parameters, Conditions, FieldSightApi.Auth, new resources)
- Modify: `.github/workflows/deploy.yml` (parameter-overrides + CORS wire step)
- Create: `scripts/wire-bucket-cors.sh`

**Interfaces:**
- Consumes: Task 1's endpoints (runtime), `PsycopgLayer`, `DbStackName/DbSecretArn/DbSubnetIds` params, `${DbStackName}-ClusterEndpoint/-DbName/-LambdaSG` exports.
- Produces: `fieldsight-test-org-api` Lambda live at `POST/GET/PATCH https://<test-api-id>.execute-api.ap-southeast-2.amazonaws.com/prod/api/org/*`; authorizer accepts BOTH test-pool and prod-pool (`ap-southeast-2_q88pd6XXr`) idTokens; test data bucket CORS allows browser PUT/GET from Amplify origins. New template param `OrgUserPoolId` (default '' — prod/legacy deploys unaffected).

- [ ] **Step 1: Add the OrgUserPoolId parameter**

In `src/template.yaml`, after the `DbSubnetIds` parameter block (ends line 207 `Default: ''`), insert:

```yaml
  OrgUserPoolId:
    Type: String
    Description: >
      Existing Cognito user pool id that real app users log in with (the
      prod pool). When set together with DbStackName, deploys the org API
      and adds this pool to the FieldSightApi authorizer so the UI's raw
      idToken is accepted on /api/org/*. Leave empty to skip (prod/default
      deploys are unaffected).
    Default: ''
```

- [ ] **Step 2: Add conditions**

Replace the single line `  HasDb: !Not [!Equals [!Ref DbStackName, '']]` with:

```yaml
  HasDb: !Not [!Equals [!Ref DbStackName, '']]
  HasOrgPool: !Not [!Equals [!Ref OrgUserPoolId, '']]
  HasOrgApi: !And [!Condition HasDb, !Condition HasOrgPool]
```

- [ ] **Step 3: Extend the authorizer to both pools**

In `FieldSightApi` → `Auth` → `Authorizers`, replace:

```yaml
          CognitoAuthorizer:
            UserPoolArn: !GetAtt UserPool.Arn
```

with:

```yaml
          CognitoAuthorizer:
            # Accepts idTokens from this stack's own pool AND (test stage)
            # the prod pool the UI logs into — /api/org/* is called by the
            # deployed UI with its prod-pool token. NB: this trusts prod
            # tokens on ALL /api/* routes of the TEST gateway; test-stage
            # data only, acceptable by design (2026-07-04).
            UserPoolArn:
              - !GetAtt UserPool.Arn
              - !If
                - HasOrgPool
                - !Sub arn:aws:cognito-idp:${AWS::Region}:${AWS::AccountId}:userpool/${OrgUserPoolId}
                - !Ref AWS::NoValue
```

AMENDED 2026-07-04: original !If-wraps-list form produced nested providerARNs through the SAM transform (review-caught); literal list + AWS::NoValue element is the verified correct encoding.

- [ ] **Step 4: Add OrgApiFunction + log group**

After the `MigrateFunction` resource block (ends line 600 `        - VPCAccessPolicy: {}`), insert:

```yaml
  # ----------------------------------------------------------
  # Lambda 10: Org API (Phase 3) — real write backend for
  # projects/members/roles/profile/images. In-VPC (psycopg → Aurora).
  # Gated by HasOrgApi (DbStackName + OrgUserPoolId both set).
  # ----------------------------------------------------------
  OrgApiFunction:
    Type: AWS::Serverless::Function
    Condition: HasOrgApi
    Properties:
      FunctionName: !Sub ["${P}-org-api", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_org_api.lambda_handler
      Timeout: 30
      MemorySize: 256
      Layers:
        - !Ref PsycopgLayer
      VpcConfig:
        SubnetIds: !Ref DbSubnetIds
        SecurityGroupIds:
          - !ImportValue
            Fn::Sub: "${DbStackName}-LambdaSG"
      Environment:
        Variables:
          # Deploy-time credential injection — see MigrateFunction notes.
          PGHOST: !ImportValue
            Fn::Sub: "${DbStackName}-ClusterEndpoint"
          PGDATABASE: !ImportValue
            Fn::Sub: "${DbStackName}-DbName"
          PGUSER: postgres
          PGPASSWORD: !Sub '{{resolve:secretsmanager:${DbSecretArn}:SecretString:password}}'
          COGNITO_USER_POOL_ID: !Ref OrgUserPoolId
          S3_BUCKET: !Ref DataBucketName
          ORG_ASSETS_PREFIX: org-assets/
      Policies:
        - VPCAccessPolicy: {}
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action:
                - cognito-idp:AdminCreateUser
                - cognito-idp:AdminGetUser
              Resource: !Sub arn:aws:cognito-idp:${AWS::Region}:${AWS::AccountId}:userpool/${OrgUserPoolId}
            - Effect: Allow
              Action:
                - s3:PutObject
                - s3:GetObject
              Resource: !Sub arn:aws:s3:::${DataBucketName}/org-assets/*
      Events:
        OrgProxy:
          Type: Api
          Properties:
            RestApiId: !Ref FieldSightApi
            Path: /api/org/{proxy+}
            Method: ANY
```

After the `MigrateLogGroup` resource block, insert:

```yaml
  OrgApiLogGroup:
    Type: AWS::Logs::LogGroup
    Condition: HasOrgApi
    Properties:
      LogGroupName: !Sub /aws/lambda/${OrgApiFunction}
      RetentionInDays: 14
```

- [ ] **Step 5: Create the bucket CORS wiring script**

Create `scripts/wire-bucket-cors.sh` (mirrors wire-s3-events.sh's role: config on an out-of-stack bucket, applied idempotently from deploy.yml):

```bash
#!/usr/bin/env bash
# wire-bucket-cors.sh BUCKET [REGION]
# Browser-direct presigned PUT/GET (org-assets uploads) is cross-origin
# fetch — S3 must answer CORS. put-bucket-cors REPLACES the whole config;
# this bucket has no other CORS consumers, so a full replace is safe.
set -euo pipefail
BUCKET="${1:?usage: wire-bucket-cors.sh BUCKET [REGION]}"
REGION="${2:-ap-southeast-2}"

aws s3api put-bucket-cors --bucket "$BUCKET" --region "$REGION" \
  --cors-configuration '{
    "CORSRules": [
      {
        "AllowedOrigins": ["https://*.amplifyapp.com", "http://localhost:8765"],
        "AllowedMethods": ["PUT", "GET"],
        "AllowedHeaders": ["*"],
        "MaxAgeSeconds": 3000
      }
    ]
  }'
echo "CORS applied to s3://$BUCKET"
aws s3api get-bucket-cors --bucket "$BUCKET" --region "$REGION"
```

- [ ] **Step 6: Wire deploy.yml**

In `.github/workflows/deploy.yml`, add one parameter line after `              "DbSecretArn=$DB_SECRET_ARN"` (keep the backslash continuation on the previous line):

```yaml
              "DbSecretArn=$DB_SECRET_ARN" \
              "OrgUserPoolId=ap-southeast-2_q88pd6XXr"
```

After the `Wire S3 events (TEST bucket)` step, insert:

```yaml
      - name: Wire bucket CORS (TEST bucket, org-assets uploads)
        run: bash scripts/wire-bucket-cors.sh fieldsight-data-test-509194952652 ${{ env.AWS_REGION }}
```

NOTE: the deploy role needs `s3:PutBucketCORS` + `s3:GetBucketCORS` on `arn:aws:s3:::fieldsight-data-test-509194952652` — same pattern as the existing PutBucketNotification grant. This is an IAM change (permission-gated): produce the exact `aws iam` statement for the user to approve/run, mirroring how `fix-sam-deploy-role.sh` grants were handled, e.g.:

```bash
# Append to the deploy role's inline policy (user-approved):
aws iam get-role-policy --role-name github-actions-fieldsight-deploy --policy-name <name>  # inspect first
# then add: {"Effect":"Allow","Action":["s3:PutBucketCORS","s3:GetBucketCORS"],
#            "Resource":"arn:aws:s3:::fieldsight-data-test-509194952652"}
```

- [ ] **Step 7: Validate**

Run: `export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1 && sam validate --template-file src/template.yaml --lint --region ap-southeast-2`
Expected: `template.yaml is a valid SAM Template`, no lint findings. (If no AWS session, `cfn-lint src/template.yaml` alone is the offline equivalent.)

- [ ] **Step 8: Commit**

```bash
git add src/template.yaml .github/workflows/deploy.yml scripts/wire-bucket-cors.sh
git commit -m "feat(3): OrgApiFunction + dual-pool authorizer + bucket CORS wiring"
```

---

### Task 10: Seed Lambda — company + Cognito users + user_mapping sites → Aurora

**Files:**
- Create: `src/lambda_org_seed.py`
- Test: `tests/unit/test_lambda_org_seed.py`
- Modify: `src/template.yaml` (OrgSeedFunction + log group, after OrgApiFunction's log group)

**Interfaces:**
- Consumes: `get_connection`, `companies.get_company_by_name / create_company`, `sites.get_company_site_by_name / create_site`, `users.upsert_user`, `memberships.ensure_membership`; Cognito `list_users` (paginated); S3 `config/user_mapping.json` (shape: `{"sites": {slug: {name, location, client}}, "mapping": {device: {name, role, sites: [slug]}}}`).
- Produces: `fieldsight-test-org-seed` Lambda, manual invoke only. Event: `{"company_name"?: str, "admin_emails"?: [str]}` (defaults `"FieldSight"` / `["benl.tech@outlook.com"]`). Idempotent — re-run creates nothing new. Returns `{"company": ..., "users": n, "sites": n, "memberships": n}`.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_lambda_org_seed.py`:

```python
import pytest

seed = pytest.importorskip("lambda_org_seed", reason="requires psycopg (installed in CI)")


COGNITO_USERS = [
    {"Attributes": [{"Name": "sub", "Value": "sub-admin"},
                    {"Name": "email", "Value": "benl.tech@outlook.com"},
                    {"Name": "name", "Value": "Ben Lin"}]},
    {"Attributes": [{"Name": "sub", "Value": "sub-jt"},
                    {"Name": "email", "Value": "benlin.chch+jt@gmail.com"},
                    {"Name": "name", "Value": "Jarley Trainor"}]},
]

MAPPING = {
    "sites": {"sb1108-ellesmere": {"name": "SB1108 Ellesmere College",
                                   "location": "Christchurch",
                                   "client": "Ministry of Education"}},
    "mapping": {"Benl1": {"name": "Jarley Trainor", "role": "site_manager",
                          "sites": ["sb1108-ellesmere"]}},
}


def test_resolve_role_admin_override_beats_mapping():
    by_name = seed.mapping_by_name(MAPPING)
    assert seed.resolve_role("benl.tech@outlook.com", "Ben Lin",
                             {"benl.tech@outlook.com"}, by_name) == "admin"
    assert seed.resolve_role("benlin.chch+jt@gmail.com", "Jarley Trainor",
                             set(), by_name) == "site_manager"
    assert seed.resolve_role("x@x.nz", "Nobody Known", set(), by_name) == "worker"


def test_split_name():
    assert seed.split_name("Jarley Trainor") == ("Jarley", "Trainor")
    assert seed.split_name("MPI1") == ("MPI1", None)
    assert seed.split_name("") == (None, None)


def test_handler_seeds_company_users_sites_memberships(monkeypatch):
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls = {"users": [], "sites": [], "memberships": []}
    monkeypatch.setattr(seed, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(seed, "load_mapping", lambda: MAPPING)
    monkeypatch.setattr(seed, "list_cognito_users", lambda: COGNITO_USERS)
    monkeypatch.setattr(seed.companies, "get_company_by_name", lambda c, n: None)
    monkeypatch.setattr(seed.companies, "create_company",
                        lambda c, n: {"id": "c-1", "name": n})
    monkeypatch.setattr(seed.sites, "get_company_site_by_name", lambda c, cid, n: None)
    monkeypatch.setattr(seed.sites, "create_site",
                        lambda c, cid, name, **kw: (calls["sites"].append(name)
                                                    or {"id": "s-" + name[:6], "name": name}))
    monkeypatch.setattr(seed.users, "upsert_user",
                        lambda c, sub, email, **kw: (calls["users"].append((sub, kw.get("global_role")))
                                                     or {"id": "u-" + sub, "cognito_sub": sub}))
    monkeypatch.setattr(seed.memberships, "ensure_membership",
                        lambda c, uid, sid, role: (calls["memberships"].append((uid, sid, role))
                                                   or {"id": "m-1"}))

    out = seed.lambda_handler({"company_name": "TestCo"}, None)
    assert out["company"]["name"] == "TestCo"
    assert ("sub-admin", "admin") in calls["users"]
    assert ("sub-jt", "site_manager") in calls["users"]
    assert calls["sites"] == ["SB1108 Ellesmere College"]
    assert calls["memberships"] == [("u-sub-jt", "s-SB1108", "site_manager")]
    assert out["users"] == 2 and out["sites"] == 1 and out["memberships"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_lambda_org_seed.py -v`
Expected: FAIL — `ModuleNotFoundError: lambda_org_seed`.

- [ ] **Step 3: Implement**

Create `src/lambda_org_seed.py`:

```python
"""
Lambda: fieldsight-org-seed v1.0 — one-shot idempotent org backfill (Phase 3)

Manual invoke only. Creates the company row, mirrors the Cognito user pool
(real login users) into Aurora users, creates sites from S3
config/user_mapping.json, and enrolls mapped users as memberships.
Re-running changes nothing (get-or-create + upserts).

Event: {"company_name"?: str, "admin_emails"?: [str]}
Needs: cognito-idp interface endpoint + S3 gateway endpoint (in-VPC, no NAT).
"""
import json
import logging
import os

import boto3

from db.connection import get_connection
from repositories import companies, memberships, sites, users

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
CONFIG_KEY = os.environ.get("CONFIG_KEY", "config/user_mapping.json")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
DEFAULT_COMPANY = "FieldSight"
DEFAULT_ADMIN_EMAILS = ["benl.tech@outlook.com"]


def load_mapping() -> dict:
    obj = boto3.client("s3").get_object(Bucket=S3_BUCKET, Key=CONFIG_KEY)
    return json.loads(obj["Body"].read().decode("utf-8"))


def list_cognito_users() -> list:
    client = boto3.client("cognito-idp")
    out, token = [], None
    while True:
        kwargs = {"UserPoolId": COGNITO_USER_POOL_ID}
        if token:
            kwargs["PaginationToken"] = token
        resp = client.list_users(**kwargs)
        out.extend(resp.get("Users", []))
        token = resp.get("PaginationToken")
        if not token:
            return out


def attrs_of(user) -> dict:
    return {a["Name"]: a["Value"] for a in user.get("Attributes", [])}


def mapping_by_name(mapping: dict) -> dict:
    """device→info mapping re-keyed by lowercased person name."""
    return {info["name"].lower(): info
            for info in mapping.get("mapping", {}).values() if info.get("name")}


def resolve_role(email, name, admin_emails, by_name) -> str:
    if email.lower() in admin_emails:
        return "admin"
    info = by_name.get((name or "").lower())
    return info.get("role", "worker") if info else "worker"


def split_name(name):
    parts = (name or "").strip().split(None, 1)
    if not parts:
        return (None, None)
    return (parts[0], parts[1] if len(parts) > 1 else None)


def lambda_handler(event, context):
    event = event or {}
    company_name = event.get("company_name", DEFAULT_COMPANY)
    admin_emails = {e.lower() for e in event.get("admin_emails", DEFAULT_ADMIN_EMAILS)}

    mapping = load_mapping()
    by_name = mapping_by_name(mapping)
    cognito_users = list_cognito_users()

    n_users = n_sites = n_memberships = 0
    with get_connection() as conn:
        company = (companies.get_company_by_name(conn, company_name)
                   or companies.create_company(conn, company_name))

        slug_to_site = {}
        for slug, s in mapping.get("sites", {}).items():
            site = sites.get_company_site_by_name(conn, company["id"], s["name"])
            if site is None:
                site = sites.create_site(conn, company["id"], s["name"],
                                         location=s.get("location"),
                                         client=s.get("client"))
                n_sites += 1
            slug_to_site[slug] = site

        for cu in cognito_users:
            a = attrs_of(cu)
            sub, email, name = a.get("sub"), a.get("email", ""), a.get("name", "")
            if not sub or not email:
                continue
            first, last = split_name(name)
            role = resolve_role(email, name, admin_emails, by_name)
            user = users.upsert_user(conn, sub, email, company_id=company["id"],
                                     first_name=first, last_name=last,
                                     global_role=role)
            n_users += 1
            info = by_name.get(name.lower())
            if info:
                for slug in info.get("sites", []):
                    site = slug_to_site.get(slug)
                    if site:
                        memberships.ensure_membership(
                            conn, user["id"], site["id"],
                            info.get("role", "worker"))
                        n_memberships += 1

    logger.info("seed done: company=%s users=%d sites=%d memberships=%d",
                company_name, n_users, n_sites, n_memberships)
    return {"company": company, "users": n_users,
            "sites": n_sites, "memberships": n_memberships}
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_lambda_org_seed.py -v` then `python -m pytest tests/unit -v`
Expected: all PASS.

- [ ] **Step 5: Add OrgSeedFunction to the template**

In `src/template.yaml`, after the `OrgApiFunction` resource block, insert:

```yaml
  # ----------------------------------------------------------
  # Lambda 11: Org Seed (Phase 3) — one-shot idempotent backfill of
  # company/users/sites/memberships from Cognito + config/user_mapping.json.
  # Manual invoke only (no Events).
  # ----------------------------------------------------------
  OrgSeedFunction:
    Type: AWS::Serverless::Function
    Condition: HasOrgApi
    Properties:
      FunctionName: !Sub ["${P}-org-seed", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_org_seed.lambda_handler
      Timeout: 120
      MemorySize: 256
      Layers:
        - !Ref PsycopgLayer
      VpcConfig:
        SubnetIds: !Ref DbSubnetIds
        SecurityGroupIds:
          - !ImportValue
            Fn::Sub: "${DbStackName}-LambdaSG"
      Environment:
        Variables:
          PGHOST: !ImportValue
            Fn::Sub: "${DbStackName}-ClusterEndpoint"
          PGDATABASE: !ImportValue
            Fn::Sub: "${DbStackName}-DbName"
          PGUSER: postgres
          PGPASSWORD: !Sub '{{resolve:secretsmanager:${DbSecretArn}:SecretString:password}}'
          COGNITO_USER_POOL_ID: !Ref OrgUserPoolId
          S3_BUCKET: !Ref DataBucketName
          CONFIG_KEY: config/user_mapping.json
      Policies:
        - VPCAccessPolicy: {}
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: cognito-idp:ListUsers
              Resource: !Sub arn:aws:cognito-idp:${AWS::Region}:${AWS::AccountId}:userpool/${OrgUserPoolId}
            - Effect: Allow
              Action: s3:GetObject
              Resource: !Sub arn:aws:s3:::${DataBucketName}/config/user_mapping.json
```

And after `OrgApiLogGroup`:

```yaml
  OrgSeedLogGroup:
    Type: AWS::Logs::LogGroup
    Condition: HasOrgApi
    Properties:
      LogGroupName: !Sub /aws/lambda/${OrgSeedFunction}
      RetentionInDays: 14
```

- [ ] **Step 6: Validate template**

Run: `cfn-lint src/template.yaml`
Expected: clean.

**Seed data caveat (document, don't fix in code):** the TEST bucket's `config/user_mapping.json` must exist (copy of prod config). Verify at Task 11 with `aws s3 ls s3://fieldsight-data-test-509194952652/config/user_mapping.json`; if missing, copy: `aws s3 cp s3://fieldsight-data-509194952652/config/user_mapping.json s3://fieldsight-data-test-509194952652/config/user_mapping.json`. Also: the local repo copy has 3 sites — live S3 is canonical (handoff said 4; seed reads live, so the discrepancy is harmless).

- [ ] **Step 7: Commit**

```bash
git add src/lambda_org_seed.py tests/unit/test_lambda_org_seed.py src/template.yaml
git commit -m "feat(3): org seed lambda — idempotent cognito+user_mapping backfill"
```

---

### Task 11: PR, deploy, seed, live smoke verification, docs

**Files:**
- Modify: `docs/MIGRATION-HANDOFF-2026-07-04.md` (§5 status note), `.superpowers/sdd/progress.md`

**Interfaces:**
- Consumes: everything above; GitHub CI (test.yml must be green on the PR; deploy.yml fires on merge to develop).
- Produces: live verified `/api/org/*` on the TEST gateway; seeded Aurora org data; updated ledger/handoff.

- [ ] **Step 1: Pre-PR checks**

```bash
python -m pytest tests/unit -v          # all green
cfn-lint src/template.yaml infra/db-template.yaml
git log --oneline origin/develop..HEAD  # only this phase's commits
git status --short                      # ONLY intended files; user's untracked notes untouched
```

- [ ] **Step 2: Push branch + open PR to develop**

```bash
git push -u origin feature/phase3-org-api
gh pr create --base develop --title "Phase 3: org write API (OrgApiFunction + seed)" --body "..."
```

Wait for PR checks (test.yml runs integration tests in the pgvector container; ci.yml lints both templates). Fix loop if red.

- [ ] **Step 3: Merge (API merge — local dirty tree makes `gh pr merge` abort)**

```bash
gh api -X PUT repos/{owner}/{repo}/pulls/<N>/merge -f merge_method=squash
```

(Get owner/repo from `gh repo view --json nameWithOwner`.) Then watch deploy: `gh run watch` or `gh run list --workflow=deploy.yml --limit 1`. Deploy prerequisites that can fail here: the deploy-role IAM grant from Task 9 Step 6 (s3:PutBucketCORS) — if the CORS step fails, apply the grant and re-run the workflow.

- [ ] **Step 4: Verify test-bucket config exists** (see Task 10 caveat) — copy `config/user_mapping.json` to the test bucket if absent.

- [ ] **Step 5: Invoke seed and verify**

```bash
export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1 MSYS_NO_PATHCONV=1
aws lambda invoke --function-name fieldsight-test-org-seed \
  --payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout --region ap-southeast-2
```

Expected: `{"company": {...}, "users": 4, "sites": 3, "memberships": ...}` (counts = live pool/config contents). Re-invoke once → same output shape, `sites: 0` new (idempotency: users count stays, nothing duplicated). Verify rows via Data API:

```bash
CLUSTER_ARN=$(aws cloudformation list-exports --query "Exports[?Name=='fieldsight-db-test-ClusterArn'].Value" --output text --region ap-southeast-2)
SECRET_ARN=$(aws cloudformation list-exports --query "Exports[?Name=='fieldsight-db-test-SecretArn'].Value" --output text --region ap-southeast-2)
aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
  --database fieldsight --sql "SELECT email, global_role FROM users ORDER BY created_at" \
  --region ap-southeast-2 --output json
```

Expected: 4 rows; `benl.tech@outlook.com` = admin; site_manager rows per mapping.

- [ ] **Step 6: Smoke the org API via direct invoke (synthetic authorizer claims)**

Get the admin sub: `aws cognito-idp list-users --user-pool-id ap-southeast-2_q88pd6XXr --query 'Users[].Attributes[?Name==`sub`].Value' --output text --region ap-southeast-2` (pick benl.tech's). Then:

```bash
SUB=<admin-sub>
printf '{"httpMethod":"GET","path":"/api/org/me","requestContext":{"authorizer":{"claims":{"sub":"%s"}}}}' "$SUB" > /tmp/ev.json
aws lambda invoke --function-name fieldsight-test-org-api \
  --payload "file://$(cygpath -w /tmp/ev.json)" --cli-binary-format raw-in-base64-out /dev/stdout --region ap-southeast-2
```

Expected: statusCode 200, body has `"global_role": "admin"` and non-empty `site_ids`. Repeat for `GET /api/org/sites` (expect 3 sites) and `GET /api/org/members` (expect 4 members). Any hang-to-timeout = BUG-36 symptom → check Task 1's endpoints are `available`.

- [ ] **Step 7: End-to-end through the gateway (real token — optional but preferred)**

Get the test gateway id: `aws cloudformation describe-stacks --stack-name fieldsight-test --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text --region ap-southeast-2`. Then a real prod-pool idToken is needed (from the deployed UI's sessionStorage `fs.session.v1`, or via cognito-idp initiate-auth with the user's password — coordinate with the user). Call `GET <endpoint>/org/me` with `Authorization: <raw idToken>`; expect 200. If skipped here, this is covered by the UI-plan Chrome verification.

- [ ] **Step 8: Update docs + ledger, commit on develop... no — commit via the normal flow**

Append to `.superpowers/sdd/progress.md`: Phase 3 backend completion line (stack state, seed counts, gateway URL, anything deferred). Update `docs/MIGRATION-HANDOFF-2026-07-04.md` §5 header to "后端已上线(develop),UI 接线见 ui 仓计划". Commit these on a short branch or directly per repo convention (docs-only commits have gone straight to develop before — match `git log` precedent; deploy.yml ignores `docs/**` and `**/*.md` so no redeploy fires).

---

## Self-Review Notes (completed)

- Handoff §5 coverage: OrgApiFunction ✅(T9) · in-VPC psycopg ✅(T9) · `/api/org/{proxy+}` + Cognito authorizer ✅(T9, dual-pool addition surfaced as a plan-level fix — handoff didn't mention the pool mismatch) · cognito-idp endpoint + S3 gateway endpoint ✅(T1) · presign offline ✅(T8) · BUG-36 creds ✅(T9/T10) · endpoint set ✅(T4-T8; +GET /members documented deviation) · seed ✅(T10/T11) · 双基址/FS_ORGWRITES = UI plan (out of scope here, stated in header).
- Type consistency: `ensure_membership(conn, user_id, site_id, role)` used in T7/T10 matches T3; `set_global_role(conn, cognito_sub, company_id, global_role)` matches T2/T6; `update_profile` kwargs match T2/T4; route strings in dispatch match tests.
- Placeholder scan: T11 Step 2 PR body "..." is intentionally free-form (execution-time content); no TBDs in code steps.
- Known accepted risks, documented in code/comments: prod-pool tokens valid on all TEST /api/* routes (T9 comment); cognito-create-then-DB-rollback orphan is retry-safe (T7 docstring); `upsert_user` in seed re-applies mapping roles on re-run (explicit roles passed — a role later changed via PATCH would be reset by a re-seed; acceptable for a one-shot tool, noted for the ledger).
