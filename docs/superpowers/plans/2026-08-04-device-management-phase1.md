# Device Management Phase 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the device ledger's backend half — schema, heartbeat capture, and an inert Notion sync — on TEST tonight, without colliding with the programme work other sessions are landing in parallel.

**Architecture:** A new `src/device_heartbeat.py` module owns all device logic. `lambda_org_api.py` — the file three other in-flight branches are editing — is touched on exactly **two lines**. Two new Lambdas (non-VPC scheduler → in-VPC ledger reader) are **appended** to the end of `template.yaml`. The Notion sync ships **inert**: with no integration token configured it logs and returns, so it is safe to deploy before the token exists.

**Tech Stack:** Python 3.12 Lambda, Aurora PostgreSQL (psycopg3 via `PsycopgLayer`), AWS SAM, pytest with the repo's `FakeConn`/`FakeCursor` doubles.

**Spec:** `docs/superpowers/specs/2026-08-03-device-management-design.md`

## Global Constraints

- **Conflict surface is the primary design constraint tonight.** `src/lambda_org_api.py` (5125 lines) is being edited by `fix/drop-redundant-guard`, `feat/suggestions-own-scope` and `test/suggestion-confirm-lost-on-snapshot`. Every line added there is a merge conflict waiting to happen. Two lines maximum, both at the top of `lambda_handler`, far from the programme routes those branches touch.
- **Migration number is `0030`.** Verified free across all branches on 2026-08-04. If another session claims it first, renumber to the next free integer — never reuse.
- **Append-only in `template.yaml`.** New resources go at the end of `Resources:`, never interleaved, so a concurrent edit conflicts at worst on one adjacent line.
- **New resources must be named `fieldsight-*`** or the deploy role denies creation and the entire stack rolls back. `!Sub ["${P}-…", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]` produces `fieldsight-test-…` / `fieldsight-prod-…`, which satisfies this.
- **Ship inert.** `NOTION_TOKEN` empty ⇒ sync is a no-op. `EnableSchedules` already gates the schedule. Nothing in this plan can affect existing behaviour when its env vars are unset.
- **No `git add -A`** in this repo (Windows autocrlf mixes CRLF/LF and it will sweep in unrelated files). Add named paths only.
- **Test harness:**
  ```
  export UV_LINK_MODE=copy
  export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
  uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
  ```
- **`export MSYS_NO_PATHCONV=1`** before any AWS CLI call with a leading-slash argument (BUG-28/42).
- All "today"/date arithmetic uses `Pacific/Auckland`, never bare `datetime.now()` (BUG-37).

---

## Task 0: Back-merge `main` into `develop` before anything else

`origin/main` is one commit ahead of `origin/develop`: `3df430c release: Save preserves local rows (guard + real fix) (#213)`. It touches `src/lambda_org_api.py` and `src/repositories/programme_tasks.py` — the same files tonight's programme branches touch. Until it is back-merged, every branch cut from `develop` is missing a fix that is already live on prod, and tonight's release merge will have to resolve that divergence under time pressure.

This task is first because everything else in this plan, and everything the other sessions merge tonight, is safer once `develop` contains prod.

**Files:** none edited by hand — this is a merge.

- [ ] **Step 1: Confirm the divergence is still exactly one commit**

```bash
cd /c/Users/camil/Dropbox/fieldsight-pipeline
git fetch origin
git log --oneline origin/develop..origin/main
```
Expected: exactly `3df430c release: Save preserves local rows (guard + real fix) (#213)`.
If more commits appear, another session pushed to main — stop and re-read them before merging.

- [ ] **Step 2: Merge on a branch, not on develop directly**

```bash
git checkout -b chore/backmerge-main-to-develop origin/develop
git merge origin/main
```
Expected: a clean merge. `develop` has not touched those regions, so a conflict here means another session pushed something new — read it rather than resolving mechanically.

- [ ] **Step 3: Prove prod's fix survived the merge**

```bash
git diff origin/main -- src/repositories/programme_tasks.py src/lambda_org_api.py
```
Expected: the diff shows only `develop`'s additions, never a removal of anything `#213` added. If any `#213` line appears as a deletion, the merge is wrong — abort and redo.

- [ ] **Step 4: Run the full unit suite**

