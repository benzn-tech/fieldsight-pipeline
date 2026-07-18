# Site Voice — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-hosted, site-scoped real-time voice-message channel to the FieldSight SAM backend — API Gateway WebSocket + a JWKS Lambda authorizer + in-VPC connect/disconnect/sendVoice + a non-VPC fanout + a reaper, backed by two additive Aurora tables (`ws_connections`, `voice_messages`), plus a dedicated off-the-record `voice/` upload/backfill path in `lambda_org_api`.

**Architecture:** The device holds a persistent WebSocket; `$connect` carries a Cognito idToken in the handshake `Authorization` header, verified by a REQUEST Lambda authorizer (JWKS/RS256) because API Gateway WebSocket has no `COGNITO_USER_POOLS` support. `sendVoice` (in-VPC) checks membership, writes a metadata-only `voice_messages` pointer, resolves online recipients (`ws_connections ⨝ memberships`), and async-invokes a non-VPC `voice-fanout` that POSTs over `@connections` (the VPC/non-VPC split avoids BUG-36 and a ~$20/mo `execute-api` endpoint). Voice audio lives under a dedicated `voice/` S3 prefix that matches no event trigger, so it never enters transcribe/report/RAG.

**Tech Stack:** Python 3.11, psycopg3, AWS SAM (CloudFormation), Aurora PostgreSQL 16, API Gateway WebSocket (`AWS::ApiGatewayV2::*`), Lambda, PyJWT[crypto] (JWKS), boto3 `apigatewaymanagementapi`, pytest (+ `pgvector/pgvector:pg16` service container), region ap-southeast-2, account 509194952652.

## Global Constraints
- English-only dev artifacts (comments/commits/docs) per the 2026-07-15 rule.
- In-VPC Lambdas reuse the EXACT `VpcConfig` (`DbSubnetIds` + `!ImportValue ${DbStackName}-LambdaSG`) + deploy-time `PGHOST/PGDATABASE/PGUSER/PGPASSWORD` env (BUG-36 — the PGPASSWORD is a CloudFormation `{{resolve:secretsmanager:...}}` deploy-time reference, NEVER a runtime Secrets Manager call).
- No DynamoDB (stale-connection reaping replaces DynamoDB TTL).
- Voice content must never enter transcribe/report/RAG: dedicated `voice/` S3 prefix (matches no `wire-s3-events.sh` trigger — BUG-13), a dedicated `voice_messages` table (NO transcript column), never the `recordings` table / `create_recording_upload_url`.
- test + prod share ONE Aurora cluster (`fieldsight-db-test`) + ONE `schema_migrations` ledger; the tables land at test-deploy time, the prod migrate invoke is a no-op.
- Migrations applied via `aws lambda invoke fieldsight-{test,prod}-migrate` AFTER `sam deploy` (idempotent).
- Region ap-southeast-2; account 509194952652.
- Unit tests are DB-free (monkeypatch); integration tests require `TEST_DATABASE_URL` and run under the `test.yml` harness (pgvector/pgvector:pg16 service) — the Windows dev host has no local Python (BUG-29), so run tests in CI or a Linux/devcontainer shell.

---

## File Structure

**New files:**
- `src/migrations/0016_site_voice.sql` — creates `ws_connections` + `voice_messages` (0015 is already taken by `0015_platform_company.sql`).
- `src/repositories/ws_connections.py` — CRUD + reap + recipient resolution for live WebSocket connections.
- `src/repositories/voice_messages.py` — insert / backfill-since / retention-prune for the metadata-only voice pointer rows.
- `src/lambda_ws_authorizer.py` — non-VPC REQUEST authorizer: verify Cognito idToken via JWKS (RS256/exp/iss/token_use), return IAM Allow + `context{sub}`.
- `src/lambda_ws_connect.py` — in-VPC `$connect`: resolve sub → upsert `ws_connections`.
- `src/lambda_ws_disconnect.py` — in-VPC `$disconnect`: delete the connection row.
- `src/lambda_ws_send_voice.py` — in-VPC `sendVoice`: ACL + insert pointer + resolve recipients + async-invoke fanout.
- `src/lambda_voice_fanout.py` — non-VPC: POST payload over `@connections`; `GoneException` → async-invoke reaper.
- `src/lambda_voice_reaper.py` — in-VPC: targeted delete (fanout) + scheduled sweep (stale connections + 30-day `voice_messages` prune).
- `infra/jwt-layer/requirements.txt` — `PyJWT[crypto]` Lambda layer for the authorizer.
- `scripts/voice-ws-smoke.sh` — end-to-end verification (authorizer/upload-url/sendVoice/fanout/backfill/reap) on fieldsight-test.
- `tests/integration/test_ws_connections_repo.py`, `tests/integration/test_voice_messages_repo.py` — repo integration tests.
- `tests/unit/test_lambda_ws_authorizer.py`, `tests/unit/test_lambda_ws_connect.py`, `tests/unit/test_lambda_ws_send_voice.py`, `tests/unit/test_lambda_voice_fanout.py`, `tests/unit/test_lambda_voice_reaper.py`, `tests/unit/test_voice_api.py` — handler unit tests.

**Modified files:**
- `src/lambda_org_api.py` — add `voice_messages` import + 3 routes/handlers (`POST /voice/upload-url`, `GET /voice/asset-url`, `GET /sites/{id}/voice`) + `_voice_s3_key`.
- `src/template.yaml` — `EnableSiteVoice` param, `HasSiteVoice` condition, `JwtLayer`, 6 new functions, WS API (`AWS::ApiGatewayV2::*`) + permissions, org-api `voice/*` S3 grant + `VOICE_PREFIX` env, `VoiceWsEndpoint` output.
- `.github/workflows/deploy.yml` — pass `EnableSiteVoice=true` (test); add voice smoke step.
- `.github/workflows/deploy-prod.yml` — pass `EnableSiteVoice=${{ vars.PROD_ENABLE_SITE_VOICE || 'false' }}`.
- `.github/workflows/test.yml` — add `PyJWT[crypto]` to the pip install.
- `scripts/wire-bucket-lifecycle.sh` — add a 30-day `voice/` expiry rule + guard.

---

## Task 1 — Migration `0016_site_voice.sql` (ws_connections + voice_messages)

**Files:**
- Create `src/migrations/0016_site_voice.sql`
- Create `tests/integration/test_ws_connections_repo.py` (initial smoke asserting the tables exist; grows in Task 2)

**Interfaces:**
- Consumes: `db.migrate.apply_migrations` (existing runner; picks up `*.sql` by filename, orders by integer prefix).
- Produces: table `ws_connections(connection_id text PK, user_id uuid NOT NULL, company_id uuid NOT NULL, connected_at timestamptz default now())`; table `voice_messages(id uuid PK default gen_random_uuid(), company_id uuid NOT NULL, site_id uuid NOT NULL, sender_user_id uuid NOT NULL, s3_key text NOT NULL, duration_s numeric, created_at timestamptz default now())`; indexes `idx_ws_connections_user`, `idx_ws_connections_connected`, `idx_voice_messages_site_created`.

> NOTE (grounded): the spec text says "migration 0015" but `src/migrations/0015_platform_company.sql` already exists — so this is `0016`. NOT-NULL is added on the always-present columns (a hardening over the bare spec DDL; `duration_s` stays nullable). No foreign keys (matches the spec's minimal DDL and `voice_ask_log`'s FK-free precedent), so the migration is purely additive with no ordering coupling.

Steps:
- [ ] Write the failing integration test `tests/integration/test_ws_connections_repo.py`:
```python
import pytest

pytestmark = pytest.mark.integration


def test_site_voice_tables_exist(db):
    # Both tables are created by 0016; a bare INSERT/SELECT proves the schema.
    db.execute(
        "INSERT INTO ws_connections (connection_id, user_id, company_id) "
        "VALUES ('c-smoke', gen_random_uuid(), gen_random_uuid())")
    assert db.execute(
        "SELECT count(*) FROM ws_connections WHERE connection_id='c-smoke'"
    ).fetchone()[0] == 1
    row = db.execute(
        "INSERT INTO voice_messages (company_id, site_id, sender_user_id, s3_key) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'voice/x.wav') "
        "RETURNING id, duration_s, created_at").fetchone()
    assert row[0] is not None and row[1] is None and row[2] is not None
```
- [ ] Run it, expect FAIL (`psycopg.errors.UndefinedTable: relation "ws_connections" does not exist`):
  `python -m pytest tests/integration/test_ws_connections_repo.py -v`
- [ ] Create `src/migrations/0016_site_voice.sql`:
```sql
-- 0016: site voice — realtime push-to-talk voice messages scoped to a site.
-- Two additive tables, no existing readers (safe on the shared cluster):
--   ws_connections  — one row per live API Gateway WebSocket connection.
--   voice_messages  — metadata-only delivery pointer. NO transcript / content
--                     column: Site voice is off-the-record (data-isolation
--                     invariant — never enters transcribe/report/RAG).
-- spec: docs/superpowers/specs/2026-07-18-site-voice-design.md
CREATE TABLE ws_connections (
  connection_id text PRIMARY KEY,
  user_id       uuid NOT NULL,
  company_id    uuid NOT NULL,
  connected_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ws_connections_user ON ws_connections (user_id);
CREATE INDEX idx_ws_connections_connected ON ws_connections (connected_at);

CREATE TABLE voice_messages (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id     uuid NOT NULL,
  site_id        uuid NOT NULL,
  sender_user_id uuid NOT NULL,
  s3_key         text NOT NULL,
  duration_s     numeric,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_voice_messages_site_created
  ON voice_messages (company_id, site_id, created_at DESC);
```
- [ ] Run it, expect PASS: `python -m pytest tests/integration/test_ws_connections_repo.py -v`
- [ ] Commit:
  `git add src/migrations/0016_site_voice.sql tests/integration/test_ws_connections_repo.py`
  `git commit -m "Add 0016_site_voice migration: ws_connections + voice_messages tables"`

---

## Task 2 — `repositories/ws_connections.py`

**Files:**
- Create `src/repositories/ws_connections.py`
- Modify `tests/integration/test_ws_connections_repo.py` (add repo tests)

**Interfaces:**
- Consumes: `repositories.companies.create_company`, `repositories.users.upsert_user`, `repositories.sites.create_site`, `repositories.memberships.add_membership` (seeding); `psycopg.rows.dict_row`.
- Produces:
  - `upsert_connection(conn, connection_id: str, user_id, company_id) -> None`
  - `delete_connection(conn, connection_id: str) -> None`
  - `delete_connections(conn, connection_ids: list[str]) -> int`
  - `delete_stale(conn, older_than: datetime) -> int`
  - `recipients_for_site(conn, company_id, site_id, exclude_user_id) -> list[str]` (connection ids of online site members minus the sender)

