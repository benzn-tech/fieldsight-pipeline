# QR Passwordless Sign-In — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authenticated FieldSight web session mint a one-time code that a GrandTime terminal redeems — via Cognito custom auth — to obtain its own Cognito tokens, with no password typed on the terminal.

**Architecture:** One authed org-api route (`POST /api/org/auth/qr/create`) writes a short-lived, single-use code to a new DynamoDB table (TTL). The terminal completes a Cognito **CUSTOM_AUTH** flow (InitiateAuth → RespondToAuthChallenge) answered with that code; three new Cognito trigger Lambdas (Define/Create/Verify) validate + consume the code and tell Cognito to issue tokens. The terminal talks to Cognito directly — redemption never touches API Gateway — so no unauthenticated endpoint is needed.

**Tech Stack:** Python 3.11 Lambda, AWS SAM (`src/template.yaml`), DynamoDB (boto3 `resource`), Cognito custom-auth triggers, existing org-api (`src/lambda_org_api.py`). Tests: pytest via `uv` (repo harness).

**Design spec:** `../specs/2026-07-30-qr-login-design.md` (in the GrandTime repo:
`C:/Users/camil/Dropbox/GrandTime/docs/superpowers/specs/2026-07-30-qr-login-design.md`).

## Global Constraints

- **Pool is shared test↔prod** (`ap-southeast-2_q88pd6XXr`), passed as SAM Parameter `OrgUserPoolId`. Triggers + app-client flow are pool/client-level → **one deployment affects both envs; no isolated pool to rehearse on** (BUG-38). Custom-auth triggers fire ONLY for `CUSTOM_AUTH`, so existing `USER_PASSWORD_AUTH` login is untouched.
- **The Cognito pool and app client `4ratjdjonqm17tln6bs2761ci3` are NOT in this repo's IaC** — managed out-of-band. So `ALLOW_CUSTOM_AUTH` and the pool `LambdaConfig` are **manual CLI steps** (Task 4), done with `describe → merge → update` to avoid clobbering the shared prod pool.
- **New CFN resource types require deploy-role IAM first**: grant `github-actions-fieldsight-deploy` (inline policy `FieldSightDeployPolicy`) create/manage perms for DynamoDB + Lambda-permission BEFORE deploying, or CREATE_FAILED rolls back the whole stack (memory *org-api-new-route-iam-trap*). Task 4a.
- **Never log the code value** anywhere (create, triggers).
- **Identity match by `sub`, not username** — the Verify trigger compares the stored `sub` against `event.request.userAttributes.sub` (robust to email-alias pools).
- **v1 = self-service**: the code logs in the caller's own account; no admin-provisioning, no test create endpoint (redemption is env-agnostic — a prod code is scannable by any terminal).
- Code TTL = **90 seconds**, single-use. Code entropy = `secrets.token_urlsafe(32)` (~256-bit).
- pytest invocation (Windows + uv), run from repo root:
  ```bash
  export UV_LINK_MODE=copy
  export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
  uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
  ```

---

## File Structure

- **Create:** `src/lambda_qr_auth.py` — the 3 Cognito trigger handlers (Define/Create/Verify) + code verify/consume helper. Pure, DynamoDB injectable.
- **Create:** `tests/unit/test_lambda_qr_auth.py` — trigger unit tests.
- **Modify:** `src/lambda_org_api.py` — add `POST /auth/qr/create` dispatch branch + `create_qr_login_code` handler + a small DynamoDB helper (`_qr_table`, `_qr_rate_ok`).
- **Modify:** `tests/unit/test_lambda_org_api.py` — create-endpoint tests.
- **Modify:** `src/template.yaml` — `QrLoginCodesTable` (DynamoDB, TTL), 3 trigger Functions, 3 `AWS::Lambda::Permission`, OrgApiFunction env + IAM, Outputs for the 3 trigger ARNs.

---

### Task 1: Cognito trigger handlers (`lambda_qr_auth.py`)

**Files:**
- Create: `src/lambda_qr_auth.py`
- Test: `tests/unit/test_lambda_qr_auth.py`