```bash
export UV_LINK_MODE=copy
export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```
Expected: PASS. A failure here is `develop` and prod being genuinely incompatible, which is exactly what this task exists to surface — report it rather than working around it.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin chore/backmerge-main-to-develop
gh pr create --base develop --head chore/backmerge-main-to-develop \
  --title "chore: back-merge main (#213) into develop" \
  --body "prod had a programme fix develop lacked. Merging before tonight's releases so every branch cut from develop contains prod."
```

---

## Task 1: Migration `0030_devices.sql`

**Files:**
- Create: `src/migrations/0030_devices.sql`
- Test: `tests/unit/test_migration_0030_devices.py`

**Interfaces:**
- Produces: table `devices` with columns `id uuid pk`, `asset_tag text unique not null`, `device_uuid text`, `uuid_trusted boolean not null default true`, `app_version text`, `last_seen_at timestamptz`, `last_account_sub text`, `created_at timestamptz not null default now()`; and column `recordings.device_id uuid` referencing it. Task 2 writes to this table; Task 4 reads it.

- [ ] **Step 1: Write the failing test**

The repo has no live Postgres in unit tests, so this test asserts the migration's *text* — that it is idempotent and declares the exact contract later tasks rely on. This is the same shape as other migration guards in this repo.

```python
# tests/unit/test_migration_0030_devices.py
from pathlib import Path

SQL = Path("src/migrations/0030_devices.sql").read_text(encoding="utf-8")


def test_creates_devices_table_idempotently():
    assert "create table if not exists devices" in SQL.lower()


def test_asset_tag_is_the_unique_authoritative_identity():
    lowered = SQL.lower()
    assert "asset_tag text unique not null" in lowered


def test_device_uuid_is_nullable_and_distrustable():
    lowered = SQL.lower()
    assert "device_uuid text" in lowered
    assert "uuid_trusted boolean not null default true" in lowered


def test_adds_device_id_to_recordings_idempotently():
    lowered = SQL.lower()
    assert "alter table recordings add column if not exists device_id uuid" in lowered


def test_indexes_the_columns_the_report_reads():
    lowered = SQL.lower()
    assert "create index if not exists" in lowered
    assert "devices (last_seen_at" in lowered or "devices(last_seen_at" in lowered
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run --with pytest pytest tests/unit/test_migration_0030_devices.py -q
```
Expected: FAIL — `FileNotFoundError: src/migrations/0030_devices.sql`.

- [ ] **Step 3: Write the migration**

```sql
-- src/migrations/0030_devices.sql
-- Device ledger. asset_tag (the physical FS-xx label) is the authoritative
-- identity; device_uuid is advisory because the F2SP ROM may report an
-- identical ANDROID_ID across every device flashed from one factory image.

create table if not exists devices (
  id                uuid primary key,
  asset_tag         text unique not null,
  device_uuid       text,
  uuid_trusted      boolean not null default true,
  app_version       text,
  last_seen_at      timestamptz,
  last_account_sub  text,
  created_at        timestamptz not null default now()
);

create index if not exists devices_last_seen_idx
  on devices (last_seen_at nulls first);

create index if not exists devices_uuid_idx
  on devices (device_uuid) where device_uuid is not null;

alter table recordings add column if not exists device_id uuid;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'recordings_device_id_fkey'
  ) then
    alter table recordings
      add constraint recordings_device_id_fkey
      foreign key (device_id) references devices(id);
  end if;
end $$;