Steps:
- [ ] Add failing repo tests to `tests/integration/test_ws_connections_repo.py`:
```python
from datetime import datetime, timedelta, timezone

from repositories import companies, users, sites, memberships, ws_connections


def _seed(db):
    co = companies.create_company(db, "Acme", industry="construction")
    a = users.upsert_user(db, "sub-a", "a@acme.com", company_id=co["id"])
    b = users.upsert_user(db, "sub-b", "b@acme.com", company_id=co["id"])
    s = sites.create_site(db, co["id"], "North Wharf")
    memberships.add_membership(db, a["id"], s["id"], "worker")
    memberships.add_membership(db, b["id"], s["id"], "worker")
    return co, a, b, s


def test_upsert_is_idempotent_on_connection_id(db):
    co, a, b, s = _seed(db)
    ws_connections.upsert_connection(db, "conn-1", a["id"], co["id"])
    ws_connections.upsert_connection(db, "conn-1", a["id"], co["id"])
    n = db.execute("SELECT count(*) FROM ws_connections WHERE connection_id='conn-1'").fetchone()[0]
    assert n == 1


def test_recipients_for_site_excludes_sender_and_offline(db):
    co, a, b, s = _seed(db)
    ws_connections.upsert_connection(db, "conn-a", a["id"], co["id"])
    ws_connections.upsert_connection(db, "conn-b", b["id"], co["id"])
    got = ws_connections.recipients_for_site(db, co["id"], s["id"], a["id"])
    assert got == ["conn-b"]                      # sender excluded; b online
    # A non-member (no membership row) is never a recipient even if connected.
    c = users.upsert_user(db, "sub-c", "c@acme.com", company_id=co["id"])
    ws_connections.upsert_connection(db, "conn-c", c["id"], co["id"])
    assert set(ws_connections.recipients_for_site(db, co["id"], s["id"], a["id"])) == {"conn-b"}


def test_recipients_cross_company_isolated(db):
    co, a, b, s = _seed(db)
    other = companies.create_company(db, "Other")
    ws_connections.upsert_connection(db, "conn-b", b["id"], co["id"])
    # Same site id, but querying under a different company returns nothing.
    assert ws_connections.recipients_for_site(db, other["id"], s["id"], a["id"]) == []


def test_delete_connection_and_bulk_and_stale(db):
    co, a, b, s = _seed(db)
    ws_connections.upsert_connection(db, "conn-a", a["id"], co["id"])
    ws_connections.upsert_connection(db, "conn-b", b["id"], co["id"])
    ws_connections.delete_connection(db, "conn-a")
    assert ws_connections.recipients_for_site(db, co["id"], s["id"], a["id"]) == ["conn-b"]
    assert ws_connections.delete_connections(db, ["conn-b", "missing"]) == 1
    assert ws_connections.delete_connections(db, []) == 0
    ws_connections.upsert_connection(db, "conn-old", a["id"], co["id"])
    db.execute("UPDATE ws_connections SET connected_at = now() - interval '48 hours' WHERE connection_id='conn-old'")
    assert ws_connections.delete_stale(db, datetime.now(timezone.utc) - timedelta(hours=24)) == 1
```
- [ ] Run, expect FAIL (`ModuleNotFoundError: No module named 'repositories.ws_connections'`):
  `python -m pytest tests/integration/test_ws_connections_repo.py -v`
- [ ] Create `src/repositories/ws_connections.py`:
```python
"""Live WebSocket connection registry (Site voice). The caller owns the
transaction (see db.connection.get_connection) — these NEVER commit."""
from psycopg.rows import dict_row


def upsert_connection(conn, connection_id, user_id, company_id) -> None:
    """Register (or refresh) a live connection. Idempotent on connection_id —
    API Gateway ids are unique per connection, so this only ever refreshes."""
    conn.cursor().execute(
        "INSERT INTO ws_connections (connection_id, user_id, company_id) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (connection_id) DO UPDATE SET "
        "user_id=EXCLUDED.user_id, company_id=EXCLUDED.company_id, connected_at=now()",
        (connection_id, user_id, company_id),
    )
    return None


def delete_connection(conn, connection_id) -> None:
    conn.cursor().execute(
        "DELETE FROM ws_connections WHERE connection_id=%s", (connection_id,))
    return None


def delete_connections(conn, connection_ids) -> int:
    """Bulk-delete gone connections (fanout GoneException reap). Empty in ->
    0 out, no round-trip."""
    if not connection_ids:
        return 0
    return conn.cursor().execute(
        "DELETE FROM ws_connections WHERE connection_id = ANY(%s)",
        (list(connection_ids),),
    ).rowcount


def delete_stale(conn, older_than) -> int:
    """Scheduled sweep: drop connections whose connected_at is older than the
    cutoff — a dead connection that never fired $disconnect. older_than is a
    timezone-aware datetime."""
    return conn.cursor().execute(
        "DELETE FROM ws_connections WHERE connected_at < %s", (older_than,)
    ).rowcount


def recipients_for_site(conn, company_id, site_id, exclude_user_id) -> list[str]:
    """Connection ids of every ONLINE member of site_id EXCEPT the sender.
    Joins live connections to non-archived memberships on the site; company-
    pinned on the connection row (multi-tenant invariant). DISTINCT guards
    against duplicate join rows; each of a member's devices is its own
    connection_id, so all their devices receive the message."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT DISTINCT wc.connection_id "
        "FROM ws_connections wc "
        "JOIN memberships m ON m.user_id = wc.user_id "
        "WHERE m.site_id = %s::uuid AND wc.company_id = %s "
        "AND wc.user_id <> %s AND m.archived_at IS NULL",
        (site_id, company_id, exclude_user_id),
    ).fetchall()
    return [r["connection_id"] for r in rows]
```
- [ ] Run, expect PASS: `python -m pytest tests/integration/test_ws_connections_repo.py -v`
- [ ] Commit:
  `git add src/repositories/ws_connections.py tests/integration/test_ws_connections_repo.py`
  `git commit -m "Add ws_connections repository (upsert/delete/reap/recipients)"`

---

## Task 3 — `repositories/voice_messages.py`

**Files:**
- Create `src/repositories/voice_messages.py`
- Create `tests/integration/test_voice_messages_repo.py`

**Interfaces:**
- Consumes: `repositories.companies/users/sites` (seeding); `psycopg.rows.dict_row`.
- Produces:
  - `insert_message(conn, company_id, site_id, sender_user_id, s3_key: str, duration_s=None) -> dict` (cols: `id, company_id, site_id, sender_user_id, s3_key, duration_s, created_at`)
  - `list_since(conn, company_id, site_id, since) -> list[dict]` (created_at > since, chronological)
  - `prune_older_than(conn, cutoff) -> int`

Steps:
- [ ] Write failing `tests/integration/test_voice_messages_repo.py`:
```python
from datetime import datetime, timedelta, timezone

import pytest
from repositories import companies, users, sites, voice_messages

pytestmark = pytest.mark.integration


def _seed(db):
    co = companies.create_company(db, "Acme")
    u = users.upsert_user(db, "sub-v", "v@acme.com", company_id=co["id"])
    s = sites.create_site(db, co["id"], "Wharf")
    return co, u, s


def test_insert_and_list_since(db):
    co, u, s = _seed(db)
    m1 = voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/a.wav", duration_s=1.5)
    assert m1["s3_key"] == "voice/a.wav" and float(m1["duration_s"]) == 1.5
    voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/b.wav")
    # since before both -> both, chronological
    all_msgs = voice_messages.list_since(db, co["id"], s["id"], "1970-01-01T00:00:00Z")
    assert [m["s3_key"] for m in all_msgs] == ["voice/a.wav", "voice/b.wav"]
    # since after m1 -> only b
    after = voice_messages.list_since(db, co["id"], s["id"], m1["created_at"])
    assert [m["s3_key"] for m in after] == ["voice/b.wav"]


def test_list_since_company_and_site_scoped(db):
    co, u, s = _seed(db)
    other = companies.create_company(db, "Other")
    voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/a.wav")
    assert voice_messages.list_since(db, other["id"], s["id"], "1970-01-01T00:00:00Z") == []


def test_prune_older_than(db):
    co, u, s = _seed(db)
    m = voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/old.wav")
    db.execute("UPDATE voice_messages SET created_at = now() - interval '40 days' WHERE id=%s", (m["id"],))
    voice_messages.insert_message(db, co["id"], s["id"], u["id"], "voice/new.wav")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    assert voice_messages.prune_older_than(db, cutoff) == 1
    remaining = voice_messages.list_since(db, co["id"], s["id"], "1970-01-01T00:00:00Z")
    assert [m["s3_key"] for m in remaining] == ["voice/new.wav"]
```
- [ ] Run, expect FAIL (`ModuleNotFoundError: No module named 'repositories.voice_messages'`):
  `python -m pytest tests/integration/test_voice_messages_repo.py -v`
- [ ] Create `src/repositories/voice_messages.py`:
```python
"""voice_messages writes/reads (Site voice delivery pointer). Metadata only —
NO transcript/content column (off-the-record invariant). The caller owns the
transaction (see db.connection.get_connection) — these NEVER commit."""
from psycopg.rows import dict_row

_COLS = "id, company_id, site_id, sender_user_id, s3_key, duration_s, created_at"


def insert_message(conn, company_id, site_id, sender_user_id, s3_key,
                   duration_s=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO voice_messages (company_id, site_id, sender_user_id, s3_key, duration_s) "
        f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, site_id, sender_user_id, s3_key, duration_s),
    ).fetchone()


def list_since(conn, company_id, site_id, since) -> list[dict]:
    """Recent messages for reconnect backfill: everything on this site created
    strictly after `since` (ISO string or datetime). Company- and site-pinned;
    chronological (oldest first) for ordered replay."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM voice_messages "
        f"WHERE company_id=%s AND site_id=%s::uuid AND created_at > %s "
        f"ORDER BY created_at",
        (company_id, site_id, since),
    ).fetchall()


def prune_older_than(conn, cutoff) -> int:
    """Scheduled retention prune: drop rows older than cutoff (30-day parity
    with the voice/ S3 lifecycle). cutoff is a tz-aware datetime."""
    return conn.cursor().execute(
        "DELETE FROM voice_messages WHERE created_at < %s", (cutoff,)
    ).rowcount
```
- [ ] Run, expect PASS: `python -m pytest tests/integration/test_voice_messages_repo.py -v`
- [ ] Commit:
  `git add src/repositories/voice_messages.py tests/integration/test_voice_messages_repo.py`
  `git commit -m "Add voice_messages repository (insert/list_since/prune)"`

---

## Task 4 — WS Lambda authorizer (`lambda_ws_authorizer.py`) + JWT layer

**Files:**
- Create `infra/jwt-layer/requirements.txt`
- Create `src/lambda_ws_authorizer.py`
- Create `tests/unit/test_lambda_ws_authorizer.py`

**Interfaces:**
- Consumes: env `WS_USER_POOL_IDS` (comma-separated Cognito pool ids to trust — wired to `!Ref OrgUserPoolId` in Task 10), `AWS_REGION` (Lambda-provided); `jwt` (PyJWT[crypto]) `PyJWKClient`/`decode`.
- Produces: `lambda_ws_authorizer.lambda_handler(event, context) -> dict` returning `{principalId, policyDocument, context:{sub}}`; raises `Exception("Unauthorized")` (→ 401) on any failure. Module attrs monkeypatched by tests: `POOL_IDS`, `_jwks_client`, `jwt`.

> DECISION (grounded): use PyJWT[crypto] carried by a `sam build` LayerVersion, exactly like `infra/psycopg-layer` (psycopg[binary]) and `infra/dashscope-layer` — the CI runner produces manylinux x86_64 py3.11 wheels compatible with the Lambda runtime, so cryptography's native bits are fine. The authorizer is NON-VPC so it can fetch JWKS over the internet (BUG-36: an in-VPC fn has no egress). Verification = RS256 signature (via the pool's JWKS) + `exp` + `iss` + `token_use == "id"`; `aud` is NOT pinned (mirrors the REST `COGNITO_USER_POOLS` authorizer, which trusts any client of the pool).

Steps:
- [ ] Create `infra/jwt-layer/requirements.txt`:
```
# PyJWT + cryptography (RS256) for the Site Voice WebSocket authorizer.
# jwt.PyJWKClient fetches the Cognito pool JWKS; cryptography verifies RS256.
# Built by `sam build` (Metadata BuildMethod python3.11) into manylinux
# wheels compatible with the Lambda python3.11 x86_64 runtime — same pattern
# as infra/psycopg-layer and infra/dashscope-layer.
PyJWT[crypto]>=2.8
```
- [ ] Write failing `tests/unit/test_lambda_ws_authorizer.py`:
```python
import pytest

wsauth = pytest.importorskip("lambda_ws_authorizer", reason="requires PyJWT")


class _FakeSigningKey:
    key = "fake-public-key"


class _FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey()


def _event(headers=None, method_arn="arn:aws:execute-api:ap-southeast-2:509194952652:abc/prod/$connect"):
    return {"headers": headers or {}, "methodArn": method_arn}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(wsauth, "POOL_IDS", ["ap-southeast-2_q88pd6XXr"])
    monkeypatch.setattr(wsauth, "_jwks_client", lambda pool_id: _FakeJwks())
    return monkeypatch


def test_valid_id_token_allows_with_sub(wired):
    wired.setattr(wsauth.jwt, "decode",
                  lambda *a, **k: {"sub": "user-123", "token_use": "id"})
    res = wsauth.lambda_handler(_event({"Authorization": "goodtoken"}), None)
    assert res["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert res["context"]["sub"] == "user-123"
    assert res["policyDocument"]["Statement"][0]["Resource"].endswith("$connect")


def test_case_insensitive_header_and_bearer_prefix(wired):
    seen = {}
    def fake_decode(token, *a, **k):
        seen["token"] = token
        return {"sub": "u", "token_use": "id"}
    wired.setattr(wsauth.jwt, "decode", fake_decode)
    wsauth.lambda_handler(_event({"authorization": "Bearer tok"}), None)
    assert seen["token"] == "tok"       # bearer prefix stripped, lowercase header found


def test_access_token_rejected(wired):
    # token_use != "id" must not authorize.
    wired.setattr(wsauth.jwt, "decode",
                  lambda *a, **k: {"sub": "u", "token_use": "access"})
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({"Authorization": "t"}), None)


def test_bad_signature_rejected(wired):
    def boom(*a, **k):
        raise ValueError("signature verification failed")
    wired.setattr(wsauth.jwt, "decode", boom)
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({"Authorization": "t"}), None)


def test_missing_header_rejected(wired):
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({}), None)
```
- [ ] Run, expect FAIL (`ModuleNotFoundError`/import skip if PyJWT missing; after Task 12 adds PyJWT to CI it collects and fails on the missing module):
  `python -m pytest tests/unit/test_lambda_ws_authorizer.py -v`