**Interfaces:**
- Produces:
  - `define_auth_challenge(event, context) -> event`
  - `create_auth_challenge(event, context) -> event`
  - `verify_auth_challenge_response(event, context) -> event`
  - `_verify_and_consume(sub: str, code: str) -> bool` (internal; tests monkeypatch `_table`)
  - `_table()` — lazy `boto3.resource("dynamodb").Table(os.environ["QR_CODES_TABLE"])`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_lambda_qr_auth.py`:

```python
import time
import pytest

qr = pytest.importorskip("lambda_qr_auth", reason="requires boto3 (installed in CI)")


class FakeTable:
    """Minimal DynamoDB Table double: get_item + conditional update_item."""
    def __init__(self, item=None):
        self.item = dict(item) if item else None
        self.updated = False

    def get_item(self, Key):
        if self.item and self.item.get("code") == Key["code"]:
            return {"Item": dict(self.item)}
        return {}

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues):
        # emulate ConditionExpression "consumed = :f"
        if not self.item or self.item.get("consumed") is not False:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.item["consumed"] = True
        self.updated = True
        return {}


def _define(session):
    return {"request": {"session": session}, "response": {}}


def test_define_issues_custom_challenge_on_first_call():
    ev = qr.define_auth_challenge(_define([]), None)
    assert ev["response"]["challengeName"] == "CUSTOM_CHALLENGE"
    assert ev["response"]["issueTokens"] is False
    assert ev["response"]["failAuthentication"] is False