create index if not exists recordings_device_created_idx
  on recordings (device_id, created_at desc);
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run --with pytest pytest tests/unit/test_migration_0030_devices.py -q
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/migrations/0030_devices.sql tests/unit/test_migration_0030_devices.py
git commit -m "feat(devices): migration 0030 — device ledger table"
```

---

## Task 2: `device_heartbeat.record()` — the conditional upsert

All device logic lives in its own module so `lambda_org_api.py` stays untouched apart from Task 3's two lines.

**Files:**
- Create: `src/device_heartbeat.py`
- Test: `tests/unit/test_device_heartbeat.py`

**Interfaces:**
- Consumes: the `devices` table from Task 1.
- Produces:
  - `parse_headers(headers: dict) -> dict | None` — returns `{"asset_tag": str, "device_uuid": str | None, "app_version": str | None}` or `None` when no device headers are present. Header lookup is case-insensitive. When `X-Device-Tag` is absent but `X-Device-Id` is present, `asset_tag` is `"unclaimed:" + device_uuid[:8]`.
  - `record(conn, ident: dict, account_sub: str | None) -> None` — performs the throttled upsert and the uuid-collision check. Never raises; logs and returns on any failure.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_device_heartbeat.py
import src.device_heartbeat as dh


class FakeCursor:
    def __init__(self, results=None):
        self.executed = []
        self._results = list(results or [])

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._results.pop(0) if self._results else []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, results=None):
        self.cur = FakeCursor(results)
        self.committed = False

    def cursor(self, *a, **k):
        return self.cur

    def commit(self):
        self.committed = True


def test_parse_headers_returns_none_without_device_headers():
    assert dh.parse_headers({"Authorization": "Bearer x"}) is None


def test_parse_headers_is_case_insensitive():
    ident = dh.parse_headers({"x-device-tag": "FS-07", "X-App-Version": "1.4.2"})
    assert ident["asset_tag"] == "FS-07"
    assert ident["app_version"] == "1.4.2"


def test_untagged_device_becomes_an_unclaimed_row():
    ident = dh.parse_headers({"X-Device-Id": "a3f9c2d1e0b7"})
    assert ident["asset_tag"] == "unclaimed:a3f9c2d1"
    assert ident["device_uuid"] == "a3f9c2d1e0b7"


def test_tag_is_trimmed_and_uppercased():
    ident = dh.parse_headers({"X-Device-Tag": "  fs-07 "})
    assert ident["asset_tag"] == "FS-07"


def test_record_throttles_on_one_hour_but_not_on_account_or_version_change():
    conn = FakeConn()
    dh.record(conn, {"asset_tag": "FS-07", "device_uuid": "u1", "app_version": "1.4.2"}, "sub-1")
    sql, params = conn.cur.executed[0]
    assert "insert into devices" in sql.lower()
    assert "on conflict (asset_tag) do update" in sql.lower()
    assert "interval '1 hour'" in sql.lower()
    assert "last_account_sub is distinct from excluded.last_account_sub" in sql.lower()
    assert "app_version is distinct from excluded.app_version" in sql.lower()


def test_record_commits():
    conn = FakeConn()
    dh.record(conn, {"asset_tag": "FS-07", "device_uuid": "u1", "app_version": "1.4.2"}, "sub-1")
    assert conn.committed is True


def test_duplicate_uuid_across_tags_clears_trust():
    # the collision query reports two distinct tags sharing one uuid
    conn = FakeConn(results=[[("u1",)]])
    dh.record(conn, {"asset_tag": "FS-08", "device_uuid": "u1", "app_version": "1.4.2"}, "sub-1")
    joined = " ".join(sql for sql, _ in conn.cur.executed).lower()
    assert "uuid_trusted = false" in joined


def test_record_never_raises_when_the_database_fails():
    class Boom:
        def cursor(self, *a, **k):
            raise RuntimeError("connection reset")

    dh.record(Boom(), {"asset_tag": "FS-07", "device_uuid": "u1", "app_version": "1.4.2"}, "sub-1")
    # reaching this line without an exception is the assertion
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" pytest tests/unit/test_device_heartbeat.py -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'src.device_heartbeat'`.

- [ ] **Step 3: Write the module**