- [ ] Create `src/lambda_ws_authorizer.py`:
```python
"""
Non-VPC Lambda: REQUEST authorizer for the Site Voice WebSocket API.

API Gateway WebSocket has no COGNITO_USER_POOLS authorizer (unlike the REST
API's CognitoAuthorizer), so the Cognito idToken is verified here in code:
RS256 signature via the pool's JWKS, plus exp / issuer / token_use=id. The
idToken rides in the $connect handshake `Authorization` header. On success we
return an IAM Allow policy + context {sub}; ws-connect (in-VPC) resolves the
sub to the user/company and upserts ws_connections. Non-VPC so JWKS can be
fetched over the internet (BUG-36: an in-VPC fn has no egress).

Env: WS_USER_POOL_IDS = comma-separated Cognito pool ids to trust (the same
pool(s) the REST CognitoAuthorizer trusts — OrgUserPoolId).
"""
import os

import jwt  # PyJWT (jwt-layer); PyJWT[crypto] pulls cryptography for RS256

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
POOL_IDS = [p for p in os.environ.get("WS_USER_POOL_IDS", "").split(",") if p]

# One PyJWKClient per pool, cached across warm invokes (each caches the fetched
# signing keys internally, so steady state does zero JWKS fetches).
_jwks_clients = {}


def _jwks_client(pool_id):
    client = _jwks_clients.get(pool_id)
    if client is None:
        url = f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/jwks.json"
        client = jwt.PyJWKClient(url)
        _jwks_clients[pool_id] = client
    return client


def _bearer(headers):
    # Handshake header is case-insensitive; scan case-folded. Tolerate an
    # optional "Bearer " prefix (mobile sends the raw token).
    for k, v in (headers or {}).items():
        if k.lower() == "authorization" and v:
            return v[7:] if v.lower().startswith("bearer ") else v
    return None


def _verify(token):
    """Return the token's claims if it validates against ANY trusted pool,
    else raise. Tries each pool's issuer + JWKS; first success wins."""
    for pool_id in POOL_IDS:
        issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}"
        try:
            signing_key = _jwks_client(pool_id).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, signing_key.key, algorithms=["RS256"],
                issuer=issuer, options={"verify_aud": False})
        except Exception:
            continue
        if claims.get("token_use") == "id" and claims.get("sub"):
            return claims
    raise ValueError("token did not validate against any trusted pool")


def _policy(effect, resource, sub):
    return {
        "principalId": sub or "unknown",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke",
                           "Effect": effect, "Resource": resource}],
        },
        "context": {"sub": sub} if sub else {},
    }


def lambda_handler(event, context):
    token = _bearer(event.get("headers"))
    method_arn = event.get("methodArn", "*")
    if not token:
        raise Exception("Unauthorized")   # API Gateway maps this to 401
    try:
        claims = _verify(token)
    except Exception:
        raise Exception("Unauthorized")
    return _policy("Allow", method_arn, claims["sub"])
```
- [ ] Run, expect PASS: `python -m pytest tests/unit/test_lambda_ws_authorizer.py -v`
- [ ] Commit:
  `git add infra/jwt-layer/requirements.txt src/lambda_ws_authorizer.py tests/unit/test_lambda_ws_authorizer.py`
  `git commit -m "Add Site Voice WebSocket JWKS authorizer + jwt-layer"`

---

## Task 5 — `ws-connect` + `ws-disconnect` Lambdas (in-VPC)

**Files:**
- Create `src/lambda_ws_connect.py`
- Create `src/lambda_ws_disconnect.py`
- Create `tests/unit/test_lambda_ws_connect.py`

**Interfaces:**
- Consumes: `db.connection.get_connection`; `repositories.users.get_user_by_sub`; `repositories.ws_connections.upsert_connection`/`delete_connection`. Reads `event.requestContext.connectionId` + `event.requestContext.authorizer.sub` (set by Task 4).
- Produces: `lambda_ws_connect.lambda_handler(event, context) -> {"statusCode": int}` (200 on register, 403 unprovisioned, 401 no sub); `lambda_ws_disconnect.lambda_handler(event, context) -> {"statusCode": 200}` (best-effort delete).

Steps:
- [ ] Write failing `tests/unit/test_lambda_ws_connect.py`:
```python
import pytest

conn_mod = pytest.importorskip("lambda_ws_connect", reason="requires psycopg import path")
disc_mod = pytest.importorskip("lambda_ws_disconnect", reason="requires psycopg import path")


class _FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _event(connection_id="conn-1", sub="sub-1"):
    return {"requestContext": {"connectionId": connection_id,
                               "authorizer": {"sub": sub} if sub else {}}}


def test_connect_upserts_for_provisioned_user(monkeypatch):
    monkeypatch.setattr(conn_mod, "get_connection", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(conn_mod.users, "get_user_by_sub",
                        lambda c, sub: {"id": "u-1", "company_id": "c-1"})
    captured = {}
    monkeypatch.setattr(conn_mod.ws_connections, "upsert_connection",
                        lambda c, cid, uid, coid: captured.update(cid=cid, uid=uid, coid=coid))
    res = conn_mod.lambda_handler(_event(), None)
    assert res["statusCode"] == 200
    assert captured == {"cid": "conn-1", "uid": "u-1", "coid": "c-1"}


def test_connect_refuses_unprovisioned(monkeypatch):
    monkeypatch.setattr(conn_mod, "get_connection", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(conn_mod.users, "get_user_by_sub", lambda c, sub: None)
    called = {"upsert": False}
    monkeypatch.setattr(conn_mod.ws_connections, "upsert_connection",
                        lambda *a, **k: called.__setitem__("upsert", True))
    res = conn_mod.lambda_handler(_event(), None)
    assert res["statusCode"] == 403 and called["upsert"] is False


def test_connect_missing_sub_401(monkeypatch):
    monkeypatch.setattr(conn_mod, "get_connection", lambda *a, **k: _FakeConn())
    res = conn_mod.lambda_handler(_event(sub=None), None)
    assert res["statusCode"] == 401


def test_disconnect_deletes_row(monkeypatch):
    monkeypatch.setattr(disc_mod, "get_connection", lambda *a, **k: _FakeConn())
    captured = {}
    monkeypatch.setattr(disc_mod.ws_connections, "delete_connection",
                        lambda c, cid: captured.update(cid=cid))
    res = disc_mod.lambda_handler({"requestContext": {"connectionId": "conn-9"}}, None)
    assert res["statusCode"] == 200 and captured == {"cid": "conn-9"}


def test_disconnect_never_fails(monkeypatch):
    monkeypatch.setattr(disc_mod, "get_connection",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    res = disc_mod.lambda_handler({"requestContext": {"connectionId": "conn-9"}}, None)
    assert res["statusCode"] == 200
```
- [ ] Run, expect FAIL (`ModuleNotFoundError: No module named 'lambda_ws_connect'`):
  `python -m pytest tests/unit/test_lambda_ws_connect.py -v`
- [ ] Create `src/lambda_ws_connect.py`:
```python
"""
In-VPC Lambda: WebSocket $connect for Site Voice. Registers the live
connection in ws_connections, keyed by the authorizer-verified Cognito sub.
Non-provisioned callers are refused (403 → API Gateway rejects the connection).
"""
import logging

from db.connection import get_connection
from repositories import users, ws_connections

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    rc = event.get("requestContext", {}) or {}
    connection_id = rc.get("connectionId")
    sub = (rc.get("authorizer") or {}).get("sub")
    if not connection_id or not sub:
        return {"statusCode": 401}
    try:
        with get_connection() as conn:
            caller = users.get_user_by_sub(conn, sub)
            if caller is None or not caller["company_id"]:
                logger.warning("ws connect refused: sub %s not provisioned", sub)
                return {"statusCode": 403}
            ws_connections.upsert_connection(
                conn, connection_id, caller["id"], caller["company_id"])
        return {"statusCode": 200}
    except Exception:
        logger.exception("ws connect failed")
        return {"statusCode": 500}
```
- [ ] Create `src/lambda_ws_disconnect.py`:
```python
"""In-VPC Lambda: WebSocket $disconnect for Site Voice. Removes the connection
row. Best-effort — a missing row (already reaped) is fine; never fail."""
import logging

from db.connection import get_connection
from repositories import ws_connections

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    connection_id = (event.get("requestContext", {}) or {}).get("connectionId")
    if not connection_id:
        return {"statusCode": 400}
    try:
        with get_connection() as conn:
            ws_connections.delete_connection(conn, connection_id)
    except Exception:
        logger.exception("ws disconnect failed")
    return {"statusCode": 200}
```
- [ ] Run, expect PASS: `python -m pytest tests/unit/test_lambda_ws_connect.py -v`
- [ ] Commit:
  `git add src/lambda_ws_connect.py src/lambda_ws_disconnect.py tests/unit/test_lambda_ws_connect.py`
  `git commit -m "Add in-VPC ws-connect / ws-disconnect handlers"`

---

## Task 6 — `sendVoice` Lambda (in-VPC)

**Files:**
- Create `src/lambda_ws_send_voice.py`
- Create `tests/unit/test_lambda_ws_send_voice.py`

**Interfaces:**
- Consumes: `db.connection.get_connection`; `repositories.users.get_user_by_sub`; `repositories.memberships.accessible_site_ids(conn, user_id, global_role) -> list` (the non-graded `_allowed_site_ids` floor); `repositories.voice_messages.insert_message`; `repositories.ws_connections.recipients_for_site`; env `VOICE_FANOUT_FUNCTION`; boto3 `lambda` client. Reads `event.body` (`{action, siteId, s3Key, durationS}`) + `event.requestContext.{connectionId,authorizer.sub,domainName,stage}`.
- Produces: `lambda_ws_send_voice.lambda_handler(event, context) -> {"statusCode", "body"}`; the async fanout payload `{"endpoint": "https://{domain}/{stage}", "connectionIds": list[str], "payload": {type, messageId, siteId, s3Key, durationS, senderUserId, createdAt}}`.