def test_define_issues_tokens_after_correct_answer():
    session = [{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": True}]
    ev = qr.define_auth_challenge(_define(session), None)
    assert ev["response"]["issueTokens"] is True
    assert ev["response"]["failAuthentication"] is False


def test_define_fails_after_wrong_answer():
    session = [{"challengeName": "CUSTOM_CHALLENGE", "challengeResult": False}]
    ev = qr.define_auth_challenge(_define(session), None)
    assert ev["response"]["issueTokens"] is False
    assert ev["response"]["failAuthentication"] is True


def test_create_sets_empty_challenge_params():
    ev = qr.create_auth_challenge(
        {"request": {"challengeName": "CUSTOM_CHALLENGE"}, "response": {}}, None)
    assert ev["response"]["publicChallengeParameters"] == {}
    assert ev["response"]["privateChallengeParameters"] == {}


def _verify_event(code, sub):
    return {
        "userName": "cognito-user-name",
        "request": {"challengeAnswer": code, "userAttributes": {"sub": sub}},
        "response": {},
    }


def test_verify_accepts_valid_code_and_consumes(monkeypatch):
    now = int(time.time())
    table = FakeTable({"code": "good", "sub": "sub-1", "consumed": False, "expiresAt": now + 90})
    monkeypatch.setattr(qr, "_table", lambda: table)
    ev = qr.verify_auth_challenge_response(_verify_event("good", "sub-1"), None)
    assert ev["response"]["answerCorrect"] is True
    assert table.updated is True


def test_verify_rejects_wrong_sub(monkeypatch):
    now = int(time.time())
    table = FakeTable({"code": "good", "sub": "sub-1", "consumed": False, "expiresAt": now + 90})
    monkeypatch.setattr(qr, "_table", lambda: table)
    ev = qr.verify_auth_challenge_response(_verify_event("good", "someone-else"), None)
    assert ev["response"]["answerCorrect"] is False


def test_verify_rejects_expired(monkeypatch):
    now = int(time.time())
    table = FakeTable({"code": "old", "sub": "sub-1", "consumed": False, "expiresAt": now - 1})
    monkeypatch.setattr(qr, "_table", lambda: table)
    ev = qr.verify_auth_challenge_response(_verify_event("old", "sub-1"), None)
    assert ev["response"]["answerCorrect"] is False


def test_verify_rejects_already_consumed(monkeypatch):
    now = int(time.time())
    table = FakeTable({"code": "used", "sub": "sub-1", "consumed": True, "expiresAt": now + 90})
    monkeypatch.setattr(qr, "_table", lambda: table)
    ev = qr.verify_auth_challenge_response(_verify_event("used", "sub-1"), None)
    assert ev["response"]["answerCorrect"] is False


def test_verify_rejects_unknown_code(monkeypatch):
    monkeypatch.setattr(qr, "_table", lambda: FakeTable(None))
    ev = qr.verify_auth_challenge_response(_verify_event("nope", "sub-1"), None)
    assert ev["response"]["answerCorrect"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest --with boto3 pytest tests/unit/test_lambda_qr_auth.py -q`
Expected: FAIL — `lambda_qr_auth` not importable (module doesn't exist yet).

- [ ] **Step 3: Write the implementation**

Create `src/lambda_qr_auth.py`:

```python
"""Cognito custom-auth triggers for QR passwordless sign-in.

Flow: DefineAuthChallenge -> CreateAuthChallenge -> VerifyAuthChallengeResponse.
The terminal answers a single CUSTOM_CHALLENGE with a one-time code minted by an
authenticated web session (org-api POST /api/org/auth/qr/create) and stored in
DynamoDB. Identity is matched by the Cognito `sub` (robust to email-alias pools).
The code value is never logged.
"""
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("QR_CODES_TABLE", "fieldsight-qr-login-codes")
_ddb_table = None


def _table():
    global _ddb_table
    if _ddb_table is None:
        _ddb_table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _ddb_table


def define_auth_challenge(event, context):
    session = event["request"].get("session") or []
    resp = event["response"]
    if not session:
        # First step: present our custom challenge.
        resp["issueTokens"] = False
        resp["failAuthentication"] = False
        resp["challengeName"] = "CUSTOM_CHALLENGE"
    else:
        last = session[-1]
        if last.get("challengeName") == "CUSTOM_CHALLENGE" and last.get("challengeResult") is True:
            resp["issueTokens"] = True
            resp["failAuthentication"] = False
        else:
            resp["issueTokens"] = False
            resp["failAuthentication"] = True
    return event


def create_auth_challenge(event, context):
    if event["request"].get("challengeName") == "CUSTOM_CHALLENGE":
        # Nothing to send — the terminal already holds the code from the QR.
        event["response"]["publicChallengeParameters"] = {}
        event["response"]["privateChallengeParameters"] = {}
        event["response"]["challengeMetadata"] = "QR_CODE"
    return event


def verify_auth_challenge_response(event, context):
    answer = (event["request"].get("challengeAnswer") or "").strip()
    sub = (event["request"].get("userAttributes") or {}).get("sub", "")
    event["response"]["answerCorrect"] = _verify_and_consume(sub, answer)
    return event


def _verify_and_consume(sub, code):
    if not code or not sub:
        return False
    try:
        item = _table().get_item(Key={"code": code}).get("Item")
    except Exception:
        logger.exception("qr verify get_item failed")
        return False
    if not item or item.get("sub") != sub or item.get("consumed"):
        return False
    if int(time.time()) >= int(item.get("expiresAt", 0)):
        return False
    # Atomic single-use: only the first verifier flips consumed false->true.
    try:
        _table().update_item(
            Key={"code": code},
            UpdateExpression="SET consumed = :t",
            ConditionExpression="consumed = :f",
            ExpressionAttributeValues={":t": True, ":f": False},
        )
    except Exception:
        return False  # lost the race / conditional failed → invalid
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --with pytest --with boto3 pytest tests/unit/test_lambda_qr_auth.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/lambda_qr_auth.py tests/unit/test_lambda_qr_auth.py
git commit -m "feat(auth): Cognito custom-auth triggers for QR passwordless login"
```

---

### Task 2: `POST /api/org/auth/qr/create` endpoint

**Files:**
- Modify: `src/lambda_org_api.py` (dispatch cascade `~:180-317`; helpers near the `_cognito_client` block `~:124-135`; imports near top `~:1-30`)
- Test: `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: existing `ok`/`error`/`dispatch`/`get_user_by_sub`/`caller` (from org-api).
- Produces: `create_qr_login_code(conn, caller, event) -> response`; helpers `_qr_table()`, `_qr_rate_ok(sub) -> bool`. Route: `POST /api/org/auth/qr/create` → `201 {"code","expiresAt","ttlSeconds"}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_lambda_org_api.py`:

```python
class FakeQrTable:
    def __init__(self):
        self.items = {}
        self.rate_hits = {}

    def put_item(self, Item):
        self.items[Item["code"]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues=None):
        # emulate the rate-limit "ADD hits :one" counter
        k = Key["code"]
        self.rate_hits[k] = self.rate_hits.get(k, 0) + int(ExpressionAttributeValues[":one"])
        return {"Attributes": {"hits": self.rate_hits[k]}}


def test_qr_create_returns_code(wired, monkeypatch):
    table = FakeQrTable()
    monkeypatch.setattr(org, "_qr_table", lambda: table)
    res = org.lambda_handler(make_event("POST", "/api/org/auth/qr/create", sub="sub-1"), None)
    assert res["statusCode"] == 201
    body = body_of(res)
    assert body["ttlSeconds"] == 90
    assert len(body["code"]) >= 32
    # the stored record binds the code to the caller's sub, unconsumed
    stored = table.items[body["code"]]
    assert stored["sub"] == "sub-1"
    assert stored["consumed"] is False
    assert stored["expiresAt"] > stored["createdAt"]


def test_qr_create_requires_auth(wired, monkeypatch):
    monkeypatch.setattr(org, "_qr_table", lambda: FakeQrTable())
    # no sub in claims → caller resolves to None → 403 (handled by dispatch guard)
    res = org.lambda_handler(make_event("POST", "/api/org/auth/qr/create", sub=""), None)
    assert res["statusCode"] == 403


def test_qr_create_rate_limited(wired, monkeypatch):
    table = FakeQrTable()
    monkeypatch.setattr(org, "_qr_table", lambda: table)
    codes = 0
    for _ in range(7):
        res = org.lambda_handler(make_event("POST", "/api/org/auth/qr/create", sub="sub-1"), None)
        if res["statusCode"] == 201:
            codes += 1
    # ≤5 per minute succeed; the rest are 429
    assert codes == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_org_api.py -k qr_create -q`
Expected: FAIL — route returns 404 / `_qr_table` missing.

- [ ] **Step 3: Add imports + helpers**

At the top of `src/lambda_org_api.py`, ensure `import secrets`, `import time`, and `import os` are present (add any missing). Near the existing `_cognito_client` lazy-client block, add:

```python
QR_CODES_TABLE = os.environ.get("QR_CODES_TABLE", "fieldsight-qr-login-codes")
QR_TTL_SECONDS = 90
QR_RATE_PER_MIN = 5
_qr_ddb_table = None


def _qr_table():
    global _qr_ddb_table
    if _qr_ddb_table is None:
        _qr_ddb_table = boto3.resource("dynamodb").Table(QR_CODES_TABLE)
    return _qr_ddb_table


def _qr_rate_ok(sub):
    """Per-user create limiter using a per-minute counter item in the same table
    (PK namespaced "RATE#..." so it never collides with a random code). Fails open."""
    minute = int(time.time() // 60)
    key = f"RATE#{sub}#{minute}"
    try:
        resp = _qr_table().update_item(
            Key={"code": key},
            UpdateExpression="ADD hits :one SET expiresAt = :exp",
            ExpressionAttributeValues={":one": 1, ":exp": (minute + 2) * 60},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["hits"]) <= QR_RATE_PER_MIN
    except Exception:
        logger.exception("qr rate check failed")
        return True
```

- [ ] **Step 4: Add the handler**

Add near the other POST handlers in `src/lambda_org_api.py`:

```python
def create_qr_login_code(conn, caller, event):
    """Self-service: mint a one-time code that logs a terminal into the CALLER's
    own account via Cognito custom auth. Code bound to the caller's sub."""
    claims = (event.get("requestContext", {}) or {}).get("authorizer", {}).get("claims", {})
    sub = claims.get("sub", "")
    if not sub:
        return error("unauthenticated", 401)
    if not _qr_rate_ok(sub):
        return error("too many requests — try again in a minute", 429)
    code = secrets.token_urlsafe(32)  # ~256-bit
    now = int(time.time())
    expires = now + QR_TTL_SECONDS
    try:
        _qr_table().put_item(Item={
            "code": code,
            "sub": sub,
            "consumed": False,
            "createdAt": now,
            "expiresAt": expires,
        })
    except Exception:
        logger.exception("qr create put_item failed")  # never log `code`
        return error("could not create login code", 500)
    return ok({"code": code, "expiresAt": expires, "ttlSeconds": QR_TTL_SECONDS}, 201)
```

- [ ] **Step 5: Wire the route**

In `dispatch(...)`, before the final `return error("not found", 404)`, add:

```python
    if route == "/auth/qr/create" and method == "POST":
        return create_qr_login_code(conn, caller, event)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_org_api.py -k qr_create -q`
Expected: PASS (3 tests). Then run the full `tests/unit` suite to confirm no regressions.

- [ ] **Step 7: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(auth): POST /api/org/auth/qr/create mints one-time QR login codes"
```

---

### Task 3: SAM template — table, triggers, permissions, IAM

**Files:**
- Modify: `src/template.yaml` (Resources; OrgApiFunction `~:821-958`; Conditions `~:340-342`; Outputs)

**Interfaces:**
- Consumes: `OrgUserPoolId` Parameter, `HasOrgApi`/`HasOrgPool` conditions, `StageConfig` map.
- Produces: `QrLoginCodesTable`; functions `QrAuthDefineFunction`/`QrAuthCreateFunction`/`QrAuthVerifyFunction`; Outputs `QrAuthDefineFunctionArn`/`QrAuthCreateFunctionArn`/`QrAuthVerifyFunctionArn` (consumed by Task 4d).

- [ ] **Step 1: Add the DynamoDB table**

Under `Resources:` add:

```yaml
  QrLoginCodesTable:
    Type: AWS::DynamoDB::Table
    Condition: HasOrgApi
    Properties:
      TableName: fieldsight-qr-login-codes
      BillingMode: PAY_PER_REQUEST
      AttributeDefinitions:
        - { AttributeName: code, AttributeType: S }
      KeySchema:
        - { AttributeName: code, KeyType: HASH }
      TimeToLiveSpecification:
        AttributeName: expiresAt
        Enabled: true
```

- [ ] **Step 2: Add the three trigger functions**

```yaml
  QrAuthDefineFunction:
    Type: AWS::Serverless::Function
    Condition: HasOrgApi
    Properties:
      FunctionName: !Sub ["${P}-qr-auth-define", { P: !FindInMap [StageConfig, !Ref Stage, Prefix] }]
      CodeUri: src/
      Handler: lambda_qr_auth.define_auth_challenge
      Timeout: 10
      MemorySize: 128

  QrAuthCreateFunction:
    Type: AWS::Serverless::Function
    Condition: HasOrgApi
    Properties:
      FunctionName: !Sub ["${P}-qr-auth-create", { P: !FindInMap [StageConfig, !Ref Stage, Prefix] }]
      CodeUri: src/
      Handler: lambda_qr_auth.create_auth_challenge
      Timeout: 10
      MemorySize: 128

  QrAuthVerifyFunction:
    Type: AWS::Serverless::Function
    Condition: HasOrgApi
    Properties:
      FunctionName: !Sub ["${P}-qr-auth-verify", { P: !FindInMap [StageConfig, !Ref Stage, Prefix] }]
      CodeUri: src/
      Handler: lambda_qr_auth.verify_auth_challenge_response
      Timeout: 10
      MemorySize: 128
      Environment:
        Variables:
          QR_CODES_TABLE: !Ref QrLoginCodesTable
      Policies:
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: [dynamodb:GetItem, dynamodb:UpdateItem]
              Resource: !GetAtt QrLoginCodesTable.Arn
```

> These functions are **non-VPC** — they only call DynamoDB (public endpoint), never Aurora, so they must NOT get `VpcConfig` (a NAT-less VPC would black-hole the DynamoDB call — BUG-36).

- [ ] **Step 3: Add Lambda invoke permissions for Cognito**

```yaml
  QrAuthDefinePermission:
    Type: AWS::Lambda::Permission
    Condition: HasOrgApi
    Properties:
      Action: lambda:InvokeFunction
      FunctionName: !Ref QrAuthDefineFunction
      Principal: cognito-idp.amazonaws.com
      SourceArn: !Sub arn:aws:cognito-idp:${AWS::Region}:${AWS::AccountId}:userpool/${OrgUserPoolId}

  QrAuthCreatePermission:
    Type: AWS::Lambda::Permission
    Condition: HasOrgApi
    Properties:
      Action: lambda:InvokeFunction
      FunctionName: !Ref QrAuthCreateFunction
      Principal: cognito-idp.amazonaws.com
      SourceArn: !Sub arn:aws:cognito-idp:${AWS::Region}:${AWS::AccountId}:userpool/${OrgUserPoolId}

  QrAuthVerifyPermission:
    Type: AWS::Lambda::Permission
    Condition: HasOrgApi
    Properties:
      Action: lambda:InvokeFunction
      FunctionName: !Ref QrAuthVerifyFunction
      Principal: cognito-idp.amazonaws.com
      SourceArn: !Sub arn:aws:cognito-idp:${AWS::Region}:${AWS::AccountId}:userpool/${OrgUserPoolId}
```

- [ ] **Step 4: Give OrgApiFunction table access + env**

In `OrgApiFunction.Properties.Environment.Variables` add:
```yaml
        QR_CODES_TABLE: !Ref QrLoginCodesTable
```
In `OrgApiFunction.Properties.Policies` inline statement list, add:
```yaml
      - Effect: Allow
        Action: [dynamodb:PutItem, dynamodb:UpdateItem]
        Resource: !GetAtt QrLoginCodesTable.Arn
```

- [ ] **Step 5: Add Outputs for the trigger ARNs**

Under `Outputs:`:
```yaml
  QrAuthDefineFunctionArn:
    Condition: HasOrgApi
    Value: !GetAtt QrAuthDefineFunction.Arn
  QrAuthCreateFunctionArn:
    Condition: HasOrgApi
    Value: !GetAtt QrAuthCreateFunction.Arn
  QrAuthVerifyFunctionArn:
    Condition: HasOrgApi
    Value: !GetAtt QrAuthVerifyFunction.Arn
```

- [ ] **Step 6: Validate the template**

Run: `sam validate --template src/template.yaml --region ap-southeast-2` (or `--lint`).
Expected: "template is valid". Fix any YAML/ref errors before committing.

- [ ] **Step 7: Commit**

```bash
git add src/template.yaml
git commit -m "infra(auth): QR login code table + Cognito custom-auth trigger functions"
```

---

### Task 4: Deploy-role IAM, deploy, out-of-band Cognito wiring, end-to-end rehearsal

> Several sub-steps are permission-gated — the user runs them in-session with the `!` prefix. Each is marked **[user runs]**. Cognito `update-*` calls REPLACE unspecified fields, so every one uses **describe → merge → update**.

- [ ] **Step 1: [user runs] Grant the deploy role IAM for the new resource types**

The deploy role `github-actions-fieldsight-deploy` (inline policy `FieldSightDeployPolicy`) needs DynamoDB table management + `lambda:AddPermission` before the new resources will deploy. Inspect first, then add:

```bash
aws iam get-role-policy --role-name github-actions-fieldsight-deploy --policy-name FieldSightDeployPolicy
# Merge in (do not drop existing statements): a statement allowing
#   dynamodb:CreateTable, dynamodb:DescribeTable, dynamodb:UpdateTimeToLive,
#   dynamodb:DeleteTable, dynamodb:TagResource, dynamodb:DescribeTimeToLive
#   on arn:aws:dynamodb:ap-southeast-2:<acct>:table/fieldsight-qr-login-codes
# and (if not already present) lambda:AddPermission / lambda:RemovePermission on the stack's functions.
aws iam put-role-policy --role-name github-actions-fieldsight-deploy \
  --policy-name FieldSightDeployPolicy --policy-document file://<merged-policy>.json
```
Expected: no error; `get-role-policy` shows the added actions.

- [ ] **Step 2: Deploy to the shared infra (push `develop` → test stack)**

```bash
git push origin develop   # deploy.yml → fieldsight-test stack (auto-migrate; no DB change here)
```
Watch the GH Action. Expected: stack UPDATE_COMPLETE; the table + 3 functions exist. Verify:
```bash
aws dynamodb describe-table --table-name fieldsight-qr-login-codes --region ap-southeast-2 \
  --query 'Table.[TableStatus,TimeToLiveDescription]'
aws cloudformation describe-stacks --stack-name <test-stack> --region ap-southeast-2 \
  --query "Stacks[0].Outputs[?starts_with(OutputKey,'QrAuth')]"
```
Expected: table ACTIVE; three `QrAuth*FunctionArn` outputs printed. **Save the three ARNs** for Step 4.

- [ ] **Step 3: [user runs] Enable `ALLOW_CUSTOM_AUTH` on the mobile app client (describe → merge → update)**

```bash
aws cognito-idp describe-user-pool-client --user-pool-id ap-southeast-2_q88pd6XXr \
  --client-id 4ratjdjonqm17tln6bs2761ci3 --region ap-southeast-2 > /tmp/client.json
# Re-issue update-user-pool-client with ALL existing fields preserved, ExplicitAuthFlows =
#   existing flows + ALLOW_CUSTOM_AUTH  (keep ALLOW_USER_PASSWORD_AUTH + ALLOW_REFRESH_TOKEN_AUTH).
aws cognito-idp update-user-pool-client --user-pool-id ap-southeast-2_q88pd6XXr \
  --client-id 4ratjdjonqm17tln6bs2761ci3 --region ap-southeast-2 \
  --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH ALLOW_CUSTOM_AUTH \
  <plus every other currently-set field from /tmp/client.json>
```
Expected: `describe-user-pool-client` afterwards lists `ALLOW_CUSTOM_AUTH` alongside the originals; **no other field lost** (callback URLs, token validity, PreventUserExistenceErrors, etc.).

- [ ] **Step 4: [user runs] Attach the 3 triggers to the shared pool (describe → merge → update)**

> **Highest-risk step — shared PROD pool. `update-user-pool` resets omitted fields to defaults.** Capture the full current pool config and re-send it with `LambdaConfig` added.

```bash
aws cognito-idp describe-user-pool --user-pool-id ap-southeast-2_q88pd6XXr \
  --region ap-southeast-2 > /tmp/pool.json
# Build an update-user-pool call that preserves EVERY currently-set field (policies, MFA,
# EmailConfiguration, AccountRecoverySetting, AdminCreateUserConfig, UserAttributeUpdateSettings,
# etc.) and adds:
#   --lambda-config DefineAuthChallenge=<DefineArn>,CreateAuthChallenge=<CreateArn>,VerifyAuthChallengeResponse=<VerifyArn>
aws cognito-idp update-user-pool --user-pool-id ap-southeast-2_q88pd6XXr --region ap-southeast-2 \
  --lambda-config DefineAuthChallenge=<DefineArn>,CreateAuthChallenge=<CreateArn>,VerifyAuthChallengeResponse=<VerifyArn> \
  <plus every other currently-set field from /tmp/pool.json>
```
Expected: `describe-user-pool` afterwards shows the three `LambdaConfig` entries and an otherwise-unchanged pool. **Immediately smoke-test password login on web to confirm the pool still works.**

- [ ] **Step 5: End-to-end rehearsal via AWS CLI (no mobile app needed)**

Insert a test code bound to a real user's sub, then drive the custom-auth flow exactly as the terminal will:

```bash
# a) find a test user's sub
aws cognito-idp admin-get-user --user-pool-id ap-southeast-2_q88pd6XXr \
  --username <test-email> --region ap-southeast-2 --query "UserAttributes[?Name=='sub'].Value"
# b) put a code (90s) bound to that sub
NOW=$(date +%s)
aws dynamodb put-item --table-name fieldsight-qr-login-codes --region ap-southeast-2 \
  --item '{"code":{"S":"REHEARSE123"},"sub":{"S":"<sub>"},"consumed":{"BOOL":false},"createdAt":{"N":"'$NOW'"},"expiresAt":{"N":"'$((NOW+90))'"}}'
# c) initiate custom auth (as the terminal does)
aws cognito-idp initiate-auth --auth-flow CUSTOM_AUTH \
  --client-id 4ratjdjonqm17tln6bs2761ci3 \
  --auth-parameters USERNAME=<test-email> --region ap-southeast-2
# → capture Session
# d) answer with the code
aws cognito-idp respond-to-auth-challenge --client-id 4ratjdjonqm17tln6bs2761ci3 \
  --challenge-name CUSTOM_CHALLENGE --session "<SESSION>" \
  --challenge-responses USERNAME=<test-email>,ANSWER=REHEARSE123 --region ap-southeast-2
```
Expected: step (d) returns `AuthenticationResult` with `IdToken`/`AccessToken`/`RefreshToken`.
Then re-run (d) → expect a failure (code consumed / new session needed), proving single-use.
If (d) fails unexpectedly, tail the trigger logs:
```bash
aws logs tail /aws/lambda/<prefix>-qr-auth-verify --since 5m --region ap-southeast-2
```

- [ ] **Step 6: Deploy the create endpoint to prod + smoke-test**

```bash
# merge develop → main → deploy-prod.yml (approval gate). Then mint a real code end-to-end:
# sign into prod web (or call the endpoint with a valid prod idToken) →
curl -s -X POST https://ys94qy2tk0.execute-api.ap-southeast-2.amazonaws.com/prod/api/org/auth/qr/create \
  -H "Authorization: <prod idToken>"
# → 201 {"code":"...","expiresAt":...,"ttlSeconds":90}
# then run Step 5 (c)+(d) with that real code to confirm the full prod loop issues tokens.
```
Expected: 201 with a code; custom-auth exchange returns tokens.

- [ ] **Step 7: Commit any config notes**

```bash
git add -A && git commit -m "docs(auth): record QR custom-auth pool wiring + rehearsal results" || true
```

---

## Self-Review

**Spec coverage** (against `2026-07-30-qr-login-design.md`):
- §5.1 create endpoint → Task 2. §5.2 three triggers → Task 1. §5.3 `ALLOW_CUSTOM_AUTH` → Task 4.3.
- §6 code table (TTL, single table, prod-owned) → Task 3.1. §7 security (entropy/TTL/single-use/sub-binding/rate-limit/no-logging) → Tasks 1–2. §9 deployment (deploy-role IAM, deploy-once triggers, prod-only create, shared-pool rehearsal) → Task 4.
- Not in this plan (correctly — other repos): §8 mobile, §10 web.

**Placeholder scan:** the `<merged-policy>` / `<...currently-set field>` / `<SESSION>` / `<sub>` tokens in Task 4 are runtime values captured by the preceding `describe`/output commands, not unfilled plan blanks — each is defined by the command above it.

**Type consistency:** record shape `{code, sub, consumed, createdAt, expiresAt}` identical in Task 1 (verify reads `sub`/`consumed`/`expiresAt`), Task 2 (create writes all five), Task 3 (table PK `code`, TTL attr `expiresAt`). Verify matches on `event.request.userAttributes.sub`; create stores `claims.sub` — same Cognito sub. `_qr_table()`/`_table()` lazy-client pattern consistent with existing `_cognito_client`.

**Open items surfaced for the executor:** confirm the exact existing `ExplicitAuthFlows` + full pool config during Task 4.3/4.4 describe steps before re-sending (do not guess the field set).