```python
# src/device_heartbeat.py
"""Device ledger heartbeat.

Rides the org-api request path: every request carrying device headers refreshes
that device's row. The upsert is conditional so an ordinary repeat heartbeat
modifies no row at all — but an account switch or a version change always writes,
because an account switch is the central event of a hand-over.

Nothing here may raise. A failure to record telemetry must never fail a user's
API request.
"""

import logging
import uuid as _uuid

logger = logging.getLogger()

_UPSERT = """
insert into devices (id, asset_tag, device_uuid, app_version, last_account_sub, last_seen_at)
values (%s, %s, %s, %s, %s, now())
on conflict (asset_tag) do update set
  last_seen_at     = now(),
  device_uuid      = coalesce(excluded.device_uuid, devices.device_uuid),
  app_version      = coalesce(excluded.app_version, devices.app_version),
  last_account_sub = coalesce(excluded.last_account_sub, devices.last_account_sub)
where devices.last_seen_at is null
   or devices.last_seen_at < now() - interval '1 hour'
   or devices.last_account_sub is distinct from excluded.last_account_sub
   or devices.app_version is distinct from excluded.app_version
"""

_COLLIDING_UUIDS = """
select device_uuid
from devices
where device_uuid = %s
group by device_uuid
having count(distinct asset_tag) > 1
"""

_DISTRUST = "update devices set uuid_trusted = false where device_uuid = %s"


def _get(headers, name):
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    value = lowered.get(name.lower())
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_headers(headers):
    """Extract device identity from request headers, or None if absent."""
    tag = _get(headers, "X-Device-Tag")
    device_uuid = _get(headers, "X-Device-Id")
    version = _get(headers, "X-App-Version")

    if not tag and not device_uuid:
        return None

    if tag:
        asset_tag = tag.upper()
    else:
        asset_tag = "unclaimed:" + device_uuid[:8]

    return {"asset_tag": asset_tag, "device_uuid": device_uuid, "app_version": version}


def record(conn, ident, account_sub):
    """Refresh a device's row. Never raises."""
    if not ident:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                _UPSERT,
                (
                    str(_uuid.uuid4()),
                    ident["asset_tag"],
                    ident.get("device_uuid"),
                    ident.get("app_version"),
                    account_sub,
                ),
            )
            if ident.get("device_uuid"):
                cur.execute(_COLLIDING_UUIDS, (ident["device_uuid"],))
                if cur.fetchall():
                    # One uuid under two tags: the ROM is reporting a shared
                    # identifier. The tag stays authoritative; the uuid is
                    # marked untrustworthy and never recovers.
                    cur.execute(_DISTRUST, (ident["device_uuid"],))
        conn.commit()
    except Exception:
        logger.exception("device heartbeat failed for %s", ident.get("asset_tag"))
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" pytest tests/unit/test_device_heartbeat.py -q
```
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add src/device_heartbeat.py tests/unit/test_device_heartbeat.py
git commit -m "feat(devices): heartbeat upsert with throttle and uuid collision detection"
```

---

## Task 3: Wire the heartbeat into org-api with two lines

**Files:**
- Modify: `src/lambda_org_api.py` — one import line, one call site
- Test: `tests/unit/test_org_api_device_heartbeat.py`

**Interfaces:**
- Consumes: `device_heartbeat.parse_headers`, `device_heartbeat.record` from Task 2.

Read `lambda_handler` first and place the call immediately after the database connection and caller identity are available, before routing. Keep it to a single statement guarded by a truthiness check so that a merge conflict, if one happens, is trivially resolvable.

- [ ] **Step 1: Write the failing test**

`lambda_handler` opens a real database connection, so these events will not run
unmodified. Read `tests/unit/test_org_api_sessions.py` first and reuse its
`make_event(...)` helper and its monkeypatching convention (repo functions are
patched onto `org.<module>`) — the events below show only the device-specific
parts that matter. Match that file's setup exactly rather than inventing a
second harness.

```python
# tests/unit/test_org_api_device_heartbeat.py
import src.lambda_org_api as org


def test_handler_records_a_heartbeat_when_device_headers_are_present(monkeypatch):
    seen = {}

    def fake_record(conn, ident, account_sub):
        seen["ident"] = ident
        seen["sub"] = account_sub

    monkeypatch.setattr(org.device_heartbeat, "record", fake_record)

    event = {
        "httpMethod": "GET",
        "path": "/api/org/me",
        "headers": {"X-Device-Tag": "FS-07", "X-App-Version": "1.4.2"},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }
    org.lambda_handler(event, None)

    assert seen["ident"]["asset_tag"] == "FS-07"