Steps:
- [ ] Write failing `tests/unit/test_lambda_ws_send_voice.py`:
```python
import json

import pytest

sv = pytest.importorskip("lambda_ws_send_voice", reason="requires psycopg import path")


class _FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeLambda:
    def __init__(self): self.calls = []
    def invoke(self, **kw):
        self.calls.append({**kw, "payload": json.loads(kw["Payload"])})
        return {"StatusCode": 202}


def _event(body, sub="sub-1"):
    return {"body": json.dumps(body),
            "requestContext": {"connectionId": "conn-1", "domainName": "ws.example.com",
                               "stage": "prod", "authorizer": {"sub": sub} if sub else {}}}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(sv, "get_connection", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(sv, "FANOUT_FUNCTION", "fieldsight-test-voice-fanout")
    monkeypatch.setattr(sv.users, "get_user_by_sub",
                        lambda c, sub: {"id": "u-1", "company_id": "c-1", "global_role": "worker"})
    monkeypatch.setattr(sv.memberships, "accessible_site_ids", lambda c, uid, role: ["s-1"])
    monkeypatch.setattr(sv.voice_messages, "insert_message",
                        lambda c, coid, sid, uid, key, duration_s=None: {
                            "id": "m-1", "site_id": sid, "s3_key": key,
                            "duration_s": duration_s, "created_at": "2026-07-18T00:00:00Z"})
    monkeypatch.setattr(sv.ws_connections, "recipients_for_site",
                        lambda c, coid, sid, uid: ["conn-b", "conn-c"])
    fake = _FakeLambda()
    monkeypatch.setattr(sv, "_lambda", lambda: fake)
    return monkeypatch, fake


def test_send_inserts_and_dispatches_fanout(wired):
    mp, fake = wired
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/c-1/s-1/x.wav", "durationS": 1.2}), None)
    assert res["statusCode"] == 200
    assert json.loads(res["body"]) == {"messageId": "m-1", "recipients": 2}
    inv = fake.calls[0]
    assert inv["FunctionName"] == "fieldsight-test-voice-fanout"
    assert inv["InvocationType"] == "Event"
    p = inv["payload"]
    assert p["endpoint"] == "https://ws.example.com/prod"
    assert p["connectionIds"] == ["conn-b", "conn-c"]
    assert p["payload"]["s3Key"] == "voice/c-1/s-1/x.wav" and p["payload"]["messageId"] == "m-1"


def test_non_member_site_403(wired):
    mp, fake = wired
    mp.setattr(sv.memberships, "accessible_site_ids", lambda c, uid, role: ["other-site"])
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/x.wav"}), None)
    assert res["statusCode"] == 403 and fake.calls == []


def test_missing_fields_400(wired):
    mp, fake = wired
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1"}), None)
    assert res["statusCode"] == 400


def test_no_recipients_skips_fanout(wired):
    mp, fake = wired
    mp.setattr(sv.ws_connections, "recipients_for_site", lambda c, coid, sid, uid: [])
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/x.wav"}), None)
    assert res["statusCode"] == 200 and fake.calls == []   # inserted, but nobody online


def test_unprovisioned_caller_403(wired):
    mp, fake = wired
    mp.setattr(sv.users, "get_user_by_sub", lambda c, sub: None)
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/x.wav"}), None)
    assert res["statusCode"] == 403
```
- [ ] Run, expect FAIL (`ModuleNotFoundError: No module named 'lambda_ws_send_voice'`):
  `python -m pytest tests/unit/test_lambda_ws_send_voice.py -v`
- [ ] Create `src/lambda_ws_send_voice.py`:
```python
"""
In-VPC Lambda: WebSocket `sendVoice` route for Site Voice.

Body {siteId, s3Key, durationS}. Verifies the sender is a member of siteId
(same ACL floor as org-api: memberships.accessible_site_ids), records the
delivery pointer (voice_messages), resolves online recipients (connected
members of the site minus the sender) and async-invokes the NON-VPC
voice-fanout Lambda to POST over @connections. BUG-36: an in-VPC fn cannot
reach the execute-api endpoint, so the broadcast is split into that hop.
"""
import json
import logging
import os

import boto3

from db.connection import get_connection
from repositories import memberships, users, voice_messages, ws_connections

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FANOUT_FUNCTION = os.environ.get("VOICE_FANOUT_FUNCTION", "")

_lambda_client = None


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def lambda_handler(event, context):
    rc = event.get("requestContext", {}) or {}
    sub = (rc.get("authorizer") or {}).get("sub")
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"statusCode": 400, "body": "malformed body"}
    site_id = body.get("siteId")
    s3_key = body.get("s3Key")
    duration_s = body.get("durationS")
    if not (sub and site_id and s3_key):
        return {"statusCode": 400, "body": "siteId and s3Key required"}
    try:
        with get_connection() as conn:
            caller = users.get_user_by_sub(conn, sub)
            if caller is None or not caller["company_id"]:
                return {"statusCode": 403, "body": "not provisioned"}
            allowed = {str(x) for x in memberships.accessible_site_ids(
                conn, caller["id"], caller["global_role"])}
            if str(site_id) not in allowed:
                return {"statusCode": 403, "body": "not a member of site"}
            msg = voice_messages.insert_message(
                conn, caller["company_id"], site_id, caller["id"], s3_key,
                duration_s=duration_s)
            recipients = ws_connections.recipients_for_site(
                conn, caller["company_id"], site_id, caller["id"])
        _dispatch_fanout(rc, recipients, msg, caller)
        return {"statusCode": 200, "body": json.dumps(
            {"messageId": str(msg["id"]), "recipients": len(recipients)})}
    except Exception:
        logger.exception("sendVoice failed")
        return {"statusCode": 500}


def _dispatch_fanout(rc, recipients, msg, caller):
    """Async-invoke the non-VPC fanout with the connection list + payload.
    Best-effort: never fail the send if the async hop can't be queued (and
    skip entirely when nobody else is online)."""
    if not recipients or not FANOUT_FUNCTION:
        return
    endpoint = f"https://{rc.get('domainName')}/{rc.get('stage')}"
    payload = {
        "type": "voice",
        "messageId": str(msg["id"]),
        "siteId": str(msg["site_id"]),
        "s3Key": msg["s3_key"],
        "durationS": float(msg["duration_s"]) if msg.get("duration_s") is not None else None,
        "senderUserId": str(caller["id"]),
        "createdAt": str(msg["created_at"]),
    }
    try:
        _lambda().invoke(
            FunctionName=FANOUT_FUNCTION, InvocationType="Event",
            Payload=json.dumps({"endpoint": endpoint,
                                "connectionIds": recipients, "payload": payload}))
    except Exception:
        logger.exception("fanout dispatch failed")
```
- [ ] Run, expect PASS: `python -m pytest tests/unit/test_lambda_ws_send_voice.py -v`
- [ ] Commit:
  `git add src/lambda_ws_send_voice.py tests/unit/test_lambda_ws_send_voice.py`
  `git commit -m "Add in-VPC sendVoice handler (ACL + pointer + fanout dispatch)"`

---

## Task 7 — `voice-fanout` Lambda (non-VPC)

**Files:**
- Create `src/lambda_voice_fanout.py`
- Create `tests/unit/test_lambda_voice_fanout.py`

**Interfaces:**
- Consumes: event `{"endpoint", "connectionIds", "payload"}` (from Task 6); boto3 `apigatewaymanagementapi` client (`post_to_connection`, `exceptions.GoneException`); env `VOICE_REAPER_FUNCTION`; boto3 `lambda`. 
- Produces: `lambda_voice_fanout.lambda_handler(event, context) -> {"sent": int, "gone": int}`; reaper async payload `{"connectionIds": list[str]}`. Module attrs monkeypatched by tests: `boto3`, `_lambda`, `REAPER_FUNCTION`.

> Grounded on the SP-Ask async pattern (`lambda_ask_agent._get_lambda_client().invoke(InvocationType="Event")`) and the non-VPC/in-VPC split (AskAgentFunction non-VPC → VoiceAuditFunction in-VPC). Here fanout (non-VPC) → reaper (in-VPC).

Steps:
- [ ] Write failing `tests/unit/test_lambda_voice_fanout.py`:
```python
import json

import pytest

fo = pytest.importorskip("lambda_voice_fanout", reason="requires boto3 import path")


class _Gone(Exception):
    pass


class _FakeApi:
    class exceptions:
        GoneException = _Gone
    def __init__(self, gone_ids=()):
        self._gone = set(gone_ids)
        self.posted = []
    def post_to_connection(self, ConnectionId=None, Data=None):
        if ConnectionId in self._gone:
            raise _Gone()
        self.posted.append((ConnectionId, json.loads(Data.decode("utf-8"))))


class _FakeLambda:
    def __init__(self): self.calls = []
    def invoke(self, **kw):
        self.calls.append({**kw, "payload": json.loads(kw["Payload"])})


def _wire(monkeypatch, api):
    monkeypatch.setattr(fo.boto3, "client", lambda svc, **kw: api)
    monkeypatch.setattr(fo, "REAPER_FUNCTION", "fieldsight-test-voice-reaper")
    fake_lambda = _FakeLambda()
    monkeypatch.setattr(fo, "_lambda", lambda: fake_lambda)
    return fake_lambda


def test_posts_to_all_connections(monkeypatch):
    api = _FakeApi()
    fake_lambda = _wire(monkeypatch, api)
    res = fo.lambda_handler({"endpoint": "https://ws/prod",
                             "connectionIds": ["a", "b"],
                             "payload": {"s3Key": "voice/x.wav"}}, None)
    assert res == {"sent": 2, "gone": 0}
    assert [c for c, _ in api.posted] == ["a", "b"]
    assert api.posted[0][1]["s3Key"] == "voice/x.wav"
    assert fake_lambda.calls == []   # nothing gone -> no reaper invoke


def test_gone_connections_trigger_reaper(monkeypatch):
    api = _FakeApi(gone_ids=["b"])
    fake_lambda = _wire(monkeypatch, api)
    res = fo.lambda_handler({"endpoint": "https://ws/prod",
                             "connectionIds": ["a", "b", "c"],
                             "payload": {"s3Key": "voice/x.wav"}}, None)
    assert res == {"sent": 2, "gone": 1}
    assert fake_lambda.calls[0]["FunctionName"] == "fieldsight-test-voice-reaper"
    assert fake_lambda.calls[0]["InvocationType"] == "Event"
    assert fake_lambda.calls[0]["payload"] == {"connectionIds": ["b"]}


def test_empty_input_is_noop(monkeypatch):
    api = _FakeApi()
    _wire(monkeypatch, api)
    assert fo.lambda_handler({"endpoint": "https://ws/prod", "connectionIds": []}, None) == {"sent": 0, "gone": 0}
```
- [ ] Run, expect FAIL (`ModuleNotFoundError: No module named 'lambda_voice_fanout'`):
  `python -m pytest tests/unit/test_lambda_voice_fanout.py -v`
- [ ] Create `src/lambda_voice_fanout.py`:
```python
"""
Non-VPC Lambda: broadcast a Site Voice payload to WebSocket connections.

Async-invoked (Event) by the in-VPC sendVoice Lambda with
{endpoint, connectionIds, payload}. POSTs the payload to each connection via
the API Gateway Management API (execute-api:ManageConnections). A
GoneException means the connection is dead — collect those ids and async-invoke
the in-VPC voice-reaper to delete their ws_connections rows (replaces
DynamoDB TTL). This split exists because an in-VPC fn cannot reach the
execute-api endpoint (no NAT / no VPC endpoint — BUG-36).
"""
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REAPER_FUNCTION = os.environ.get("VOICE_REAPER_FUNCTION", "")

_lambda_client = None


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def lambda_handler(event, context):
    endpoint = event.get("endpoint")
    connection_ids = event.get("connectionIds") or []
    payload = event.get("payload") or {}
    if not endpoint or not connection_ids:
        return {"sent": 0, "gone": 0}
    api = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
    data = json.dumps(payload).encode("utf-8")
    sent, gone = 0, []
    for cid in connection_ids:
        try:
            api.post_to_connection(ConnectionId=cid, Data=data)
            sent += 1
        except api.exceptions.GoneException:
            gone.append(cid)
        except ClientError:
            logger.exception("post_to_connection failed for %s", cid)
    if gone:
        _reap(gone)
    return {"sent": sent, "gone": len(gone)}


def _reap(gone):
    if not REAPER_FUNCTION:
        return
    try:
        _lambda().invoke(
            FunctionName=REAPER_FUNCTION, InvocationType="Event",
            Payload=json.dumps({"connectionIds": gone}))
    except Exception:
        logger.exception("reaper dispatch failed")
```
- [ ] Run, expect PASS: `python -m pytest tests/unit/test_lambda_voice_fanout.py -v`
- [ ] Commit:
  `git add src/lambda_voice_fanout.py tests/unit/test_lambda_voice_fanout.py`
  `git commit -m "Add non-VPC voice-fanout (@connections POST + GoneException reap)"`

---

## Task 8 — `voice-reaper` Lambda (in-VPC; targeted + scheduled sweep)

**Files:**
- Create `src/lambda_voice_reaper.py`
- Create `tests/unit/test_lambda_voice_reaper.py`

**Interfaces:**
- Consumes: `db.connection.get_connection`; `repositories.ws_connections.delete_connections`/`delete_stale`; `repositories.voice_messages.prune_older_than`; env `WS_STALE_HOURS` (default 24), `VOICE_RETENTION_DAYS` (default 30).
- Produces: `lambda_voice_reaper.lambda_handler(event, context)`:
  - targeted mode `{"connectionIds": [...]}` → `{"deleted": int}`
  - sweep mode `{"sweep": true}` → `{"swept_connections": int, "pruned_messages": int}`

