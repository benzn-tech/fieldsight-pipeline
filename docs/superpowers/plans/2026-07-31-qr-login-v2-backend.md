# QR Terminal Sign-In v2 (Refresh-Token Handoff) — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the blocked Cognito-CUSTOM_AUTH backend with a refresh-token handoff: the authed web session stashes its refresh token against a one-time code; the terminal redeems the code (unauthenticated) for that refresh token and completes `REFRESH_TOKEN_AUTH` itself.

**Architecture:** `POST /api/org/auth/qr/create` (authed) now stores the caller's refresh token with the code. A NEW **public** `POST /api/org/auth/qr/redeem` validates + atomically consumes the code and returns the refresh token. The 3 Cognito trigger Lambdas + `lambda_qr_auth.py` are removed; `QrLoginCodesTable` is kept and gains a non-key `refreshToken` attribute. The shared pool `LambdaConfig` is unwound out-of-band.

**Tech Stack:** Python 3.11 Lambda, AWS SAM (`src/template.yaml`), DynamoDB (boto3 resource), existing org-api (`src/lambda_org_api.py`). Tests: pytest via `uv` (repo harness).

**Design spec:** `GrandTime/docs/superpowers/specs/2026-07-31-qr-login-refresh-handoff-design.md`.

**Base branch:** cut `feat/qr-login-v2-backend` off **current `origin/develop`** (which already has the v1 create endpoint + table from PR #157). This plan MODIFIES those.

## Global Constraints
- **Never log** `code` or `refreshToken` anywhere (create, redeem, rate-limit).
- **Redeem is UNAUTHENTICATED** (the terminal has no session). It is the ONLY org-api route that runs before the dispatch caller-guard. Keep it minimal; it returns a token only for a valid, unconsumed, unexpired code, atomically consumed (single-use), rate-limited, generic errors (no enumeration).
- Code TTL = **90 s**, single-use. Code = `secrets.token_urlsafe(32)`.
- Record shape `{ code (PK, S), refreshToken (S), sub (S), consumed (BOOL), createdAt (N), expiresAt (N, TTL) }` — identical between create (writes all) and redeem (reads refreshToken, consumed, expiresAt).
- pytest (Windows + uv), from repo root:
  ```bash
  export UV_LINK_MODE=copy
  export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
  uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
  ```

---

## File Structure
- **Modify:** `src/lambda_org_api.py` — `create_qr_login_code` (store refreshToken) + NEW `redeem_qr_login_code` + a pre-caller-guard dispatch branch + a redeem rate-limit helper.
- **Modify:** `tests/unit/test_lambda_org_api.py` — create-stores-token test + redeem tests.
- **Modify:** `src/template.yaml` — add the public redeem `Api` event to OrgApiFunction; remove the 3 `QrAuth*Function`/`QrAuth*Permission`/`QrAuth*FunctionArn`. Keep `QrLoginCodesTable`.
- **Delete:** `src/lambda_qr_auth.py`, `tests/unit/test_lambda_qr_auth.py`.

---

### Task 1: `create` stores the web's refresh token

**Files:** Modify `src/lambda_org_api.py` (`create_qr_login_code`); Test `tests/unit/test_lambda_org_api.py`.

**Interfaces:**
- Modifies `create_qr_login_code(conn, caller, event)`: reads `refreshToken` from the parsed body; stores it in the item. Response unchanged (`{code, expiresAt, ttlSeconds}`).

- [ ] **Step 1: Update the failing test**

In `tests/unit/test_lambda_org_api.py`, update `test_qr_create_returns_code` (and keep the others) so the create body carries a refresh token and the stored item includes it:
```python
def test_qr_create_stores_refresh_token(wired, monkeypatch):
    table = FakeQrTable()
    monkeypatch.setattr(org, "_qr_table", lambda: table)
    res = org.lambda_handler(
        make_event("POST", "/api/org/auth/qr/create", sub="sub-1",
                   body={"refreshToken": "RT-abc"}), None)
    assert res["statusCode"] == 201
    stored = table.items[body_of(res)["code"]]
    assert stored["refreshToken"] == "RT-abc"
    assert stored["sub"] == "sub-1"
    assert stored["consumed"] is False
    assert stored["expiresAt"] > stored["createdAt"]
```
(Ensure `make_event` supports a `body=` kwarg that lands in `event.body` as JSON; if not, follow the file's existing pattern for POST bodies.)

- [ ] **Step 2: Run it, confirm it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_org_api.py -k qr_create -q`
Expected: FAIL — stored item has no `refreshToken`.

- [ ] **Step 3: Implement**

In `create_qr_login_code`, read the body's `refreshToken` and add it to the `put_item`:
```python
    body = parse_body(event) or {}
    refresh_token = body.get("refreshToken")
    if not refresh_token:
        return error("missing refreshToken", 400)
    ...
    _qr_table().put_item(Item={
        "code": code, "sub": sub, "refreshToken": refresh_token,
        "consumed": False, "createdAt": now, "expiresAt": expires,
    })
```
(Keep the existing rate-limit + `secrets.token_urlsafe(32)` + 201 response. Never log `code`/`refreshToken`.)

- [ ] **Step 4: Run, confirm pass**

Run the same `-k qr_create` command → PASS. Then the full `tests/unit` suite → no regressions.

- [ ] **Step 5: Commit**
```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(auth): qr create stores the web refresh token with the code"
```

---

### Task 2: public `redeem` endpoint + dispatch wiring

**Files:** Modify `src/lambda_org_api.py` (dispatch + new handler + rate helper); Test `tests/unit/test_lambda_org_api.py`.

**Interfaces:**
- Produces `redeem_qr_login_code(event) -> response` (no `conn`/`caller` — it runs pre-auth). Route `POST /api/org/auth/qr/redeem` → `200 {"refreshToken": ...}` | `401 {"error": "Invalid or expired code"}`.
- Consumes `_qr_table()`, `error`, `ok`, `parse_body` (existing).

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_lambda_org_api.py`:
```python
def test_qr_redeem_returns_token_and_consumes(monkeypatch):
    now = int(__import__("time").time())
    table = FakeQrTable()
    table.items["good"] = {"code": "good", "refreshToken": "RT-1", "sub": "s",
                            "consumed": False, "createdAt": now, "expiresAt": now + 90}
    monkeypatch.setattr(org, "_qr_table", lambda: table)
    res = org.lambda_handler(make_event("POST", "/api/org/auth/qr/redeem", sub="", body={"code": "good"}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["refreshToken"] == "RT-1"
    # second redeem fails (single-use)
    res2 = org.lambda_handler(make_event("POST", "/api/org/auth/qr/redeem", sub="", body={"code": "good"}), None)
    assert res2["statusCode"] == 401

def test_qr_redeem_rejects_expired(monkeypatch):
    now = int(__import__("time").time())
    table = FakeQrTable()
    table.items["old"] = {"code": "old", "refreshToken": "RT", "sub": "s",
                           "consumed": False, "createdAt": now-100, "expiresAt": now-1}
    monkeypatch.setattr(org, "_qr_table", lambda: table)
    res = org.lambda_handler(make_event("POST", "/api/org/auth/qr/redeem", sub="", body={"code": "old"}), None)
    assert res["statusCode"] == 401

def test_qr_redeem_unknown_code_generic(monkeypatch):
    monkeypatch.setattr(org, "_qr_table", lambda: FakeQrTable())
    res = org.lambda_handler(make_event("POST", "/api/org/auth/qr/redeem", sub="", body={"code": "nope"}), None)
    assert res["statusCode"] == 401
```
> Note `sub=""` — redeem must succeed with NO caller (it runs before the caller-guard). Extend `FakeQrTable` with a conditional `update_item` that flips `consumed false→true` and raises `ClientError(ConditionalCheckFailedException)` if already consumed (mirror the v1 FakeQrTable/RaceLoserTable pattern).

- [ ] **Step 2: Run, confirm fail**

Run: `... pytest tests/unit/test_lambda_org_api.py -k qr_redeem -q` → FAIL (route 404 / handler missing).

- [ ] **Step 3: Add the handler + rate helper**

```python
def redeem_qr_login_code(event):
    """PUBLIC (pre-auth): exchange a one-time code for the stored refresh token.
    Single-use atomic consume; generic errors; never logs code/token."""
    body = parse_body(event) or {}
    code = (body.get("code") or "").strip()
    if not code:
        return error("Invalid or expired code", 401)
    if not _qr_redeem_rate_ok(event):
        return error("too many requests", 429)
    try:
        item = _qr_table().get_item(Key={"code": code}).get("Item")
    except Exception:
        logger.exception("qr redeem get_item failed")  # never log `code`
        return error("Invalid or expired code", 401)
    if not item or item.get("consumed") or int(time.time()) >= int(item.get("expiresAt", 0)):
        return error("Invalid or expired code", 401)
    try:
        _qr_table().update_item(
            Key={"code": code},
            UpdateExpression="SET consumed = :t",
            ConditionExpression="consumed = :f",
            ExpressionAttributeValues={":t": True, ":f": False},
        )
    except Exception:
        return error("Invalid or expired code", 401)  # lost race / already consumed
    return ok({"refreshToken": item["refreshToken"]}, 200)
```
Add `_qr_redeem_rate_ok(event)` — a source-based per-minute limiter using the same table's `RATE#redeem#{minute}#{src}` counter, where `src` = `event.requestContext.identity.sourceIp` (falls open on error). (Reuse the `_qr_rate_ok` structure.)

- [ ] **Step 4: Wire dispatch BEFORE the caller-guard**

In `lambda_handler`/`dispatch`, add the redeem route **before** `caller = users.get_user_by_sub(...)` and the `caller is None → 403` guard:
```python
    # PUBLIC route — runs before the caller-guard (terminal has no session yet).
    if route == "/auth/qr/redeem" and method == "POST":
        return redeem_qr_login_code(event)
```
Place this at the top of `dispatch` (right after `route`/`method` are computed, before caller resolution). Confirm `route` is the `/api/org`-stripped path (so `/auth/qr/redeem`).

- [ ] **Step 5: Run, confirm pass**

Run `-k qr_redeem` → PASS (3 tests, single-use proven). Then full `tests/unit` → no regressions.

- [ ] **Step 6: Commit**
```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(auth): public POST /auth/qr/redeem returns the stashed refresh token (single-use)"
```

---

### Task 3: SAM template — public redeem route + remove trigger infra

**Files:** Modify `src/template.yaml`.

- [ ] **Step 1: Add the public redeem Api event to OrgApiFunction**

In `OrgApiFunction.Properties.Events` (alongside the existing `Type: Api` `/api/org/{proxy+}` event), add:
```yaml
        QrRedeem:
          Type: Api
          Properties:
            RestApiId: !Ref FieldSightApi
            Path: /api/org/auth/qr/redeem
            Method: post
            Auth:
              Authorizer: NONE
```
> `Authorizer: NONE` overrides the API's default Cognito authorizer for THIS route only (SAM). An explicit path takes precedence over `{proxy+}`. Confirm the API's `Auth.DefaultAuthorizer` name in the `FieldSightApi` definition so `NONE` is the correct override token for this SAM version.

- [ ] **Step 2: Remove the CUSTOM_AUTH trigger resources**

Delete from `Resources`: `QrAuthDefineFunction`, `QrAuthCreateFunction`, `QrAuthVerifyFunction`, `QrAuthDefinePermission`, `QrAuthCreatePermission`, `QrAuthVerifyPermission`. Delete from `Outputs`: `QrAuthDefineFunctionArn`, `QrAuthCreateFunctionArn`, `QrAuthVerifyFunctionArn`. **KEEP** `QrLoginCodesTable` (Condition IsProdWithOrgApi) and OrgApiFunction's `QR_CODES_TABLE` env + the Put/Update `!If [IsProdWithOrgApi,...]` policy (redeem/create both need the table). The `IsProd`/`IsProdWithOrgApi` conditions stay (still used by the table + env + policy).

- [ ] **Step 3: Validate**

`sam` is unavailable on this box — run the PyYAML CFN-tags-ignored well-formedness check (see the v1 backend plan's Task 3 snippet, `PYTHONUTF8=1`), asserting: `QrLoginCodesTable` present + gated `IsProdWithOrgApi`; the 3 `QrAuth*Function`/`*Permission`/`*FunctionArn` are GONE; OrgApiFunction has the `QrRedeem` event.

- [ ] **Step 4: Commit**
```bash
git add src/template.yaml
git commit -m "infra(auth): public redeem route on org-api; drop CUSTOM_AUTH triggers (keep code table)"
```

---

### Task 4: Delete the dead CUSTOM_AUTH module

**Files:** Delete `src/lambda_qr_auth.py`, `tests/unit/test_lambda_qr_auth.py`.

- [ ] **Step 1:** `git rm src/lambda_qr_auth.py tests/unit/test_lambda_qr_auth.py`
- [ ] **Step 2:** Run full `tests/unit` → still green (nothing imports the deleted module).
- [ ] **Step 3:** Commit `chore(auth): remove unused Cognito custom-auth triggers (superseded by refresh handoff)`.

---

### Task 5: Deploy + unwind the pool (USER-RUN gated steps)

> Permission-gated / shared-prod — the operator runs these with the `!` prefix or after granting Bash permission rules. Uses the same discipline as v1.

- [ ] **Step 1:** Merge `feat/qr-login-v2-backend` → develop (test deploy; redeem route deploys, QR table/env prod-gated no-op on test). Verify deploy.yml green.
- [ ] **Step 2:** For prod: `git checkout -b hotfix/qr-login-v2 origin/main`, cherry-pick the v2 QR commits (Tasks 1–4), resolve additively, PR → main, approve deploy-prod. (Deploy-role IAM for the table was already granted for v1 — still valid; redeem adds no new resource type.)
- [ ] **Step 3: [operator] Unwind the shared pool `LambdaConfig` → `{}`** (describe→merge→update, preserve ALL other fields; the exact reverse of the v1 wiring — rollback capture already at `deploy-backup/pool.json`). Verify `describe-user-pool` shows `LambdaConfig: {}` and everything else intact; smoke-test password login.
- [ ] **Step 4: [operator] End-to-end rehearsal via CLI:** put an item with a real refresh token → `curl POST .../api/org/auth/qr/redeem {"code":...}` → returns the token → `initiate-auth REFRESH_TOKEN_AUTH --auth-parameters REFRESH_TOKEN=<it>` → returns Id/Access tokens. Re-run redeem → 401 (single-use).

---

## Self-Review
**Spec coverage:** §4.1 create-stores-token → Task 1. §4.2 redeem endpoint (public, pre-guard, single-use, generic) → Task 2 + Task 3 Step 1 (gateway). §4.3 remove triggers + keep table → Tasks 3–4; pool unwind → Task 5.3. §4.4 record shape → Tasks 1–2. §7 security (no logging, single-use, generic) → Tasks 1–2.
**Placeholder scan:** the `_qr_redeem_rate_ok` body reuses the existing `_qr_rate_ok` structure (spec §11 open item: source keying via `sourceIp`) — the exact counter code mirrors the in-tree `_qr_rate_ok`; implementer copies its shape.
**Type consistency:** record `{code, refreshToken, sub, consumed, createdAt, expiresAt}` identical across create (writes) + redeem (reads refreshToken/consumed/expiresAt). `redeem_qr_login_code(event)` takes only `event` (pre-auth). Route strings `/auth/qr/create` + `/auth/qr/redeem` both `/api/org`-stripped.