def test_handler_records_nothing_without_device_headers(monkeypatch):
    calls = []
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a: calls.append(a))

    event = {
        "httpMethod": "GET",
        "path": "/api/org/me",
        "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }
    org.lambda_handler(event, None)

    assert calls == []


def test_a_failing_heartbeat_does_not_fail_the_request(monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")

    monkeypatch.setattr(org.device_heartbeat, "record", boom)

    event = {
        "httpMethod": "GET",
        "path": "/api/org/me",
        "headers": {"X-Device-Tag": "FS-07"},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }
    resp = org.lambda_handler(event, None)
    assert resp["statusCode"] != 500
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 pytest tests/unit/test_org_api_device_heartbeat.py -q
```
Expected: FAIL — `AttributeError: module 'src.lambda_org_api' has no attribute 'device_heartbeat'`.

- [ ] **Step 3: Add the import**

Add next to the other local module imports at the top of `src/lambda_org_api.py`:

```python
import device_heartbeat
```

If the file's other local imports use a package-qualified form, match that form exactly rather than introducing a second convention.

- [ ] **Step 4: Add the single call site**

Inside `lambda_handler`, after the connection and caller sub exist and before routing:

```python
    _dev = device_heartbeat.parse_headers(event.get("headers") or {})
    if _dev:
        device_heartbeat.record(conn, _dev, caller_sub)
```

Use the names `lambda_handler` actually binds for the connection and the caller's sub — read them from the surrounding code rather than assuming `conn` / `caller_sub`. The third test above guards the failure path; `record` already swallows its own exceptions, so no try/except belongs here.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 pytest tests/unit/test_org_api_device_heartbeat.py -q
```
Expected: PASS, 3 tests.

- [ ] **Step 6: Run the whole org-api suite for regressions**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```
Expected: PASS. This file is being edited by three other branches tonight — a failure here is more likely to be someone else's landed change than your own; read the failure before assuming.

- [ ] **Step 7: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_org_api_device_heartbeat.py
git commit -m "feat(devices): record a heartbeat on every org-api request"
```

---

## Task 4: `device_ledger` — the in-VPC reader Lambda

**Files:**
- Create: `src/lambda_device_ledger.py`
- Test: `tests/unit/test_lambda_device_ledger.py`

**Interfaces:**
- Consumes: the `devices` and `recordings` tables.
- Produces: `lambda_handler(event, context) -> {"devices": [...]}`, each entry
  `{"asset_tag": str, "device_uuid": str | None, "uuid_trusted": bool, "app_version": str | None, "last_seen_at": str | None, "last_account_sub": str | None, "actual_site": str | None, "actual_company": str | None}`.
  `last_seen_at` is an ISO-8601 string in UTC or `None`. Task 5 consumes this shape.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lambda_device_ledger.py
import datetime as dt
import src.lambda_device_ledger as ledger


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.description = None

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self, *a, **k):
        return self.cur


def test_serialises_a_device_row(monkeypatch):
    seen = dt.datetime(2026, 8, 4, 2, 30, tzinfo=dt.timezone.utc)
    rows = [("FS-07", "u1", True, "1.4.2", seen, "sub-1", "UC PK", "UC Property")]
    monkeypatch.setattr(ledger, "_connect", lambda: FakeConn(rows))

    out = ledger.lambda_handler({}, None)

    assert out["devices"] == [{
        "asset_tag": "FS-07",
        "device_uuid": "u1",
        "uuid_trusted": True,
        "app_version": "1.4.2",
        "last_seen_at": "2026-08-04T02:30:00+00:00",
        "last_account_sub": "sub-1",
        "actual_site": "UC PK",
        "actual_company": "UC Property",
    }]


def test_a_device_that_has_never_been_seen_serialises_as_null(monkeypatch):
    rows = [("FS-11", None, True, None, None, None, None, None)]
    monkeypatch.setattr(ledger, "_connect", lambda: FakeConn(rows))

    out = ledger.lambda_handler({}, None)

    assert out["devices"][0]["last_seen_at"] is None
    assert out["devices"][0]["actual_site"] is None


def test_empty_ledger_returns_an_empty_list_not_an_error(monkeypatch):
    monkeypatch.setattr(ledger, "_connect", lambda: FakeConn([]))
    assert ledger.lambda_handler({}, None) == {"devices": []}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" pytest tests/unit/test_lambda_device_ledger.py -q
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the Lambda**

Mirror `lambda_finalize_claim.py`'s connection helper exactly — same env var names, same psycopg import — rather than inventing a second convention.

```python
# src/lambda_device_ledger.py
"""In-VPC leaf: reads the device ledger and returns it as JSON.

This function makes NO outbound calls. It cannot: there is no NAT gateway, so
anything in the VPC can reach Aurora and nothing else (BUG-36). The Notion and
Teams half runs in lambda_device_report, outside the VPC, which invokes this.
"""

import logging
import os

import psycopg

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_QUERY = """
select d.asset_tag,
       d.device_uuid,
       d.uuid_trusted,
       d.app_version,
       d.last_seen_at,
       d.last_account_sub,
       s.name  as actual_site,
       c.name  as actual_company
from devices d
left join lateral (
    select r.site_id
    from recordings r
    where r.device_id = d.id and r.site_id is not null
    order by r.created_at desc
    limit 1
) last_rec on true
left join sites     s on s.id = last_rec.site_id
left join companies c on c.id = s.company_id
order by d.last_seen_at asc nulls first, d.asset_tag asc
"""


def _connect():
    return psycopg.connect(
        host=os.environ["PGHOST"],
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        connect_timeout=10,
    )


def lambda_handler(event, context):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_QUERY)
            rows = cur.fetchall()

    devices = [
        {
            "asset_tag": r[0],
            "device_uuid": r[1],
            "uuid_trusted": r[2],
            "app_version": r[3],
            "last_seen_at": r[4].isoformat() if r[4] else None,
            "last_account_sub": r[5],
            "actual_site": r[6],
            "actual_company": r[7],
        }
        for r in rows
    ]
    logger.info("device ledger returning %d rows", len(devices))
    return {"devices": devices}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" pytest tests/unit/test_lambda_device_ledger.py -q
```
Expected: PASS, 3 tests.

- [ ] **Step 5: Confirm the join column names against the live schema**

The query assumes `sites.name`, `sites.company_id` and `companies.name`. Verify before deploying:

```bash
export MSYS_NO_PATHCONV=1
aws rds-data execute-statement \
  --resource-arn "$(aws rds describe-db-clusters --query 'DBClusters[?contains(DBClusterIdentifier,`fieldsight-db-test`)].DBClusterArn' --output text)" \
  --secret-arn "$DB_SECRET_ARN" --database fieldsight_test \
  --sql "select column_name from information_schema.columns where table_name in ('sites','companies') order by table_name, column_name" \
  --region ap-southeast-2
```
If a column differs, fix the query and the test's expected keys together.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_device_ledger.py tests/unit/test_lambda_device_ledger.py
git commit -m "feat(devices): in-VPC ledger reader lambda"
```

---

## Task 5: `device_report` — the non-VPC scheduler, inert without a token

**Files:**
- Create: `src/lambda_device_report.py`
- Test: `tests/unit/test_lambda_device_report.py`

**Interfaces:**
- Consumes: `lambda_device_ledger`'s return shape via `lambda:Invoke`.
- Produces: `lambda_handler(event, context) -> {"status": "disabled"|"ok", "devices": int}`.

Alert derivation and the Notion write land in Phase 3, once a real database and token exist. Tonight this function proves the hop: it invokes the in-VPC leaf, logs the shape, and returns. With `NOTION_TOKEN` unset it does not even do that.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lambda_device_report.py
import json
import src.lambda_device_report as report


class FakeLambdaClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)
        body = json.dumps(self._payload).encode()

        class Body:
            def read(self_inner):
                return body

        return {"StatusCode": 200, "Payload": Body()}


def test_disabled_without_a_notion_token(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "")
    client = FakeLambdaClient({"devices": []})
    monkeypatch.setattr(report, "_lambda", lambda: client)

    assert report.lambda_handler({}, None) == {"status": "disabled", "devices": 0}
    assert client.calls == []


def test_invokes_the_in_vpc_ledger_when_enabled(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "secret_x")
    monkeypatch.setattr(report, "LEDGER_FUNCTION", "fieldsight-test-device-ledger")
    client = FakeLambdaClient({"devices": [{"asset_tag": "FS-07"}]})
    monkeypatch.setattr(report, "_lambda", lambda: client)

    out = report.lambda_handler({}, None)

    assert out == {"status": "ok", "devices": 1}
    assert client.calls[0]["FunctionName"] == "fieldsight-test-device-ledger"


def test_a_ledger_failure_raises_so_the_lambda_is_marked_failed(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "secret_x")

    class Boom:
        def invoke(self, **kw):
            raise RuntimeError("ledger unreachable")

    monkeypatch.setattr(report, "_lambda", lambda: Boom())

    try:
        report.lambda_handler({}, None)
    except RuntimeError:
        return
    raise AssertionError("a ledger failure must surface, not be swallowed")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run --with pytest --with boto3 pytest tests/unit/test_lambda_device_report.py -q
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the Lambda**

```python
# src/lambda_device_report.py
"""Non-VPC scheduler for the device ledger.

Lives outside the VPC because it must reach Notion and Teams; it therefore
cannot reach Aurora, so it invokes the in-VPC lambda_device_ledger for data.
This is the same split as AskAgentFunction -> RagSearchFunction.

Phase 1 ships this inert: with NOTION_TOKEN unset it returns immediately.
Alert derivation and the Notion write arrive in Phase 3.

A ledger failure is deliberately allowed to raise. A silent partial run would
leave a Notion table that merely looks unchanged, which is the failure mode
this design exists to avoid.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
LEDGER_FUNCTION = os.environ.get("LEDGER_FUNCTION", "")

_client = None


def _lambda():
    global _client
    if _client is None:
        _client = boto3.client("lambda")
    return _client


def lambda_handler(event, context):
    if not NOTION_TOKEN:
        logger.info("device report disabled: NOTION_TOKEN unset")
        return {"status": "disabled", "devices": 0}

    resp = _lambda().invoke(
        FunctionName=LEDGER_FUNCTION,
        InvocationType="RequestResponse",
        Payload=b"{}",
    )
    payload = json.loads(resp["Payload"].read() or b"{}")
    devices = payload.get("devices") or []
    logger.info("device report received %d devices", len(devices))
    return {"status": "ok", "devices": len(devices)}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run --with pytest --with boto3 pytest tests/unit/test_lambda_device_report.py -q
```
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_device_report.py tests/unit/test_lambda_device_report.py
git commit -m "feat(devices): non-VPC report scheduler, inert without a Notion token"
```

---

## Task 6: Template resources, appended

**Files:**
- Modify: `src/template.yaml` — append two functions at the end of `Resources:`, add one `Parameter`

**Interfaces:**
- Consumes: `lambda_device_ledger.lambda_handler`, `lambda_device_report.lambda_handler`.
- Produces: functions `fieldsight-{stage}-device-ledger` and `fieldsight-{stage}-device-report`.

Both names begin with `fieldsight-`, so the deploy role's prefix grants cover the functions, their log groups, their execution roles and the schedule rule. Appending keeps the diff away from the regions other branches touch.

- [ ] **Step 1: Add the parameter**

In `Parameters:`, alongside the other secret-bearing parameters:

```yaml
  NotionToken:
    Type: String
    Default: ''
    NoEcho: true
    Description: >
      Notion integration token for the device ledger sync. Empty disables the
      sync entirely (lambda_device_report returns immediately), which is the
      Phase 1 state.
```

- [ ] **Step 2: Append the two functions at the very end of `Resources:`**

Copy `FinalizeSweepFunction`'s `Layers` / `VpcConfig` / PG env block verbatim for the ledger function — same subnets, same security group import, same secret resolution — so it inherits a configuration already proven to connect.

```yaml
  # --- Device management (spec 2026-08-03) -----------------------------------
  # Two hops on purpose: no NAT gateway means an in-VPC function reaches Aurora
  # and nothing else, while an outbound function reaches Notion and not Aurora.
  DeviceLedgerFunction:
    Type: AWS::Serverless::Function
    Condition: HasDb
    Properties:
      FunctionName: !Sub ["${P}-device-ledger", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_device_ledger.lambda_handler
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
          PGDATABASE: !If [HasPgDatabaseOverride, !Ref PgDatabase, !ImportValue {"Fn::Sub": "${DbStackName}-DbName"}]
          PGUSER: postgres
          PGPASSWORD: !Sub '{{resolve:secretsmanager:${DbSecretArn}:SecretString:password}}'
      Policies:
        - VPCAccessPolicy: {}

  DeviceReportFunction:
    Type: AWS::Serverless::Function
    Condition: HasDb
    Properties:
      FunctionName: !Sub ["${P}-device-report", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_device_report.lambda_handler
      Timeout: 120
      MemorySize: 256
      Environment:
        Variables:
          NOTION_TOKEN: !Ref NotionToken
          LEDGER_FUNCTION: !Ref DeviceLedgerFunction
      Policies:
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: lambda:InvokeFunction
              Resource: !GetAtt DeviceLedgerFunction.Arn
      Events:
        DailyDeviceReport:
          Type: Schedule
          Properties:
            # 16:00 UTC = 04:00 NZST, before anyone looks in the morning.
            Schedule: cron(0 16 * * ? *)
            Description: Device ledger sync to Notion.
            State: !If [ShouldEnableSchedules, ENABLED, DISABLED]
```

- [ ] **Step 3: Validate the template**

```bash
export MSYS_NO_PATHCONV=1 AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1
sam validate --template src/template.yaml --region ap-southeast-2 --lint
```
Expected: `is a valid SAM Template`. A `HasDb` / `PsycopgLayer` / `DbSubnetIds` not-found error means a name differs from `FinalizeSweepFunction`'s — copy from there rather than guessing.

- [ ] **Step 4: Confirm the deploy role can create these exact ARNs**

```bash
export MSYS_NO_PATHCONV=1
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::509194952652:role/github-actions-fieldsight-deploy \
  --action-names lambda:CreateFunction logs:CreateLogGroup \
  --resource-arns \
    arn:aws:lambda:ap-southeast-2:509194952652:function:fieldsight-test-device-ledger \
    arn:aws:logs:ap-southeast-2:509194952652:log-group:/aws/lambda/fieldsight-test-device-report \
  --query 'EvaluationResults[].[EvalActionName,EvalResourceName,EvalDecision]' --output text
```
Expected: `allowed` on every row. Anything else means the stack will roll back — fix the IAM before pushing, never after.

- [ ] **Step 5: Commit**

```bash
git add src/template.yaml
git commit -m "feat(devices): device-ledger and device-report functions"
```

---

## Task 7: Deploy to TEST and verify

- [ ] **Step 1: Push the branch and open a PR into `develop`**

```bash
git push -u origin feat/device-management-phase1
gh pr create --base develop --head feat/device-management-phase1 \
  --title "feat(devices): device ledger phase 1 — schema, heartbeat, inert Notion sync" \
  --body "Backend half of docs/superpowers/specs/2026-08-03-device-management-design.md. Ships inert: NOTION_TOKEN is unset so device-report returns immediately. Touches lambda_org_api.py on two lines only."
```

- [ ] **Step 2: Merge to `develop` only after Task 0's back-merge has landed**

Order matters. If this merges first, the back-merge PR has to resolve against a file this task just touched.

- [ ] **Step 3: Watch the TEST deploy**

```bash
gh run list --workflow deploy.yml --limit 3
gh run watch <run-id>
```
Expected: success. A `CREATE_FAILED` on either new function means Step 4 of Task 6 was skipped or lied — read the stack events, do not retry blindly.

- [ ] **Step 4: Confirm the migration ran and the table exists**

```bash
export MSYS_NO_PATHCONV=1
aws logs tail /aws/lambda/fieldsight-test-migrate --since 15m --region ap-southeast-2 | grep -i 0030
```
Expected: a line showing `0030_devices` applied.

- [ ] **Step 5: Prove the heartbeat end to end**

```bash
export MSYS_NO_PATHCONV=1   # without this the path argument is rewritten (BUG-42)
python scripts/invoke_org_api.py --stage test --method GET --path /api/org/me \
  --header 'X-Device-Tag: FS-99' --header 'X-Device-Id: testuuid00001' --header 'X-App-Version: 0.0.1-test'
```
Then read the row back through the ledger function:

```bash
aws lambda invoke --function-name fieldsight-test-device-ledger \
  --payload '{}' --cli-binary-format raw-in-base64-out \
  --region ap-southeast-2 /tmp/ledger.json && cat /tmp/ledger.json
```
Expected: a `devices` array containing `FS-99` with a `last_seen_at` from the last few minutes and `app_version` `0.0.1-test`.

- [ ] **Step 6: Prove the report function is genuinely inert**

```bash
aws lambda invoke --function-name fieldsight-test-device-report \
  --payload '{}' --cli-binary-format raw-in-base64-out \
  --region ap-southeast-2 /tmp/report.json && cat /tmp/report.json
```
Expected: `{"status": "disabled", "devices": 0}`.

- [ ] **Step 7: Confirm nothing else on TEST regressed**

The heartbeat sits on the org-api hot path, so a mistake there breaks every dashboard call. Open the TEST dashboard in the browser, sign in, and confirm the site list, Today and a report all still load. Check the browser console for 4xx/5xx on `/api/org/*`. If browser automation is unavailable, fall back to:

```bash
python scripts/invoke_org_api.py --stage test --method GET --path /api/org/sites
python scripts/invoke_org_api.py --stage test --method GET --path /api/org/me
```
Expected: both return 200 with their usual bodies.

- [ ] **Step 8: Clean up the smoke-test row**

`FS-99` is not a real device and would show up as an unclaimed row. Remove it once the checks pass, or leave it and note it — but do not let it silently become part of the ledger.

---

## Not in this plan

Phase 2 (mobile `DeviceIdentity`, the interceptor, Settings, logout hygiene) is a separate repo and a separate release; it cannot ship tonight and is not blocked by anything here. Phase 3 (alert derivation, the Notion write, Teams and email) needs a Notion database and an integration token that do not yet exist, and needs a month of real `last_seen_at` data before `QUIET_ALERT_WORKING_DAYS` can be set honestly.

Prod deployment of Phase 1 is deliberately excluded. Nothing here is useful on prod until the app sends headers, and tonight's prod release should carry only the programme work that is already tested.