Steps:
- [ ] Write failing `tests/unit/test_lambda_voice_reaper.py`:
```python
import pytest

rp = pytest.importorskip("lambda_voice_reaper", reason="requires psycopg import path")


class _FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(rp, "get_connection", lambda *a, **k: _FakeConn())
    calls = {}
    monkeypatch.setattr(rp.ws_connections, "delete_connections",
                        lambda c, ids: calls.setdefault("del_ids", ids) or len(ids))
    monkeypatch.setattr(rp.ws_connections, "delete_stale",
                        lambda c, cutoff: calls.setdefault("stale_cutoff", cutoff) or 3)
    monkeypatch.setattr(rp.voice_messages, "prune_older_than",
                        lambda c, cutoff: calls.setdefault("prune_cutoff", cutoff) or 5)
    return monkeypatch, calls


def test_targeted_delete(wired):
    mp, calls = wired
    res = rp.lambda_handler({"connectionIds": ["a", "b"]}, None)
    assert res == {"deleted": 2} and calls["del_ids"] == ["a", "b"]
    assert "stale_cutoff" not in calls   # targeted mode never sweeps


def test_sweep_mode(wired):
    mp, calls = wired
    res = rp.lambda_handler({"sweep": True}, None)
    assert res == {"swept_connections": 3, "pruned_messages": 5}
    assert calls["stale_cutoff"] is not None and calls["prune_cutoff"] is not None


def test_empty_targeted(wired):
    mp, calls = wired
    res = rp.lambda_handler({"connectionIds": []}, None)
    assert res == {"deleted": 0}
```
- [ ] Run, expect FAIL (`ModuleNotFoundError: No module named 'lambda_voice_reaper'`):
  `python -m pytest tests/unit/test_lambda_voice_reaper.py -v`
- [ ] Create `src/lambda_voice_reaper.py`:
```python
"""
In-VPC Lambda: delete stale ws_connections rows (replaces DynamoDB TTL).

Two modes:
  * targeted — {"connectionIds": [...]}: rows for connections a fanout POST
    hit with GoneException.
  * sweep    — {"sweep": true}: scheduled belt-and-braces — drop connections
    older than WS_STALE_HOURS (a dead conn that never fired $disconnect) and
    prune voice_messages older than VOICE_RETENTION_DAYS (S3 lifecycle parity).
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from db.connection import get_connection
from repositories import voice_messages, ws_connections

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STALE_HOURS = int(os.environ.get("WS_STALE_HOURS", "24"))
RETENTION_DAYS = int(os.environ.get("VOICE_RETENTION_DAYS", "30"))


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        if event.get("sweep"):
            swept = ws_connections.delete_stale(
                conn, now - timedelta(hours=STALE_HOURS))
            pruned = voice_messages.prune_older_than(
                conn, now - timedelta(days=RETENTION_DAYS))
            return {"swept_connections": swept, "pruned_messages": pruned}
        ids = event.get("connectionIds") or []
        deleted = ws_connections.delete_connections(conn, ids)
        return {"deleted": deleted}
```
- [ ] Run, expect PASS: `python -m pytest tests/unit/test_lambda_voice_reaper.py -v`
- [ ] Commit:
  `git add src/lambda_voice_reaper.py tests/unit/test_lambda_voice_reaper.py`
  `git commit -m "Add in-VPC voice-reaper (targeted delete + scheduled sweep)"`

---

## Task 9 — `lambda_org_api`: voice upload-url + asset-url + backfill

**Files:**
- Modify `src/lambda_org_api.py` (import `voice_messages`; add `VOICE_PREFIX` + `ALLOWED_VOICE_TYPES` + `_voice_s3_key`; 3 dispatch routes + 3 handlers)
- Create `tests/unit/test_voice_api.py`

**Interfaces:**
- Consumes: existing `_allowed_site_ids(conn, caller)`, `_safe_seg`, `s3()`, `ok`/`error`, `parse_body`, `PRESIGNED_URL_EXPIRY`, `S3_BUCKET`; `repositories.voice_messages.list_since` (Task 9 does NOT insert — `sendVoice`/Task 6 is the sole writer of `voice_messages`).
- Produces (org-api REST, in-VPC, existing Cognito authorizer — NO new auth):
  - `POST /api/org/voice/upload-url` body `{contentType, siteId, durationS?}` → `{uploadUrl, s3Key}` (presigned PUT under `voice/{company}/{site}/{uuid}.{ext}`; does NOT insert a row — `sendVoice` is the sole writer of `voice_messages`, so an abandoned recording leaves no orphan row).
  - `GET /api/org/voice/asset-url?key=voice/...` → `{url, expiresIn}` (presigned GET; key must be under the caller's company prefix).
  - `GET /api/org/sites/{siteId}/voice?since=<ts>` → `{items:[{s3Key, senderUserId, durationS, createdAt}], site}` (backfill; ACL via `_allowed_site_ids`; rows serialized to camelCase to match the app + the other voice endpoints).
  - `_voice_s3_key(company_id, site_id, sender_id, file_ext) -> str`.

> Grounded: mirrors the existing `create_upload_url`/`get_asset_url` presign pattern and the `create_recording_upload_url` idempotency shape, but a DEDICATED `voice/` prefix + `voice_messages` table — never `recordings`/`create_recording_upload_url` (data-isolation invariant). The `voice/asset-url` GET is added because the app must turn an `s3Key` (from the WS payload or backfill) into a downloadable presigned GET; it enforces tenant isolation by requiring the key to start with the caller's company prefix.

Steps:
- [ ] Write failing `tests/unit/test_voice_api.py` (mirrors `tests/unit/test_recordings_api.py` fixtures):
```python
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeS3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params, "expires": ExpiresIn}
        return "https://s3.example/" + Params["Key"]


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "global_role": "pm", "created_at": "2026-07-04", "archived_at": None}


def make_event(method, path, sub="sub-1", body=None, qs=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": qs,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}}}


def body_of(res):
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-1"})
    fake = FakeS3()
    monkeypatch.setattr(org, "_s3_client", fake)
    monkeypatch.setattr(org, "S3_BUCKET", "fieldsight-data-test-509194952652")
    return monkeypatch, fake


def test_voice_upload_url_presigns_only_no_row(wired):
    mp, fake = wired
    # upload-url must NOT write voice_messages — sendVoice is the sole writer,
    # so an abandoned recording leaves no orphan/duplicate backfill row.
    mp.setattr(org.voice_messages, "insert_message",
               lambda *a, **k: (_ for _ in ()).throw(AssertionError("upload-url must not insert")))
    res = org.lambda_handler(make_event("POST", "/api/org/voice/upload-url", body={
        "contentType": "audio/wav", "siteId": "s-1", "durationS": 2.0}), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert "messageId" not in b
    assert b["s3Key"].startswith("voice/c-1/s-1/") and b["s3Key"].endswith(".wav")
    assert b["uploadUrl"].endswith(b["s3Key"])
    assert fake.last["op"] == "put_object" and fake.last["params"]["ContentType"] == "audio/wav"


def test_voice_upload_url_bad_content_type_400(wired):
    mp, fake = wired
    res = org.lambda_handler(make_event("POST", "/api/org/voice/upload-url", body={
        "contentType": "video/mp4", "siteId": "s-1"}), None)
    assert res["statusCode"] == 400


def test_voice_upload_url_site_not_accessible_403(wired):
    mp, fake = wired
    mp.setattr(org, "_allowed_site_ids", lambda conn, caller: {"other"})
    res = org.lambda_handler(make_event("POST", "/api/org/voice/upload-url", body={
        "contentType": "audio/wav", "siteId": "s-1"}), None)
    assert res["statusCode"] == 403


def test_voice_asset_url_scoped_to_company_prefix(wired):
    mp, fake = wired
    res = org.lambda_handler(make_event("GET", "/api/org/voice/asset-url",
                                        qs={"key": "voice/c-1/s-1/x.wav"}), None)
    assert res["statusCode"] == 200 and fake.last["op"] == "get_object"
    # a key outside the caller's company prefix is refused
    bad = org.lambda_handler(make_event("GET", "/api/org/voice/asset-url",
                                        qs={"key": "voice/OTHER/s-1/x.wav"}), None)
    assert bad["statusCode"] == 400


def test_site_voice_backfill_lists_since(wired):
    mp, fake = wired
    mp.setattr(org.voice_messages, "list_since",
               lambda c, coid, sid, since: [{"id": "m-1", "s3_key": "voice/c-1/s-1/x.wav",
                                             "sender_user_id": "u-9", "duration_s": 3,
                                             "site_id": sid, "created_at": since}])
    res = org.lambda_handler(make_event("GET", "/api/org/sites/s-1/voice",
                                        qs={"since": "2026-07-18T00:00:00Z"}), None)
    assert res["statusCode"] == 200
    item = body_of(res)["items"][0]
    assert item["s3Key"] == "voice/c-1/s-1/x.wav"
    assert item["senderUserId"] == "u-9" and item["durationS"] == 3
    assert "s3_key" not in item  # camelCase only — no snake_case leaks across the API


def test_site_voice_backfill_acl_403(wired):
    mp, fake = wired
    mp.setattr(org, "_allowed_site_ids", lambda conn, caller: {"other"})
    res = org.lambda_handler(make_event("GET", "/api/org/sites/s-1/voice", qs={}), None)
    assert res["statusCode"] == 403
```
- [ ] Run, expect FAIL (`AttributeError: module 'lambda_org_api' has no attribute 'voice_messages'` / route returns 404):
  `python -m pytest tests/unit/test_voice_api.py -v`
- [ ] Edit `src/lambda_org_api.py` import to add `voice_messages`. Change:
```python
from repositories import (companies, memberships, observations, programme, programme_suggestions,
                          recordings, rollup, scope, sites, topics, users)
```
  to:
```python
from repositories import (companies, memberships, observations, programme, programme_suggestions,
                          recordings, rollup, scope, sites, topics, users, voice_messages)
```
- [ ] Edit `src/lambda_org_api.py`: add constants after `RECORDING_KINDS`/`_KIND_FOLDER` (line ~94):
```python
# Site voice (off-the-record): a DEDICATED voice/ prefix that matches NO S3
# event trigger (BUG-13), and the voice_messages table — never recordings /
# create_recording_upload_url (data-isolation invariant).
VOICE_PREFIX = os.environ.get("VOICE_PREFIX", "voice/")
ALLOWED_VOICE_TYPES = {"audio/wav": "wav", "audio/x-wav": "wav",
                       "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac"}
```
- [ ] Edit `src/lambda_org_api.py`: in `dispatch`, add routes just before `return error("not found", 404)`:
```python
    if route == "/voice/upload-url" and method == "POST":
        return create_voice_upload_url(conn, caller, parse_body(event))
    if route == "/voice/asset-url" and method == "GET":
        return get_voice_asset_url(event, caller)
    m_sv = re.match(r"^/sites/([^/]+)/voice$", route)
    if m_sv and method == "GET":
        return list_site_voice(conn, caller, m_sv.group(1), event)
```
- [ ] Edit `src/lambda_org_api.py`: add handlers (place after `complete_recording`, ~line 347):
```python
# ----------------------------------------------------------
# /voice — Site voice (off-the-record; dedicated voice/ prefix + voice_messages)
# ----------------------------------------------------------
def _voice_s3_key(company_id, site_id, sender_id, file_ext):
    # Dedicated voice/ prefix — matches NO S3 event trigger (BUG-13 / data
    # isolation). Scoped by company/site so a listing (and the asset-url ACL)
    # stays tenant-bounded. sender_id keeps sibling clips distinct in audit.
    return (f"{VOICE_PREFIX}{_safe_seg(str(company_id))}/{_safe_seg(str(site_id))}/"
            f"{_safe_seg(str(sender_id))}_{uuid.uuid4().hex}.{file_ext}")


def create_voice_upload_url(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    content_type = body.get("contentType")
    ext = ALLOWED_VOICE_TYPES.get(content_type) if isinstance(content_type, str) else None
    if ext is None:
        return error(f"contentType must be one of {sorted(ALLOWED_VOICE_TYPES)}", 400)
    site_id = body.get("siteId")
    if not site_id:
        return error("siteId is required", 400)
    if str(site_id) not in _allowed_site_ids(conn, caller):
        return error("site not accessible", 403)
    key = _voice_s3_key(caller["company_id"], site_id, caller["id"], ext)
    # NOTE: no voice_messages insert here — sendVoice (Task 6) is the sole writer
    # of the row (created when the clip is actually sent). An abandoned recording
    # thus leaves at most an orphan S3 object, reaped by the 30-day voice/ lifecycle.
    url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY)
    return ok({"uploadUrl": url, "s3Key": key})


def get_voice_asset_url(event, caller):
    """Presigned GET for a voice clip. Tenant-isolated: the key must live under
    the caller's own company prefix (voice/{company}/...), so a caller can only
    fetch their company's clips."""
    key = (event.get("queryStringParameters") or {}).get("key", "")
    prefix = f"{VOICE_PREFIX}{_safe_seg(str(caller['company_id']))}/"
    if not key.startswith(prefix):
        return error("key must be one of your company's voice clips", 400)
    url = s3().generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=PRESIGNED_URL_EXPIRY)
    return ok({"url": url, "expiresIn": PRESIGNED_URL_EXPIRY})


def list_site_voice(conn, caller, site_id, event):
    """Reconnect backfill: recent voice messages on a site the caller can see.
    ACL mirrors list_site_members / list_live_items (_allowed_site_ids)."""
    if str(site_id) not in _allowed_site_ids(conn, caller):
        return error("access denied to this site", 403)
    since = (event.get("queryStringParameters") or {}).get("since") or "1970-01-01T00:00:00Z"
    rows = voice_messages.list_since(conn, caller["company_id"], site_id, since)
    # Serialize to camelCase (matches upload-url/asset-url + the app's parser);
    # never leak snake_case DB column names across the API boundary.
    items = [{"s3Key": r["s3_key"], "senderUserId": str(r["sender_user_id"]),
              "durationS": r["duration_s"], "createdAt": str(r["created_at"])}
             for r in rows]
    return ok({"items": items, "site": str(site_id)})
```
- [ ] Run, expect PASS: `python -m pytest tests/unit/test_voice_api.py -v`
- [ ] Run the whole org-api suite to confirm no regression:
  `python -m pytest tests/unit/test_lambda_org_api.py tests/unit/test_recordings_api.py tests/unit/test_voice_api.py -v`
- [ ] Commit:
  `git add src/lambda_org_api.py tests/unit/test_voice_api.py`
  `git commit -m "Add org-api voice upload-url / asset-url / site backfill (off-the-record voice/ prefix)"`

---

## Task 10 — `template.yaml`: parameter, condition, JWT layer, 6 Lambdas, org-api IAM

**Files:**
- Modify `src/template.yaml`

**Interfaces:**
- Consumes: existing params `Stage`, `DbStackName`, `DbSecretArn`, `DbSubnetIds`, `OrgUserPoolId`, `EnableSchedules`, `DataBucketName`; conditions `HasDb`, `HasOrgApi`; `PsycopgLayer`; `StageConfig` map; the exact in-VPC `VpcConfig` + PG env block used by `MigrateFunction`/`OrgApiFunction`/`RagSearchFunction`/`VoiceAuditFunction`; the non-VPC SP-Ask split (`AskAgentFunction` → `VoiceAuditFunction`).
- Produces: Parameter `EnableSiteVoice`; Condition `HasSiteVoice`; `JwtLayer`; Functions `VoiceWsAuthorizerFunction`, `WsConnectFunction`, `WsDisconnectFunction`, `WsSendVoiceFunction`, `VoiceFanoutFunction`, `VoiceReaperFunction`; their LogGroups; `OrgApiFunction` gains `VOICE_PREFIX` env + `voice/*` S3 grant. These physical function names (`${Prefix}-voice-ws-authorizer`, `-ws-connect`, `-ws-disconnect`, `-send-voice`, `-voice-fanout`, `-voice-reaper`) are referenced by Task 11 (integrations/permissions) and Task 14 (smoke).

> Infra tasks are gated on `sam validate --lint` + `sam build` (there is no red unit test for CloudFormation — this mirrors how `deploy.yml` verifies the template). In-VPC functions reuse the MigrateFunction VpcConfig + PG env VERBATIM (BUG-36). The fanout's `execute-api:ManageConnections` is scoped to `POST/@connections/*` (any api in-account — acceptable least-privilege; only this fn holds the role). The reaper's `Input:'{"sweep":true}'` schedule is gated on `ShouldEnableSchedules` (OFF on test, as with every other schedule).

Steps:
- [ ] Add the `EnableSiteVoice` parameter (after `ManageDataBucketPolicy`, ~line 299):
```yaml
  EnableSiteVoice:
    Type: String
    Default: 'false'
    AllowedValues: ['true', 'false']
    Description: >-
      When true (and HasOrgApi), deploys the Site Voice WebSocket API + its
      Lambdas. Default false; test sets true (deploy.yml), prod is dark-launched
      false until the app is device-accepted (repo var PROD_ENABLE_SITE_VOICE,
      deploy-prod.yml) — mirrors PROD_ENABLE_SCHEDULES / PROD_AUTHORITY_FLIP.
```
- [ ] Add the `HasSiteVoice` condition (in `Conditions:`, after `HasOrgApi`, ~line 322):
```yaml
  HasSiteVoice: !And [!Condition HasOrgApi, !Equals [!Ref EnableSiteVoice, 'true']]
```
- [ ] Run: `sam validate --template-file src/template.yaml --lint --region ap-southeast-2` (expect PASS — param/condition alone are inert).
- [ ] Add the JWT layer (after `DashScopeLayer`, ~line 723):
```yaml
  # ----------------------------------------------------------
  # Lambda Layer: PyJWT[crypto] for the Site Voice WS authorizer.
  # Same sam-build pattern as PsycopgLayer/DashScopeLayer. Gated HasSiteVoice.
  # ----------------------------------------------------------
  JwtLayer:
    Type: AWS::Serverless::LayerVersion
    Condition: HasSiteVoice
    Properties:
      LayerName: !Sub ["${P}-jwt-layer", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      Description: PyJWT[crypto] (JWKS RS256) for the Site Voice WebSocket authorizer
      ContentUri: infra/jwt-layer/
      CompatibleRuntimes:
        - python3.11
    Metadata:
      BuildMethod: python3.11
```
- [ ] Add the 6 functions (after `VoiceAuditFunction`, ~line 1373, before the COGNITO section). Reaper first, then fanout, then the WS handlers, then the authorizer:
```yaml
  # ==========================================================
  # SITE VOICE (WebSocket) — Lambdas. WS API wiring is in the
  # ApiGatewayV2 block below. All gated by HasSiteVoice.
  # ==========================================================

  # In-VPC reaper: targeted delete (fanout GoneException) + scheduled sweep.
  VoiceReaperFunction:
    Type: AWS::Serverless::Function
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Sub ["${P}-voice-reaper", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_voice_reaper.lambda_handler
      Timeout: 60
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
      Policies:
        - VPCAccessPolicy: {}
      Events:
        SweepEvent:
          Type: Schedule
          Properties:
            Schedule: rate(6 hours)
            Description: Reap stale ws_connections + prune voice_messages > 30d
            State: !If [ShouldEnableSchedules, ENABLED, DISABLED]
            Input: '{"sweep": true}'

  # Non-VPC fanout: POST payload over @connections; GoneException -> reaper.
  VoiceFanoutFunction:
    Type: AWS::Serverless::Function
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Sub ["${P}-voice-fanout", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_voice_fanout.lambda_handler
      Timeout: 30
      MemorySize: 256
      Environment:
        Variables:
          VOICE_REAPER_FUNCTION: !Ref VoiceReaperFunction
      Policies:
        - LambdaInvokePolicy:
            FunctionName: !Ref VoiceReaperFunction
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              # @connections management POST. Scoped to POST/@connections; the
              # concrete WS api id (VoiceWebSocketApi, below) is created in the
              # ApiGatewayV2 block — wildcard api keeps this fn's role
              # self-contained and is least-privilege on action+path.
              Action: execute-api:ManageConnections
              Resource: !Sub arn:aws:execute-api:${AWS::Region}:${AWS::AccountId}:*/*/POST/@connections/*

  # In-VPC $connect: resolve sub -> upsert ws_connections.
  WsConnectFunction:
    Type: AWS::Serverless::Function
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Sub ["${P}-ws-connect", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_ws_connect.lambda_handler
      Timeout: 15
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
      Policies:
        - VPCAccessPolicy: {}

  # In-VPC $disconnect: delete the connection row.
  WsDisconnectFunction:
    Type: AWS::Serverless::Function
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Sub ["${P}-ws-disconnect", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_ws_disconnect.lambda_handler
      Timeout: 15
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
      Policies:
        - VPCAccessPolicy: {}

  # In-VPC sendVoice: ACL + insert pointer + resolve recipients + async fanout.
  WsSendVoiceFunction:
    Type: AWS::Serverless::Function
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Sub ["${P}-send-voice", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_ws_send_voice.lambda_handler
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
          PGHOST: !ImportValue
            Fn::Sub: "${DbStackName}-ClusterEndpoint"
          PGDATABASE: !ImportValue
            Fn::Sub: "${DbStackName}-DbName"
          PGUSER: postgres
          PGPASSWORD: !Sub '{{resolve:secretsmanager:${DbSecretArn}:SecretString:password}}'
          VOICE_FANOUT_FUNCTION: !Ref VoiceFanoutFunction
      Policies:
        - VPCAccessPolicy: {}
        - LambdaInvokePolicy:
            FunctionName: !Ref VoiceFanoutFunction

  # Non-VPC REQUEST authorizer: verify Cognito idToken via JWKS (RS256).
  VoiceWsAuthorizerFunction:
    Type: AWS::Serverless::Function
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Sub ["${P}-voice-ws-authorizer", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_ws_authorizer.lambda_handler
      Timeout: 10
      MemorySize: 256
      Layers:
        - !Ref JwtLayer
      Environment:
        Variables:
          # Trust the same pool(s) the REST CognitoAuthorizer trusts (the pool
          # the app logs into). Comma-separated to allow adding this stack's
          # own UserPool later; today just the org pool.
          WS_USER_POOL_IDS: !Ref OrgUserPoolId
```
- [ ] Add LogGroups for the 6 functions (in the LogGroups section, after `OrgSeedLogGroup`, ~line 1807):
```yaml
  VoiceWsAuthorizerLogGroup:
    Type: AWS::Logs::LogGroup
    Condition: HasSiteVoice
    Properties:
      LogGroupName: !Sub /aws/lambda/${VoiceWsAuthorizerFunction}
      RetentionInDays: 14

  WsConnectLogGroup:
    Type: AWS::Logs::LogGroup
    Condition: HasSiteVoice
    Properties:
      LogGroupName: !Sub /aws/lambda/${WsConnectFunction}
      RetentionInDays: 14

  WsDisconnectLogGroup:
    Type: AWS::Logs::LogGroup
    Condition: HasSiteVoice
    Properties:
      LogGroupName: !Sub /aws/lambda/${WsDisconnectFunction}
      RetentionInDays: 14

  WsSendVoiceLogGroup:
    Type: AWS::Logs::LogGroup
    Condition: HasSiteVoice
    Properties:
      LogGroupName: !Sub /aws/lambda/${WsSendVoiceFunction}
      RetentionInDays: 14

  VoiceFanoutLogGroup:
    Type: AWS::Logs::LogGroup
    Condition: HasSiteVoice
    Properties:
      LogGroupName: !Sub /aws/lambda/${VoiceFanoutFunction}
      RetentionInDays: 14

  VoiceReaperLogGroup:
    Type: AWS::Logs::LogGroup
    Condition: HasSiteVoice
    Properties:
      LogGroupName: !Sub /aws/lambda/${VoiceReaperFunction}
      RetentionInDays: 14
```
- [ ] Grant `OrgApiFunction` the `voice/` prefix + `VOICE_PREFIX` env. In `OrgApiFunction.Environment.Variables` (after `GRADED_ROLES`, ~line 820) add:
```yaml
          # Site voice: dedicated off-the-record prefix (matches no S3 trigger).
          VOICE_PREFIX: voice/
```
  and in `OrgApiFunction.Policies` add a statement beside the `users/*` grant (~line 849):
```yaml
            - Effect: Allow
              # Site voice clips: presigned PUT/GET under a DEDICATED voice/
              # prefix (never users/ or recordings) — the data-isolation
              # invariant. Same bucket as recordings (DataBucketName); voice/
              # matches no wire-s3-events.sh trigger, so it never ingests.
              Action:
                - s3:PutObject
                - s3:GetObject
              Resource: !Sub arn:aws:s3:::${DataBucketName}/voice/*
```
- [ ] Run: `sam validate --template-file src/template.yaml --lint --region ap-southeast-2` (expect PASS).
- [ ] Run `sam build --template-file src/template.yaml --base-dir .` (expect the JwtLayer to pip-build PyJWT[crypto] and all functions to build).
- [ ] Commit:
  `git add src/template.yaml`
  `git commit -m "SAM: EnableSiteVoice param, jwt-layer, 6 Site Voice Lambdas, org-api voice/ grant"`

---

## Task 11 — `template.yaml`: raw `AWS::ApiGatewayV2::*` WebSocket API

**Files:**
- Modify `src/template.yaml`

**Interfaces:**
- Consumes: Task 10's `VoiceWsAuthorizerFunction`, `WsConnectFunction`, `WsDisconnectFunction`, `WsSendVoiceFunction` (`.Arn`); condition `HasSiteVoice`; `StageConfig`.
- Produces: `VoiceWebSocketApi` (WEBSOCKET), `VoiceWsAuthorizer` (REQUEST), integrations + routes for `$connect`/`$disconnect`/`sendVoice`, `VoiceWsDeployment`, `VoiceWsStage` (StageName `prod`), 4 `AWS::Lambda::Permission` resources, and Output `VoiceWsEndpoint` (`wss://${VoiceWebSocketApi}.execute-api.${region}.amazonaws.com/prod`, read by Task 14's smoke script).

> Grounded: the template has NO ApiGatewayV2 resources today — this is all-new (SAM has no first-class WebSocket type). `RouteSelectionExpression: $request.body.action` routes `{"action":"sendVoice",...}`. Raw integrations need explicit `AWS::Lambda::Permission` (unlike SAM `Api` events, which synthesize them). `$connect` uses the CUSTOM authorizer; `$disconnect`/`sendVoice` are NONE (the connection is already trusted post-handshake). The explicit Deployment + Stage matches the spec; a routes change requires the deployment to be recreated on `sam deploy` (acceptable — or switch the Stage to `AutoDeploy: true` and drop the Deployment).

Steps:
- [ ] Add the WebSocket API + authorizer (after the Site Voice Lambdas from Task 10, before the COGNITO section):
```yaml
  # ----------------------------------------------------------
  # Site Voice WebSocket API (raw ApiGatewayV2 — SAM has no WS type).
  # ----------------------------------------------------------
  VoiceWebSocketApi:
    Type: AWS::ApiGatewayV2::Api
    Condition: HasSiteVoice
    Properties:
      Name: !Sub ["${P}-voice-ws", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      ProtocolType: WEBSOCKET
      RouteSelectionExpression: "$request.body.action"

  VoiceWsAuthorizer:
    Type: AWS::ApiGatewayV2::Authorizer
    Condition: HasSiteVoice
    Properties:
      ApiId: !Ref VoiceWebSocketApi
      Name: !Sub ["${P}-voice-ws-authorizer", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      AuthorizerType: REQUEST
      IdentitySource:
        - route.request.header.Authorization
      AuthorizerUri: !Sub arn:aws:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/${VoiceWsAuthorizerFunction.Arn}/invocations
```
- [ ] Add the three integrations:
```yaml
  VoiceWsConnectIntegration:
    Type: AWS::ApiGatewayV2::Integration
    Condition: HasSiteVoice
    Properties:
      ApiId: !Ref VoiceWebSocketApi
      IntegrationType: AWS_PROXY
      IntegrationUri: !Sub arn:aws:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/${WsConnectFunction.Arn}/invocations

  VoiceWsDisconnectIntegration:
    Type: AWS::ApiGatewayV2::Integration
    Condition: HasSiteVoice
    Properties:
      ApiId: !Ref VoiceWebSocketApi
      IntegrationType: AWS_PROXY
      IntegrationUri: !Sub arn:aws:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/${WsDisconnectFunction.Arn}/invocations

  VoiceWsSendVoiceIntegration:
    Type: AWS::ApiGatewayV2::Integration
    Condition: HasSiteVoice
    Properties:
      ApiId: !Ref VoiceWebSocketApi
      IntegrationType: AWS_PROXY
      IntegrationUri: !Sub arn:aws:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/${WsSendVoiceFunction.Arn}/invocations
```
- [ ] Add the three routes ($connect authorized; $disconnect + sendVoice NONE):
```yaml
  VoiceWsConnectRoute:
    Type: AWS::ApiGatewayV2::Route
    Condition: HasSiteVoice
    Properties:
      ApiId: !Ref VoiceWebSocketApi
      RouteKey: $connect
      AuthorizationType: CUSTOM
      AuthorizerId: !Ref VoiceWsAuthorizer
      Target: !Sub integrations/${VoiceWsConnectIntegration}

  VoiceWsDisconnectRoute:
    Type: AWS::ApiGatewayV2::Route
    Condition: HasSiteVoice
    Properties:
      ApiId: !Ref VoiceWebSocketApi
      RouteKey: $disconnect
      AuthorizationType: NONE
      Target: !Sub integrations/${VoiceWsDisconnectIntegration}

  VoiceWsSendVoiceRoute:
    Type: AWS::ApiGatewayV2::Route
    Condition: HasSiteVoice
    Properties:
      ApiId: !Ref VoiceWebSocketApi
      RouteKey: sendVoice
      AuthorizationType: NONE
      Target: !Sub integrations/${VoiceWsSendVoiceIntegration}
```
- [ ] Add the deployment + stage (DependsOn all routes so they exist before deploy):
```yaml
  VoiceWsDeployment:
    Type: AWS::ApiGatewayV2::Deployment
    Condition: HasSiteVoice
    DependsOn:
      - VoiceWsConnectRoute
      - VoiceWsDisconnectRoute
      - VoiceWsSendVoiceRoute
    Properties:
      ApiId: !Ref VoiceWebSocketApi

  VoiceWsStage:
    Type: AWS::ApiGatewayV2::Stage
    Condition: HasSiteVoice
    Properties:
      ApiId: !Ref VoiceWebSocketApi
      StageName: prod
      DeploymentId: !Ref VoiceWsDeployment
```
- [ ] Add the four Lambda invoke permissions (API Gateway → each function):
```yaml
  VoiceWsAuthorizerPermission:
    Type: AWS::Lambda::Permission
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Ref VoiceWsAuthorizerFunction
      Action: lambda:InvokeFunction
      Principal: apigateway.amazonaws.com
      SourceArn: !Sub arn:aws:execute-api:${AWS::Region}:${AWS::AccountId}:${VoiceWebSocketApi}/*

  WsConnectPermission:
    Type: AWS::Lambda::Permission
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Ref WsConnectFunction
      Action: lambda:InvokeFunction
      Principal: apigateway.amazonaws.com
      SourceArn: !Sub arn:aws:execute-api:${AWS::Region}:${AWS::AccountId}:${VoiceWebSocketApi}/*

  WsDisconnectPermission:
    Type: AWS::Lambda::Permission
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Ref WsDisconnectFunction
      Action: lambda:InvokeFunction
      Principal: apigateway.amazonaws.com
      SourceArn: !Sub arn:aws:execute-api:${AWS::Region}:${AWS::AccountId}:${VoiceWebSocketApi}/*

  WsSendVoicePermission:
    Type: AWS::Lambda::Permission
    Condition: HasSiteVoice
    Properties:
      FunctionName: !Ref WsSendVoiceFunction
      Action: lambda:InvokeFunction
      Principal: apigateway.amazonaws.com
      SourceArn: !Sub arn:aws:execute-api:${AWS::Region}:${AWS::AccountId}:${VoiceWebSocketApi}/*
```
- [ ] Add the WS endpoint Output (in `Outputs:`, after `ApiEndpoint`, ~line 1943):
```yaml
  VoiceWsEndpoint:
    Description: Site Voice WebSocket endpoint (wss)
    Condition: HasSiteVoice
    Value: !Sub wss://${VoiceWebSocketApi}.execute-api.${AWS::Region}.amazonaws.com/prod
```
- [ ] Run: `sam validate --template-file src/template.yaml --lint --region ap-southeast-2` (expect PASS).
- [ ] Run: `sam build --template-file src/template.yaml --base-dir .` (expect PASS).
- [ ] Commit:
  `git add src/template.yaml`
  `git commit -m "SAM: Site Voice WebSocket API (ApiGatewayV2 routes/authorizer/deployment/stage + permissions)"`

---

## Task 12 — Deploy workflows, `samconfig.toml`, CI test deps

**Files:**
- Modify `.github/workflows/deploy.yml` (test: `EnableSiteVoice=true`)
- Modify `.github/workflows/deploy-prod.yml` (prod: repo var `PROD_ENABLE_SITE_VOICE`)
- Modify `samconfig.toml` (`[test.deploy.parameters]` → `EnableSiteVoice=true`)
- Modify `.github/workflows/test.yml` (add `PyJWT[crypto]` so the authorizer unit tests run)

**Interfaces:**
- Consumes: Task 10's `EnableSiteVoice` parameter.
- Produces: `EnableSiteVoice=true` on test deploys; `EnableSiteVoice=<PROD_ENABLE_SITE_VOICE|false>` on prod (dark-launch, default false — mirrors `PROD_ENABLE_SCHEDULES` / `PROD_AUTHORITY_FLIP`); CI installs PyJWT so `test_lambda_ws_authorizer.py` collects.

Steps:
- [ ] Edit `.github/workflows/deploy.yml`, in the `sam deploy --config-env test` `--parameter-overrides`, add after the `"EnableSchedules=false"` line:
```yaml
              "EnableSiteVoice=true" \
```
- [ ] Edit `.github/workflows/deploy-prod.yml`, in the `sam deploy --config-env prod` `--parameter-overrides`, add after the `"EnableSchedules=..."` line:
```yaml
              "EnableSiteVoice=${{ vars.PROD_ENABLE_SITE_VOICE || 'false' }}" \
```
- [ ] Edit `samconfig.toml`, in `[test.deploy.parameters].parameter_overrides`, add `"EnableSiteVoice=true",` after `"EnableSchedules=false",` (keeps a local `sam deploy --config-env test` consistent with CI):
```toml
    "EnableSchedules=false",
    "EnableSiteVoice=true",
```
- [ ] Edit `.github/workflows/test.yml`, extend the pip install line to include PyJWT[crypto]:
```yaml
      - run: pip install "psycopg[binary]>=3.1" "pgvector>=0.3" "PyJWT[crypto]>=2.8" boto3 pytest pytest-cov
```
- [ ] Verify workflow YAML is well-formed: `python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" .github/workflows/deploy.yml .github/workflows/deploy-prod.yml .github/workflows/test.yml` (expect no error). If PyYAML/python is unavailable locally (BUG-29), rely on the `sam validate`/CI parse instead.
- [ ] Manual (one-time, before the prod cut): create the repo variable `PROD_ENABLE_SITE_VOICE=false` (GitHub → Settings → Variables). Flip to `true` only after the app is device-accepted (rollout step 4).
- [ ] Commit:
  `git add .github/workflows/deploy.yml .github/workflows/deploy-prod.yml samconfig.toml .github/workflows/test.yml`
  `git commit -m "Wire EnableSiteVoice (test=true, prod dark via PROD_ENABLE_SITE_VOICE) + PyJWT CI dep"`

---

## Task 13 — 30-day `voice/` S3 lifecycle rule

**Files:**
- Modify `scripts/wire-bucket-lifecycle.sh`

**Interfaces:**
- Consumes: the existing `put-bucket-lifecycle-configuration` replace-all pattern + the "other rules present → abort" guard.
- Produces: a `voice-clips-expiry` rule (Prefix `voice/`, Expiration 30 days) applied to the TEST bucket by `deploy.yml`'s existing lifecycle step.

> Grounded: `put-bucket-lifecycle-configuration` REPLACES the whole config, so the rule is added to the SAME array and its ID is added to the guard's allow-list. The script runs on `DataBucketName` (the TEST bucket) in `deploy.yml`; on PROD the lake bucket's lifecycle is hand-managed — the same `voice/` 30-day rule must be added there by hand (see rollout note), mirroring how prod S3 event notifications are hand-managed.

Steps:
- [ ] Edit the guard query in `scripts/wire-bucket-lifecycle.sh` to also allow the new id — change:
```bash
  --query 'Rules[?ID!=`org-assets-pending-expiry` && ID!=`download-claims-expiry`].ID' \
```
  to:
```bash
  --query 'Rules[?ID!=`org-assets-pending-expiry` && ID!=`download-claims-expiry` && ID!=`voice-clips-expiry`].ID' \
```
- [ ] Add the rule to the `--lifecycle-configuration` JSON `Rules` array (after the `download-claims-expiry` rule object):
```json
      ,{
        "ID": "voice-clips-expiry",
        "Status": "Enabled",
        "Filter": { "Prefix": "voice/" },
        "Expiration": { "Days": 30 }
      }
```
- [ ] Verify shell syntax: `bash -n scripts/wire-bucket-lifecycle.sh` (expect no output = OK). If `shellcheck` is available: `shellcheck scripts/wire-bucket-lifecycle.sh`.
- [ ] Verify the embedded JSON is valid (three rules): `node -e "const s=require('fs').readFileSync('scripts/wire-bucket-lifecycle.sh','utf8'); const j=s.slice(s.indexOf('{',s.indexOf('lifecycle-configuration')), s.lastIndexOf('}')+1); console.log(JSON.parse(j).Rules.map(r=>r.ID))"` (expect `[ 'org-assets-pending-expiry', 'download-claims-expiry', 'voice-clips-expiry' ]`).
- [ ] Commit:
  `git add scripts/wire-bucket-lifecycle.sh`
  `git commit -m "Add 30-day voice/ S3 lifecycle expiry rule"`

---

## Task 14 — End-to-end verification: `wscat` smoke + `deploy.yml` step

**Files:**
- Create `scripts/voice-ws-smoke.sh`
- Modify `.github/workflows/deploy.yml` (add a best-effort voice smoke step)

**Interfaces:**
- Consumes: stack outputs `VoiceWsEndpoint` + `ApiEndpoint`; env `VOICE_SMOKE_TOKEN` (Cognito idToken of a site member), `VOICE_SMOKE_SITE` (site UUID), optional `VOICE_SMOKE_TOKEN2` (a SECOND member — enables the fanout-receive assertion). Reaper: `aws lambda invoke fieldsight-<stage>-voice-reaper`.
- Produces: `scripts/voice-ws-smoke.sh <stack> <region>` covering connect/authorizer(allow+deny)/upload-url/PUT/sendVoice/fanout/backfill/reap; a deploy.yml step that runs it best-effort (skips cleanly when secrets are absent, so CI never breaks).

> This is a smoke/verification script, not shipped code: it needs node with the `ws` package (installed to a temp dir) and the AWS CLI. It skips gracefully (exit 0) when `VOICE_SMOKE_TOKEN`/`VOICE_SMOKE_SITE` are unset. The full two-client fanout assertion runs only when `VOICE_SMOKE_TOKEN2` is also provided (recipients EXCLUDE the sender's user, so two DISTINCT members are required).

Steps:
- [ ] Create `scripts/voice-ws-smoke.sh`:
```bash
#!/usr/bin/env bash
# voice-ws-smoke.sh <stack> <region>
# End-to-end Site Voice smoke on a deployed stack (e.g. fieldsight-test):
#   authorizer (allow + deny) / connect / upload-url + PUT / sendVoice /
#   fanout-receive / backfill / reaper sweep.
# Requires: aws CLI, node (auto-installs the `ws` package to a temp dir).
# Env:
#   VOICE_SMOKE_TOKEN   Cognito idToken of a site member (REQUIRED to run;
#                       absent -> skip cleanly with exit 0 so CI never breaks)
#   VOICE_SMOKE_SITE    site UUID that member belongs to (REQUIRED)
#   VOICE_SMOKE_TOKEN2  a SECOND member's idToken (OPTIONAL) -> enables the
#                       fanout-receive assertion (sender is excluded, so two
#                       distinct members are needed to observe delivery)
set -euo pipefail
STACK="${1:?usage: voice-ws-smoke.sh <stack> <region>}"
REGION="${2:?missing region}"

if [ -z "${VOICE_SMOKE_TOKEN:-}" ] || [ -z "${VOICE_SMOKE_SITE:-}" ]; then
  echo "SKIP: VOICE_SMOKE_TOKEN / VOICE_SMOKE_SITE not set (no token to drive the WS)."
  exit 0
fi

out() { aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
WS="$(out VoiceWsEndpoint)"; API="$(out ApiEndpoint)"
[ -n "$WS" ] && [ "$WS" != "None" ] || { echo "no VoiceWsEndpoint output"; exit 1; }
echo "WS=$WS  API=$API"

# --- REST: reserve an upload, PUT bytes -----------------------------------
UP="$(curl -s -X POST "$API/org/voice/upload-url" \
  -H "Authorization: $VOICE_SMOKE_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"contentType\":\"audio/wav\",\"siteId\":\"$VOICE_SMOKE_SITE\",\"durationS\":1.0}")"
echo "upload-url -> $UP"
S3KEY="$(node -e "process.stdin.on('data',d=>{const j=JSON.parse(d);console.log(j.s3Key)})" <<<"$UP")"
URL="$(node -e "process.stdin.on('data',d=>{const j=JSON.parse(d);console.log(j.uploadUrl)})" <<<"$UP")"
[ -n "$S3KEY" ] || { echo "no s3Key returned"; exit 1; }
printf 'RIFF....WAVEfmt ' > /tmp/voice-smoke.wav   # 16 bytes is enough to PUT
curl -s -X PUT "$URL" -H 'Content-Type: audio/wav' --data-binary @/tmp/voice-smoke.wav
echo "PUT ok: $S3KEY"

# --- node ws harness: authorizer allow/deny + connect + sendVoice/fanout ---
WORK="$(mktemp -d)"; ( cd "$WORK" && npm i ws --silent >/dev/null 2>&1 )
export NODE_PATH="$WORK/node_modules"
WS_URL="$WS" GOOD="$VOICE_SMOKE_TOKEN" TOKEN2="${VOICE_SMOKE_TOKEN2:-}" \
SITE="$VOICE_SMOKE_SITE" S3KEY="$S3KEY" node <<'NODE'
const WebSocket = require('ws');
const { WS_URL, GOOD, TOKEN2, SITE, S3KEY } = process.env;
const conn = (tok) => new Promise((res, rej) => {
  const w = new WebSocket(WS_URL, { headers: { Authorization: tok } });
  w.on('open', () => res(w));
  w.on('unexpected-response', (_r, r) => rej(new Error('handshake ' + r.statusCode)));
  w.on('error', rej);
});
(async () => {
  // 1) authorizer DENY: a bad token must be refused at the handshake.
  let denied = false;
  try { await conn('not-a-real-token'); } catch (e) { denied = /handshake 401|403/.test(e.message); }
  if (!denied) throw new Error('bad token was NOT rejected');
  console.log('authorizer deny: ok');

  // 2) authorizer ALLOW + connect.
  const a = await conn(GOOD);
  console.log('authorizer allow + connect: ok');

  if (TOKEN2) {
    // 3) fanout: B (a second member) must receive A's sendVoice; A must not.
    const b = await conn(TOKEN2);
    const got = new Promise((res) => b.on('message', (m) => res(JSON.parse(m))));
    let selfEcho = false; a.on('message', () => { selfEcho = true; });
    a.send(JSON.stringify({ action: 'sendVoice', siteId: SITE, s3Key: S3KEY, durationS: 1.0 }));
    const msg = await Promise.race([got,
      new Promise((_, rej) => setTimeout(() => rej(new Error('B never received')), 8000))]);
    if (msg.s3Key !== S3KEY) throw new Error('B got wrong payload: ' + JSON.stringify(msg));
    if (selfEcho) throw new Error('sender received its own message');
    console.log('fanout receive (B got it, A did not): ok');
    b.close();
  } else {
    // No second member -> just prove sendVoice is accepted (0 recipients).
    a.send(JSON.stringify({ action: 'sendVoice', siteId: SITE, s3Key: S3KEY, durationS: 1.0 }));
    console.log('sendVoice accepted (single member; fanout receive skipped — set VOICE_SMOKE_TOKEN2)');
  }
  a.close();
})().then(() => process.exit(0)).catch((e) => { console.error('FAIL:', e.message); process.exit(1); });
NODE

# --- REST: backfill must list the message we just sent --------------------
BF="$(curl -s "$API/org/sites/$VOICE_SMOKE_SITE/voice?since=1970-01-01T00:00:00Z" \
  -H "Authorization: $VOICE_SMOKE_TOKEN")"
echo "$BF" | node -e "process.stdin.on('data',d=>{const j=JSON.parse(d);const hit=(j.items||[]).some(m=>m.s3Key===process.env.S3KEY);if(!hit){console.error('backfill missing '+process.env.S3KEY);process.exit(1)}console.log('backfill: ok ('+j.items.length+' msgs)')})" S3KEY="$S3KEY"

# --- reaper sweep: must run without a FunctionError ------------------------
PREFIX="$(basename "$STACK")"   # fieldsight-test / fieldsight-prod
RESP="$(aws lambda invoke --function-name "${PREFIX}-voice-reaper" \
  --cli-binary-format raw-in-base64-out --payload '{"sweep": true}' \
  /tmp/reaper-out.json --region "$REGION")"
echo "reaper: $RESP"; cat /tmp/reaper-out.json; echo
echo "$RESP" | grep -q '"FunctionError"' && { echo "reaper raised"; exit 1; } || true
echo "ALL SITE VOICE SMOKE CHECKS PASSED"
```
- [ ] Verify shell + node harness syntax: `bash -n scripts/voice-ws-smoke.sh` (expect no output).
- [ ] Add a best-effort step to `.github/workflows/deploy.yml` (after the existing `Smoke test /api/health (TEST)` step):
```yaml
      - name: Site Voice WS smoke (TEST, best-effort)
        # Runs only if a member idToken + site are provisioned as repo secrets;
        # skips cleanly (exit 0) otherwise so the deploy never fails on absence.
        env:
          VOICE_SMOKE_TOKEN: ${{ secrets.VOICE_SMOKE_TOKEN }}
          VOICE_SMOKE_SITE: ${{ secrets.VOICE_SMOKE_SITE }}
          VOICE_SMOKE_TOKEN2: ${{ secrets.VOICE_SMOKE_TOKEN2 }}
        run: bash scripts/voice-ws-smoke.sh fieldsight-test ${{ env.AWS_REGION }}
```
- [ ] Run the smoke against the deployed test stack (after Tasks 1-13 are deployed via `develop`):
  `VOICE_SMOKE_TOKEN=<idToken> VOICE_SMOKE_SITE=<site-uuid> VOICE_SMOKE_TOKEN2=<idToken2> bash scripts/voice-ws-smoke.sh fieldsight-test ap-southeast-2`
  (expect `ALL SITE VOICE SMOKE CHECKS PASSED`).
- [ ] Commit:
  `git add scripts/voice-ws-smoke.sh .github/workflows/deploy.yml`
  `git commit -m "Add Site Voice end-to-end wscat/node smoke + best-effort deploy step"`

---

## Deployment & rollout (execution order)
1. **Backend → test.** Merge `feature/site-voice` → `develop`. `deploy.yml` runs `sam deploy --config-env test` (`EnableSiteVoice=true`) + invokes `fieldsight-test-migrate` (creates `ws_connections` + `voice_messages` on the shared Aurora) + wires the `voice/` lifecycle rule. Verify with `scripts/voice-ws-smoke.sh` (Task 14).
2. **App → test.** GrandTime dev flavor points at the test WS stack; real-device soak (out of scope for this backend plan).
3. **Backend → prod (dark).** Merge `main` → `deploy-prod.yml` (production reviewer gate) + `fieldsight-prod-migrate` (no-op, shared ledger). `PROD_ENABLE_SITE_VOICE=false` → the WS API + its Lambdas are NOT created (HasSiteVoice false); purely additive, zero crossover. Add the `voice/` 30-day rule to the hand-managed lake-bucket lifecycle by hand at this point.
4. **Enable + ship.** Flip `PROD_ENABLE_SITE_VOICE=true`, re-run deploy-prod; ship the GrandTime prod flavor.

**Coordination:** the Phase-3 graded-roles plan touches memberships/roles; `sendVoice`/backfill ACL reuses `memberships.accessible_site_ids` / `_allowed_site_ids` — no schema collision (this plan only ADDS tables), but land after/independently of that plan's membership changes.
