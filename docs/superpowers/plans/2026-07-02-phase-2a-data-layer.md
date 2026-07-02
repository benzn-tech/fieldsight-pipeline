# Phase 2A — Postgres Data Layer (schema + migrations + repos + tests) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the FieldSight relational + vector data layer (schema, migration system, repositories, ACL-filtered vector search) as pure code + SQL that runs against any PostgreSQL+pgvector, testable now without provisioning Aurora.

**Architecture:** Versioned `.sql` migrations applied by a small runner tracking a `schema_migrations` table. Data access is raw `psycopg` (v3) through thin per-aggregate repository functions that receive a connection (no ORM). Pure logic (migration ordering, ACL scope resolution, search-SQL construction) lives in driver-free modules that never import `psycopg`, so it unit-tests locally with no database. Database behavior is verified by integration tests against a `pgvector/pgvector` Postgres — run in CI (and locally if a Postgres is available).

**Tech Stack:** Python 3.11 (Lambda + CI) / 3.14 (local unit tests only), `psycopg[binary]` v3, `pgvector` (python), PostgreSQL 16 + pgvector extension, pytest, GitHub Actions.

## Global Constraints

- **Target account is the user's own SAM pipeline `509194952652`** (repo `benzn-tech/fieldsight-pipeline`). This is NOT the company CDK account `164088480050` — do not touch or reference that account's resources.
- All backend code lives under `src/`; all tests under `tests/`. SAM `CodeUri: src/` bundles everything under `src/`, so **`src/` is the import root at Lambda runtime** — application modules import each other WITHOUT a `src.` prefix (e.g. `from db.migrate import ...`). Tests match this by putting `src/` on the path via pytest `pythonpath = ["src"]`. **Do not create `src/__init__.py`** (it must stay a path root, not a package).
- **Data access = raw `psycopg` v3 + thin repository functions. No ORM. No SQLAlchemy. No Alembic.**
- **Migrations = versioned `.sql` files** in `src/migrations/`, filename `NNNN_name.sql` (4-digit zero-padded). Applied in ascending numeric order; each recorded in `schema_migrations(version, applied_at)`.
- **Embedding dimension is fixed at 1024** (Bedrock Amazon Titan Text Embeddings V2). Vector column type is `vector(1024)`; index is pgvector **HNSW** with `vector_cosine_ops`.
- **Phase 2A does NOT call Bedrock or generate embeddings.** Repositories accept a pre-computed embedding (`list[float]` length 1024) as input. Embedding generation is Phase 5.
- **Pure-logic modules MUST NOT import `psycopg`/`pgvector` at module top level.** Only `db/connection.py`, the repositories' DB functions, and integration-test fixtures import them. Pure functions with LOCAL unit tests (`resolve_scope`, `build_search_sql`) live in dedicated driver-free modules (`repositories/acl.py`, `repositories/search_sql.py`) so `pytest -m "not integration"` runs on local Python 3.14 with no database and no psycopg wheel.
- **Integration tests are marked `@pytest.mark.integration`** and skip automatically when `TEST_DATABASE_URL` is unset. Local default loop = unit tests; DB tests = CI (or local when a Postgres is provided).
- **ACL is deny-by-default:** every cross-site read query filters `WHERE site_id = ANY(%(site_ids)s)` with an explicit list. `admin`/`gm` are expanded to the full site-id list by a query, never by dropping the filter.
- **Windows/git:** `core.autocrlf=true`, mixed CRLF/LF. Use single-line anchors when editing existing files. **Never `git add -A`** — stage explicit paths. **Do not commit to `develop` or `main` directly**; all work on a feature branch.
- **CI:** the new `.github/workflows/test.yml` is the repo's FIRST automated test gate (current `deploy.yml` has none). Do not modify `deploy.yml` in this phase.
- Frequent commits; conventional-commit messages. UTC for stored timestamps (`timestamptz`).

---

## File Structure

```
src/
  db/
    __init__.py
    migrate.py          # pure pending_versions() + apply_migrations(conn, dir)
    connection.py       # get_connection() — imports psycopg/pgvector
  repositories/
    __init__.py
    acl.py              # resolve_scope() — DRIVER-FREE (pure)
    search_sql.py       # build_search_sql() — DRIVER-FREE (pure)
    companies.py
    users.py
    sites.py
    memberships.py      # add_membership + accessible_site_ids (re-exports resolve_scope)
    topics.py           # upsert_topic (+ children) / list_site_topics / get_topic_photos
    chunks.py           # insert_chunk / search_chunks (uses search_sql.build_search_sql)
  migrations/
    0001_extensions.sql
    0002_core_relational.sql
    0003_dashboard_readmodel.sql
    0004_report_chunks.sql
  lambda_migrate.py     # in-VPC migration Lambda handler (wired by Phase 2B)
tests/
  conftest.py           # DB fixtures (lazy psycopg import), integration skip logic
  unit/
    test_migrate_ordering.py
    test_acl_scope.py
    test_chunk_search_sql.py
    test_lambda_migrate.py
  integration/
    test_migrations_apply.py
    test_connection.py
    test_core_repositories.py
    test_memberships_acl.py
    test_topics_repository.py
    test_chunk_search.py
.github/workflows/
  test.yml              # NEW — pytest + pgvector service (first CI gate)
pyproject.toml          # add psycopg[binary], pgvector; pytest markers + pythonpath
```

**Import convention (critical):** application modules and tests import top-level packages `db`, `repositories`, and top-level modules like `lambda_migrate` — never `src.db` / `src.repositories`. This is what works at Lambda runtime (where `src/` is the bundle root) and in tests (via `pythonpath = ["src"]`).

---

### Task 1: Test scaffolding + first CI gate

Establishes pytest, `pythonpath`, the integration-skip mechanism, dependencies, and the repo's first CI test workflow. Everything after this task plugs into it.

**Files:**
- Modify: `pyproject.toml` (add deps, pytest config with `pythonpath`)
- Create: `tests/__init__.py`, `tests/unit/__init__.py`, `tests/integration/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/test_sanity.py`
- Create: `.github/workflows/test.yml`

**Interfaces:**
- Produces: pytest marker `integration`; fixtures `migrated_db_url` (str) and `db` (a `psycopg` connection wrapped in a per-test transaction that rolls back). Both skip when `TEST_DATABASE_URL` is unset. `pythonpath = ["src"]` makes `db`/`repositories`/`lambda_migrate` importable.

- [ ] **Step 1: Create a feature branch off `develop`**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" fetch origin
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" checkout develop
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" pull --ff-only
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" checkout -b feature/phase2a-data-layer
```

- [ ] **Step 2: Add dependencies and pytest config to `pyproject.toml`**

Merge these blocks (keep existing content):

```toml
[project.optional-dependencies]
dev = [
  "pytest>=7.0",
  "pytest-cov>=4.0",
  "psycopg[binary]>=3.1",
  "pgvector>=0.3",
  "ruff>=0.4",
  "mypy>=1.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
markers = [
  "integration: touches a real PostgreSQL (skipped unless TEST_DATABASE_URL is set)",
]
```

- [ ] **Step 3: Create the test package files and conftest**

`tests/__init__.py`, `tests/unit/__init__.py`, `tests/integration/__init__.py` — each empty.

`tests/conftest.py` (psycopg imported lazily *inside* fixtures only):

```python
import os
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_needs_db = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set; skipping DB integration test"
)


def pytest_collection_modifyitems(config, items):
    # Auto-skip anything marked 'integration' when no test DB is configured.
    for item in items:
        if "integration" in item.keywords and not TEST_DB_URL:
            item.add_marker(_needs_db)


@pytest.fixture(scope="session")
def migrated_db_url():
    """Apply all migrations once against the test DB; return its URL."""
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    from db.migrate import apply_migrations

    migrations_dir = os.path.join(os.path.dirname(__file__), "..", "src", "migrations")
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        apply_migrations(conn, os.path.abspath(migrations_dir))
    return TEST_DB_URL


@pytest.fixture
def db(migrated_db_url):
    """A connection whose work is rolled back after each test (isolation)."""
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(migrated_db_url)
    register_vector(conn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
```

- [ ] **Step 4: Write a sanity unit test**

`tests/unit/test_sanity.py`:

```python
def test_sanity():
    assert 1 + 1 == 2
```

- [ ] **Step 5: Run unit tests locally to verify the harness works**

Run: `python -m pytest -m "not integration" -v`
Expected: `test_sanity` PASSES; no integration tests run; no import errors (psycopg not required).
(If pytest is missing locally: `python -m pip install pytest` first.)

- [ ] **Step 6: Create the CI test workflow**

`.github/workflows/test.yml`:

```yaml
name: Tests
on:
  push:
    branches: ["feature/**"]
  pull_request:
    branches: [develop, main]

jobs:
  pytest:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: fieldsight
          POSTGRES_PASSWORD: fieldsight
          POSTGRES_DB: fieldsight_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U fieldsight"
          --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      TEST_DATABASE_URL: postgresql://fieldsight:fieldsight@localhost:5432/fieldsight_test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python -m pip install --upgrade pip
      - run: pip install "psycopg[binary]>=3.1" "pgvector>=0.3" pytest pytest-cov
      - run: python -m pytest -v
```

(Explicit dep install — no `pip install -e .` — avoids depending on the repo's packaging config; `pythonpath = ["src"]` handles imports.)

- [ ] **Step 7: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add pyproject.toml tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py tests/conftest.py tests/unit/test_sanity.py .github/workflows/test.yml
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "test: add pytest harness, pgvector CI gate, DB fixtures"
```

---

### Task 2: Migration runner — pure ordering logic

The database-free core: given migration files present and versions applied, decide what to run and in what order. Fully unit-testable locally.

**Files:**
- Create: `src/db/__init__.py`
- Create: `src/db/migrate.py`
- Test: `tests/unit/test_migrate_ordering.py`

**Interfaces:**
- Produces: `pending_versions(all_files: list[str], applied: set[str]) -> list[str]` (filenames not in `applied`, sorted ascending by 4-digit prefix); `parse_version(filename: str) -> int`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_migrate_ordering.py`:

```python
from db.migrate import pending_versions, parse_version


def test_parse_version_reads_numeric_prefix():
    assert parse_version("0003_dashboard_readmodel.sql") == 3


def test_pending_versions_orders_and_filters():
    files = ["0002_core.sql", "0001_extensions.sql", "0003_read.sql"]
    applied = {"0001_extensions.sql"}
    assert pending_versions(files, applied) == ["0002_core.sql", "0003_read.sql"]


def test_pending_versions_empty_when_all_applied():
    files = ["0001_extensions.sql"]
    assert pending_versions(files, {"0001_extensions.sql"}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_migrate_ordering.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db.migrate'`.

- [ ] **Step 3: Write minimal implementation**

`src/db/__init__.py` — empty.

`src/db/migrate.py`:

```python
"""Versioned .sql migration runner. No ORM; no psycopg import at module top."""
import os


def parse_version(filename: str) -> int:
    return int(filename.split("_", 1)[0])


def pending_versions(all_files: list[str], applied: set[str]) -> list[str]:
    todo = [f for f in all_files if f.endswith(".sql") and f not in applied]
    return sorted(todo, key=parse_version)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_migrate_ordering.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/db/__init__.py src/db/migrate.py tests/unit/test_migrate_ordering.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: migration version ordering (pure)"
```

---

### Task 3: Migration runner — apply against a database

Adds DB-touching `apply_migrations`: ensures `schema_migrations` exists, reads applied versions, runs each pending file, records it. Integration-tested in CI.

**Files:**
- Modify: `src/db/migrate.py`
- Test: `tests/integration/test_migrations_apply.py`

**Interfaces:**
- Consumes: `pending_versions`, `parse_version` (Task 2).
- Produces: `apply_migrations(conn, migrations_dir: str) -> list[str]` (returns filenames applied this call); `applied_versions(conn) -> set[str]`. `conn` exposes psycopg-style `.execute(sql[, params])`.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_migrations_apply.py`:

```python
import os
import pytest
import psycopg
from db.migrate import apply_migrations, applied_versions

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
)


def _fresh_conn():
    """Autocommit connection on a wiped public schema. CI DB is ephemeral."""
    url = os.environ["TEST_DATABASE_URL"]
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    return conn


def test_apply_is_idempotent_and_records_versions():
    conn = _fresh_conn()
    try:
        first = apply_migrations(conn, MIGRATIONS_DIR)
        assert first, "expected at least one migration applied on empty DB"
        assert "0001_extensions.sql" in first
        second = apply_migrations(conn, MIGRATIONS_DIR)
        assert second == [], "re-running must apply nothing"
        assert "0001_extensions.sql" in applied_versions(conn)
    finally:
        conn.close()
```

Note: each test here wipes then re-applies migrations, always leaving a fully-migrated schema; the CI Postgres is a dedicated ephemeral service, so this does not disturb other tests' expectations.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_migrations_apply.py -v` (skips locally; runs in CI)
Expected (CI): FAIL — `ImportError: cannot import name 'apply_migrations'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/db/migrate.py`:

```python
def applied_versions(conn) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def apply_migrations(conn, migrations_dir: str) -> list[str]:
    done = applied_versions(conn)
    all_files = os.listdir(migrations_dir)
    applied_now: list[str] = []
    for fname in pending_versions(all_files, done):
        with open(os.path.join(migrations_dir, fname), "r", encoding="utf-8") as fh:
            sql = fh.read()
        conn.execute(sql)  # no params -> simple query protocol -> multi-statement OK
        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (fname,))
        applied_now.append(fname)
    return applied_now
```

Important: `conn.execute(sql)` is called with **no parameters**, so psycopg v3 uses the simple query protocol, which permits multiple `;`-separated statements in one migration file. The `INSERT ... VALUES (%s)` call is a separate parameterized statement. Callers use `autocommit=True` connections (see conftest / Task 8) so DDL like `CREATE EXTENSION` / `CREATE INDEX` commits cleanly.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_migrations_apply.py -v`
Expected (CI): PASS once migration files exist (Tasks 4–7). If executing strictly in order, create `0001` (Task 4) before running this file's tests, or run Tasks 4–7 then this.

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/db/migrate.py tests/integration/test_migrations_apply.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: apply migrations with schema_migrations tracking"
```

---

### Task 4: Migration 0001 — extensions

Enables `vector` (pgvector) and `pgcrypto` (for `gen_random_uuid()`).

**Files:**
- Create: `src/migrations/0001_extensions.sql`
- Test: `tests/integration/test_migrations_apply.py` (append)

**Interfaces:**
- Produces: DB has extensions `vector` and `pgcrypto` after migration.

- [ ] **Step 1: Write the failing test (append)**

Append to `tests/integration/test_migrations_apply.py`:

```python
def test_extensions_installed():
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        names = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        assert "vector" in names
        assert "pgcrypto" in names
    finally:
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_migrations_apply.py::test_extensions_installed -v`
Expected (CI): FAIL — extensions missing / file not found.

- [ ] **Step 3: Write the migration**

`src/migrations/0001_extensions.sql`:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_migrations_apply.py::test_extensions_installed -v`
Expected (CI): PASS.

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/migrations/0001_extensions.sql tests/integration/test_migrations_apply.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: migration 0001 — vector + pgcrypto extensions"
```

---

### Task 5: Migration 0002 — core relational tables

`companies`, `users`, `sites`, `memberships`. Cognito stays the auth system; `users` is the app profile keyed by `cognito_sub`.

**Files:**
- Create: `src/migrations/0002_core_relational.sql`
- Test: `tests/integration/test_core_repositories.py` (schema assertions; repo CRUD in Task 9)

**Interfaces:**
- Produces tables: `companies(id, name, industry, created_at)`; `users(id, cognito_sub UNIQUE, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at)`; `sites(id, company_id, name, location, client, industry, icon_s3_key, created_at)`; `memberships(id, user_id, site_id, role, created_at, UNIQUE(user_id, site_id))`. All `id uuid PRIMARY KEY DEFAULT gen_random_uuid()`.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_core_repositories.py`:

```python
import pytest

pytestmark = pytest.mark.integration


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_core_tables_exist_with_key_columns(db):
    assert {"id", "name", "industry", "created_at"} <= _columns(db, "companies")
    assert {"id", "cognito_sub", "company_id", "email", "global_role"} <= _columns(db, "users")
    assert {"id", "company_id", "name", "location", "icon_s3_key"} <= _columns(db, "sites")
    assert {"id", "user_id", "site_id", "role"} <= _columns(db, "memberships")


def test_membership_unique_user_site(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role) "
        "VALUES ('sub1', %s, 'a@x.com', 'worker') RETURNING id", (cid,)).fetchone()[0]
    sid = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id", (cid,)).fetchone()[0]
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'worker')", (uid, sid))
    with pytest.raises(Exception):
        db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'pm')", (uid, sid))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_core_repositories.py -v`
Expected (CI): FAIL — relations do not exist.

- [ ] **Step 3: Write the migration**

`src/migrations/0002_core_relational.sql`:

```sql
CREATE TABLE companies (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  industry    text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cognito_sub   text NOT NULL UNIQUE,
  company_id    uuid REFERENCES companies(id),
  email         text NOT NULL,
  first_name    text,
  last_name     text,
  avatar_s3_key text,
  global_role   text NOT NULL DEFAULT 'worker',
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sites (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id  uuid NOT NULL REFERENCES companies(id),
  name        text NOT NULL,
  location    text,
  client      text,
  industry    text,
  icon_s3_key text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  role        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, site_id)
);

CREATE INDEX idx_memberships_user ON memberships (user_id);
CREATE INDEX idx_memberships_site ON memberships (site_id);
CREATE INDEX idx_sites_company ON sites (company_id);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_core_repositories.py -v`
Expected (CI): PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/migrations/0002_core_relational.sql tests/integration/test_core_repositories.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: migration 0002 — companies/users/sites/memberships"
```

---

### Task 6: Migration 0003 — dashboard read model

`topics` + children `action_items`, `safety_observations`, `topic_photos`. Phase 4 populates; Phase 2A only defines.

**Files:**
- Create: `src/migrations/0003_dashboard_readmodel.sql`
- Test: `tests/integration/test_topics_repository.py` (schema assertions; behavior in Task 11)

**Interfaces:**
- Produces tables: `topics(id, site_id, user_id, source_s3_key, report_date, occurred_at, category, title, summary, created_at)`; `action_items(id, topic_id, site_id, text, responsible, deadline, priority, status, created_at)`; `safety_observations(id, topic_id, site_id, observation, risk_level, location, status, created_at)`; `topic_photos(id, topic_id, s3_key, caption_text, created_at)`.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_topics_repository.py`:

```python
import pytest

pytestmark = pytest.mark.integration


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_readmodel_tables_exist(db):
    assert {"id", "site_id", "report_date", "category", "title", "summary"} <= _columns(db, "topics")
    assert {"id", "topic_id", "site_id", "text", "status", "deadline"} <= _columns(db, "action_items")
    assert {"id", "topic_id", "site_id", "observation", "risk_level"} <= _columns(db, "safety_observations")
    assert {"id", "topic_id", "s3_key", "caption_text"} <= _columns(db, "topic_photos")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_topics_repository.py::test_readmodel_tables_exist -v`
Expected (CI): FAIL — relations do not exist.

- [ ] **Step 3: Write the migration**

`src/migrations/0003_dashboard_readmodel.sql`:

```sql
CREATE TABLE topics (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id       uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  user_id       uuid REFERENCES users(id),
  source_s3_key text,
  report_date   date NOT NULL,
  occurred_at   timestamptz,
  category      text,
  title         text NOT NULL,
  summary       text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE action_items (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  text        text NOT NULL,
  responsible text,
  deadline    date,
  priority    text,
  status      text NOT NULL DEFAULT 'open',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE safety_observations (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id    uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id     uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  observation text NOT NULL,
  risk_level  text,
  location    text,
  status      text NOT NULL DEFAULT 'open',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE topic_photos (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id     uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  s3_key       text NOT NULL,
  caption_text text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_topics_site_date ON topics (site_id, report_date);
CREATE INDEX idx_action_items_site_status ON action_items (site_id, status);
CREATE INDEX idx_safety_site_status ON safety_observations (site_id, status);
CREATE INDEX idx_topic_photos_topic ON topic_photos (topic_id);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_topics_repository.py::test_readmodel_tables_exist -v`
Expected (CI): PASS.

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/migrations/0003_dashboard_readmodel.sql tests/integration/test_topics_repository.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: migration 0003 — dashboard read model tables"
```

---

### Task 7: Migration 0004 — report_chunks + HNSW index

The semantic-search store: chunks + `vector(1024)` + metadata, co-located with relational tables so ACL filtering is one SQL join. HNSW cosine index.

**Files:**
- Create: `src/migrations/0004_report_chunks.sql`
- Test: `tests/integration/test_chunk_search.py` (schema + vector insert; search in Task 12)

**Interfaces:**
- Produces table: `report_chunks(id, site_id, user_id, source_s3_key, topic_id, report_date, chunk_type, chunk_text, embedding vector(1024), metadata jsonb, created_at)` with HNSW index `idx_report_chunks_embedding` (`vector_cosine_ops`) and btree `(site_id, report_date)`.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_chunk_search.py`:

```python
import pytest

pytestmark = pytest.mark.integration


def _seed_site(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    return db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'S') RETURNING id", (cid,)).fetchone()[0]


def test_report_chunks_accepts_1024_vector(db):
    site_id = _seed_site(db)
    vec = [0.0] * 1024
    vec[0] = 1.0
    db.execute(
        "INSERT INTO report_chunks (site_id, report_date, chunk_type, chunk_text, embedding) "
        "VALUES (%s, '2026-07-02', 'topic', 'hello', %s)",
        (site_id, vec),
    )
    n = db.execute("SELECT count(*) FROM report_chunks").fetchone()[0]
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_chunk_search.py::test_report_chunks_accepts_1024_vector -v`
Expected (CI): FAIL — relation `report_chunks` does not exist.

- [ ] **Step 3: Write the migration**

`src/migrations/0004_report_chunks.sql`:

```sql
CREATE TABLE report_chunks (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id       uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  user_id       uuid REFERENCES users(id),
  source_s3_key text,
  topic_id      uuid REFERENCES topics(id) ON DELETE SET NULL,
  report_date   date NOT NULL,
  chunk_type    text NOT NULL,
  chunk_text    text NOT NULL,
  embedding     vector(1024) NOT NULL,
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_report_chunks_embedding
  ON report_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX idx_report_chunks_site_date ON report_chunks (site_id, report_date);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_chunk_search.py::test_report_chunks_accepts_1024_vector -v`
Expected (CI): PASS. (The `db` fixture calls `register_vector`, so a Python list binds to `vector`.)

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/migrations/0004_report_chunks.sql tests/integration/test_chunk_search.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: migration 0004 — report_chunks + HNSW cosine index"
```

---

### Task 8: `db/connection.py` — psycopg connection factory

The module that imports `psycopg`/`pgvector`. Builds a connection from `DATABASE_URL` and registers the vector type. Used by the migration Lambda (2B) and write API (Phase 3); repositories receive the connection.

**Files:**
- Create: `src/db/connection.py`
- Test: `tests/integration/test_connection.py`

**Interfaces:**
- Produces: `get_connection(dsn: str | None = None, autocommit: bool = False)` — returns a `psycopg` connection with `pgvector` registered; `dsn` defaults to `os.environ["DATABASE_URL"]`.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_connection.py`:

```python
import pytest
from db.connection import get_connection

pytestmark = pytest.mark.integration


def test_get_connection_runs_query_and_has_vector(migrated_db_url):
    conn = get_connection(migrated_db_url, autocommit=True)
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        conn.execute("SELECT %s::vector", ([0.0, 1.0, 2.0],))  # vector type usable
    finally:
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_connection.py -v`
Expected (CI): FAIL — `ModuleNotFoundError: No module named 'db.connection'`.

- [ ] **Step 3: Write minimal implementation**

`src/db/connection.py`:

```python
"""Imports psycopg/pgvector. Repositories receive a connection from here."""
import os
import psycopg
from pgvector.psycopg import register_vector


def get_connection(dsn: str | None = None, autocommit: bool = False):
    dsn = dsn or os.environ["DATABASE_URL"]
    conn = psycopg.connect(dsn, autocommit=autocommit)
    register_vector(conn)
    return conn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_connection.py -v`
Expected (CI): PASS.

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/db/connection.py tests/integration/test_connection.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: psycopg connection factory with pgvector registration"
```

---

### Task 9: Repositories — companies, users, sites

Thin CRUD taking a connection. `upsert_user` is keyed by `cognito_sub`.

**Files:**
- Create: `src/repositories/__init__.py`
- Create: `src/repositories/companies.py`, `src/repositories/users.py`, `src/repositories/sites.py`
- Test: `tests/integration/test_core_repositories.py` (append)

**Interfaces:**
- Consumes: a psycopg connection.
- Produces:
  - `companies.create_company(conn, name, industry=None) -> dict`
  - `users.upsert_user(conn, cognito_sub, email, company_id=None, first_name=None, last_name=None, global_role='worker') -> dict`; `users.get_user_by_sub(conn, cognito_sub) -> dict | None`
  - `sites.create_site(conn, company_id, name, location=None, client=None, industry=None, icon_s3_key=None) -> dict`; `sites.get_site(conn, site_id) -> dict | None`; `sites.list_company_sites(conn, company_id) -> list[dict]`

- [ ] **Step 1: Write the failing test (append)**

Append to `tests/integration/test_core_repositories.py`:

```python
from repositories import companies, users, sites


def test_company_user_site_roundtrip(db):
    co = companies.create_company(db, "Acme", industry="construction")
    assert co["name"] == "Acme" and co["id"]

    u1 = users.upsert_user(db, "sub-9", "a@acme.com", company_id=co["id"], global_role="pm")
    u2 = users.upsert_user(db, "sub-9", "a@acme.com", company_id=co["id"], first_name="Ann")
    assert u1["id"] == u2["id"], "upsert by cognito_sub must not create a duplicate"
    assert users.get_user_by_sub(db, "sub-9")["first_name"] == "Ann"

    s = sites.create_site(db, co["id"], "North Wharf", location="Auckland")
    assert sites.get_site(db, s["id"])["name"] == "North Wharf"
    assert [x["id"] for x in sites.list_company_sites(db, co["id"])] == [s["id"]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_core_repositories.py::test_company_user_site_roundtrip -v`
Expected (CI): FAIL — `ModuleNotFoundError: No module named 'repositories'`.

- [ ] **Step 3: Write minimal implementation**

`src/repositories/__init__.py` — empty.

`src/repositories/companies.py`:

```python
from psycopg.rows import dict_row


def create_company(conn, name, industry=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO companies (name, industry) VALUES (%s, %s) "
        "RETURNING id, name, industry, created_at",
        (name, industry),
    ).fetchone()
```

`src/repositories/users.py`:

```python
from psycopg.rows import dict_row

_COLS = "id, cognito_sub, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at"


def upsert_user(conn, cognito_sub, email, company_id=None, first_name=None,
                last_name=None, global_role="worker") -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO users (cognito_sub, email, company_id, first_name, last_name, global_role) "
        f"VALUES (%s, %s, %s, %s, %s, %s) "
        f"ON CONFLICT (cognito_sub) DO UPDATE SET "
        f"  email=EXCLUDED.email, company_id=EXCLUDED.company_id, "
        f"  first_name=COALESCE(EXCLUDED.first_name, users.first_name), "
        f"  last_name=COALESCE(EXCLUDED.last_name, users.last_name), "
        f"  global_role=EXCLUDED.global_role "
        f"RETURNING {_COLS}",
        (cognito_sub, email, company_id, first_name, last_name, global_role),
    ).fetchone()


def get_user_by_sub(conn, cognito_sub) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE cognito_sub=%s", (cognito_sub,)
    ).fetchone()
```

`src/repositories/sites.py`:

```python
from psycopg.rows import dict_row

_COLS = "id, company_id, name, location, client, industry, icon_s3_key, created_at"


def create_site(conn, company_id, name, location=None, client=None,
                industry=None, icon_s3_key=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO sites (company_id, name, location, client, industry, icon_s3_key) "
        f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
        (company_id, name, location, client, industry, icon_s3_key),
    ).fetchone()


def get_site(conn, site_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE id=%s", (site_id,)
    ).fetchone()


def list_company_sites(conn, company_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM sites WHERE company_id=%s ORDER BY created_at", (company_id,)
    ).fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_core_repositories.py::test_company_user_site_roundtrip -v`
Expected (CI): PASS.

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/repositories/__init__.py src/repositories/companies.py src/repositories/users.py src/repositories/sites.py tests/integration/test_core_repositories.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: company/user/site repositories"
```

---

### Task 10: ACL scope (pure) + memberships repo

`resolve_scope` is pure and driver-free (local unit test). `accessible_site_ids` runs the concrete query (integration). `admin`/`gm` expand to all site ids — deny-by-default elsewhere.

**Files:**
- Create: `src/repositories/acl.py` (driver-free)
- Create: `src/repositories/memberships.py`
- Test: `tests/unit/test_acl_scope.py` (pure)
- Test: `tests/integration/test_memberships_acl.py` (DB)

**Interfaces:**
- Produces:
  - `acl.resolve_scope(global_role: str) -> str` — `"ALL"` for `admin`/`gm`, else `"MEMBERSHIPS"`. (pure, no psycopg)
  - `memberships.resolve_scope` — re-exported from `acl` for convenience.
  - `memberships.add_membership(conn, user_id, site_id, role) -> dict`
  - `memberships.accessible_site_ids(conn, user_id, global_role) -> list`

- [ ] **Step 1: Write the failing unit test (pure)**

`tests/unit/test_acl_scope.py`:

```python
from repositories.acl import resolve_scope


def test_admin_and_gm_see_all():
    assert resolve_scope("admin") == "ALL"
    assert resolve_scope("gm") == "ALL"


def test_others_scoped_to_memberships():
    assert resolve_scope("pm") == "MEMBERSHIPS"
    assert resolve_scope("site_manager") == "MEMBERSHIPS"
    assert resolve_scope("worker") == "MEMBERSHIPS"
```

- [ ] **Step 2: Run unit test to verify it fails**

Run: `python -m pytest tests/unit/test_acl_scope.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'repositories.acl'`. (This import must NOT pull in psycopg — `acl.py` is driver-free.)

- [ ] **Step 3: Write minimal implementation**

`src/repositories/acl.py` (driver-free — no psycopg import):

```python
"""Pure ACL logic. MUST NOT import psycopg (unit-tested locally without a DB)."""
_ALL_ROLES = {"admin", "gm"}


def resolve_scope(global_role: str) -> str:
    return "ALL" if global_role in _ALL_ROLES else "MEMBERSHIPS"
```

`src/repositories/memberships.py`:

```python
from psycopg.rows import dict_row
from repositories.acl import resolve_scope  # re-export

__all__ = ["resolve_scope", "add_membership", "accessible_site_ids"]


def add_membership(conn, user_id, site_id, role) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO memberships (user_id, site_id, role) VALUES (%s, %s, %s) "
        "RETURNING id, user_id, site_id, role, created_at",
        (user_id, site_id, role),
    ).fetchone()


def accessible_site_ids(conn, user_id, global_role) -> list:
    if resolve_scope(global_role) == "ALL":
        rows = conn.execute("SELECT id FROM sites").fetchall()
    else:
        rows = conn.execute(
            "SELECT site_id FROM memberships WHERE user_id=%s", (user_id,)
        ).fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Run unit test to verify it passes**

Run: `python -m pytest tests/unit/test_acl_scope.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing integration test**

`tests/integration/test_memberships_acl.py`:

```python
import pytest
from repositories import companies, users, sites, memberships

pytestmark = pytest.mark.integration


def test_accessible_site_ids_by_role(db):
    co = companies.create_company(db, "Acme")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")

    admin = users.upsert_user(db, "sub-admin", "admin@a.com", company_id=co["id"], global_role="admin")
    worker = users.upsert_user(db, "sub-w", "w@a.com", company_id=co["id"], global_role="worker")
    memberships.add_membership(db, worker["id"], s1["id"], "worker")

    admin_sites = set(memberships.accessible_site_ids(db, admin["id"], "admin"))
    worker_sites = set(memberships.accessible_site_ids(db, worker["id"], "worker"))

    assert admin_sites == {s1["id"], s2["id"]}       # admin sees all
    assert worker_sites == {s1["id"]}                # worker sees only membership
```

- [ ] **Step 6: Run integration test**

Run: `python -m pytest tests/integration/test_memberships_acl.py -v`
Expected (CI): PASS.

- [ ] **Step 7: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/repositories/acl.py src/repositories/memberships.py tests/unit/test_acl_scope.py tests/integration/test_memberships_acl.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: ACL scope (pure) + memberships repo"
```

---

### Task 11: Topics repository (+ children)

Writes a topic with its action items, safety observations, photos in one call; reads topics for a site/date. Phase 4 will call `upsert_topic`.

**Files:**
- Create: `src/repositories/topics.py`
- Test: `tests/integration/test_topics_repository.py` (append behavior)

**Interfaces:**
- Consumes: a psycopg connection; `create_company`/`create_site` (Task 9).
- Produces:
  - `topics.upsert_topic(conn, site_id, report_date, title, *, user_id=None, source_s3_key=None, occurred_at=None, category=None, summary=None, action_items=None, safety=None, photos=None) -> dict` — `action_items`: list of `{text, responsible?, deadline?, priority?, status?}`; `safety`: list of `{observation, risk_level?, location?, status?}`; `photos`: list of `{s3_key, caption_text?}`. Returns the topic row.
  - `topics.list_site_topics(conn, site_id, report_date) -> list[dict]`
  - `topics.get_topic_photos(conn, topic_id) -> list[dict]`

- [ ] **Step 1: Write the failing test (append)**

Append to `tests/integration/test_topics_repository.py`:

```python
from repositories import companies, sites, topics


def test_upsert_topic_with_children(db):
    co = companies.create_company(db, "Acme")
    s = sites.create_site(db, co["id"], "S1")
    t = topics.upsert_topic(
        db, s["id"], "2026-07-02", "Concrete pour B2",
        category="progress", summary="Poured level B2 slab.",
        action_items=[{"text": "Order rebar", "responsible": "Sam", "priority": "high"}],
        safety=[{"observation": "Edge unprotected", "risk_level": "high"}],
        photos=[{"s3_key": "reports/2026-07-02/x/p1.jpg", "caption_text": "slab"}],
    )
    assert t["title"] == "Concrete pour B2"

    listed = topics.list_site_topics(db, s["id"], "2026-07-02")
    assert len(listed) == 1 and listed[0]["id"] == t["id"]

    ai = db.execute("SELECT text FROM action_items WHERE topic_id=%s", (t["id"],)).fetchall()
    sf = db.execute("SELECT observation FROM safety_observations WHERE topic_id=%s", (t["id"],)).fetchall()
    ph = topics.get_topic_photos(db, t["id"])
    assert ai == [("Order rebar",)]
    assert sf == [("Edge unprotected",)]
    assert ph[0]["s3_key"].endswith("p1.jpg")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_topics_repository.py::test_upsert_topic_with_children -v`
Expected (CI): FAIL — `ModuleNotFoundError: No module named 'repositories.topics'`.

- [ ] **Step 3: Write minimal implementation**

`src/repositories/topics.py`:

```python
from psycopg.rows import dict_row

_TOPIC_COLS = ("id, site_id, user_id, source_s3_key, report_date, occurred_at, "
               "category, title, summary, created_at")


def upsert_topic(conn, site_id, report_date, title, *, user_id=None, source_s3_key=None,
                 occurred_at=None, category=None, summary=None,
                 action_items=None, safety=None, photos=None) -> dict:
    cur = conn.cursor(row_factory=dict_row)
    topic = cur.execute(
        f"INSERT INTO topics (site_id, user_id, source_s3_key, report_date, occurred_at, "
        f"category, title, summary) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_TOPIC_COLS}",
        (site_id, user_id, source_s3_key, report_date, occurred_at, category, title, summary),
    ).fetchone()
    tid = topic["id"]
    for a in (action_items or []):
        conn.execute(
            "INSERT INTO action_items (topic_id, site_id, text, responsible, deadline, priority, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (tid, site_id, a["text"], a.get("responsible"), a.get("deadline"),
             a.get("priority"), a.get("status", "open")),
        )
    for o in (safety or []):
        conn.execute(
            "INSERT INTO safety_observations (topic_id, site_id, observation, risk_level, location, status) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (tid, site_id, o["observation"], o.get("risk_level"), o.get("location"),
             o.get("status", "open")),
        )
    for p in (photos or []):
        conn.execute(
            "INSERT INTO topic_photos (topic_id, s3_key, caption_text) VALUES (%s,%s,%s)",
            (tid, p["s3_key"], p.get("caption_text")),
        )
    return topic


def list_site_topics(conn, site_id, report_date) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TOPIC_COLS} FROM topics WHERE site_id=%s AND report_date=%s "
        f"ORDER BY occurred_at NULLS LAST, created_at",
        (site_id, report_date),
    ).fetchall()


def get_topic_photos(conn, topic_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, s3_key, caption_text, created_at FROM topic_photos "
        "WHERE topic_id=%s ORDER BY created_at",
        (topic_id,),
    ).fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_topics_repository.py -v`
Expected (CI): PASS (schema test from Task 6 + this behavior test).

- [ ] **Step 5: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/repositories/topics.py tests/integration/test_topics_repository.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: topics repository with action/safety/photo children"
```

---

### Task 12: Chunks repository — insert + ACL-filtered ANN search (small-to-big)

Vector nearest-neighbour search with the ACL filter and topic join in one SQL. `build_search_sql` is pure/driver-free; a unit test guards that the ACL `WHERE site_id = ANY` clause and cosine operator are always present (a security guardrail against a BUG-25-style leak). Behavior verified against Postgres.

**Files:**
- Create: `src/repositories/search_sql.py` (driver-free)
- Create: `src/repositories/chunks.py`
- Test: `tests/unit/test_chunk_search_sql.py` (pure guardrail)
- Test: `tests/integration/test_chunk_search.py` (append search behavior)

**Interfaces:**
- Produces:
  - `search_sql.build_search_sql() -> str` — the parameterized search SQL (`%(q)s`, `%(site_ids)s`, `%(k)s`). (pure, no psycopg)
  - `chunks.build_search_sql` — re-exported from `search_sql`.
  - `chunks.insert_chunk(conn, site_id, report_date, chunk_type, chunk_text, embedding, *, user_id=None, source_s3_key=None, topic_id=None, metadata=None) -> dict`
  - `chunks.search_chunks(conn, query_embedding, accessible_site_ids, k=5) -> list[dict]` — rows include `id, chunk_text, chunk_type, topic_id, source_s3_key, metadata, topic_title, topic_summary, distance`, nearest first, filtered to `accessible_site_ids`.

- [ ] **Step 1: Write the failing unit test (pure guardrail)**

`tests/unit/test_chunk_search_sql.py`:

```python
from repositories.search_sql import build_search_sql


def test_search_sql_always_enforces_acl_and_cosine():
    sql = build_search_sql().lower()
    assert "site_id = any(" in sql, "ACL site filter must be present (deny-by-default)"
    assert "<=>" in sql, "must order by cosine distance operator"
    assert "left join topics" in sql, "small-to-big: must join topic for rollup"
```

- [ ] **Step 2: Run unit test to verify it fails**

Run: `python -m pytest tests/unit/test_chunk_search_sql.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'repositories.search_sql'`. (Driver-free, so runs locally without psycopg.)

- [ ] **Step 3: Write minimal implementation**

`src/repositories/search_sql.py` (driver-free — no psycopg import):

```python
"""Pure search-SQL construction. MUST NOT import psycopg."""


def build_search_sql() -> str:
    # Deny-by-default: ALWAYS filter by the caller's accessible site ids.
    # small-to-big: return the parent topic's title/summary via LEFT JOIN.
    return (
        "SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "       c.metadata, t.title AS topic_title, t.summary AS topic_summary, "
        "       c.embedding <=> %(q)s AS distance "
        "FROM report_chunks c "
        "LEFT JOIN topics t ON t.id = c.topic_id "
        "WHERE c.site_id = ANY(%(site_ids)s) "
        "ORDER BY c.embedding <=> %(q)s "
        "LIMIT %(k)s"
    )
```

`src/repositories/chunks.py`:

```python
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from repositories.search_sql import build_search_sql  # re-export

__all__ = ["build_search_sql", "insert_chunk", "search_chunks"]


def insert_chunk(conn, site_id, report_date, chunk_type, chunk_text, embedding, *,
                 user_id=None, source_s3_key=None, topic_id=None, metadata=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO report_chunks (site_id, user_id, source_s3_key, topic_id, report_date, "
        "chunk_type, chunk_text, embedding, metadata) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "RETURNING id, site_id, topic_id, chunk_type, report_date, created_at",
        (site_id, user_id, source_s3_key, topic_id, report_date, chunk_type,
         chunk_text, embedding, Jsonb(metadata or {})),
    ).fetchone()


def search_chunks(conn, query_embedding, accessible_site_ids, k=5) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        build_search_sql(),
        {"q": query_embedding, "site_ids": list(accessible_site_ids), "k": k},
    ).fetchall()
```

- [ ] **Step 4: Run unit test to verify it passes**

Run: `python -m pytest tests/unit/test_chunk_search_sql.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing integration test (append)**

Append to `tests/integration/test_chunk_search.py`:

```python
from repositories import companies, sites, topics, chunks


def _unit_vec(dim, hot):
    v = [0.0] * dim
    v[hot] = 1.0
    return v


def test_search_ranks_by_similarity_and_enforces_acl(db):
    co = companies.create_company(db, "Acme")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")
    t1 = topics.upsert_topic(db, s1["id"], "2026-07-02", "Concrete pour", summary="B2 slab")

    chunks.insert_chunk(db, s1["id"], "2026-07-02", "topic", "concrete", _unit_vec(1024, 0), topic_id=t1["id"])
    chunks.insert_chunk(db, s1["id"], "2026-07-02", "topic", "scaffolding", _unit_vec(1024, 5))
    chunks.insert_chunk(db, s2["id"], "2026-07-02", "topic", "secret other site", _unit_vec(1024, 0))

    results = chunks.search_chunks(db, _unit_vec(1024, 0), [s1["id"]], k=5)

    texts = [r["chunk_text"] for r in results]
    assert texts[0] == "concrete", "nearest by cosine must rank first"
    assert "secret other site" not in texts, "ACL must exclude non-accessible sites"
    assert results[0]["topic_title"] == "Concrete pour", "small-to-big returns parent topic"
```

- [ ] **Step 6: Run integration test to verify it passes**

Run: `python -m pytest tests/integration/test_chunk_search.py -v`
Expected (CI): PASS (vector insert from Task 7 + this search test).

- [ ] **Step 7: Commit**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/repositories/search_sql.py src/repositories/chunks.py tests/unit/test_chunk_search_sql.py tests/integration/test_chunk_search.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: chunk repo — ACL-filtered ANN search with topic rollup"
```

---

### Task 13: Migration Lambda handler (ready for Phase 2B wiring)

A tiny in-VPC handler that applies pending migrations against Aurora. Phase 2B provisions the Lambda/VPC/`DATABASE_URL` secret; this task delivers the handler + a unit test so the code is reviewed and ready.

**Files:**
- Create: `src/lambda_migrate.py`
- Test: `tests/unit/test_lambda_migrate.py`

**Interfaces:**
- Consumes: `db.connection.get_connection`, `db.migrate.apply_migrations`.
- Produces: `lambda_handler(event, context) -> dict` = `{"applied": [...filenames]}`. `MIGRATIONS_DIR` env overrides the bundled `migrations/` dir.

- [ ] **Step 1: Write the failing unit test**

`tests/unit/test_lambda_migrate.py`:

```python
import lambda_migrate as lm


def test_handler_returns_applied_list(monkeypatch):
    calls = {}

    def fake_get_connection(dsn=None, autocommit=False):
        calls["autocommit"] = autocommit
        return object()

    def fake_apply(conn, migrations_dir):
        calls["dir"] = migrations_dir
        return ["0001_extensions.sql", "0002_core_relational.sql"]

    monkeypatch.setattr(lm, "get_connection", fake_get_connection)
    monkeypatch.setattr(lm, "apply_migrations", fake_apply)

    out = lm.lambda_handler({}, None)
    assert out == {"applied": ["0001_extensions.sql", "0002_core_relational.sql"]}
    assert calls["autocommit"] is True
    assert calls["dir"].endswith("migrations")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_lambda_migrate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lambda_migrate'`.

Note: importing `lambda_migrate` imports `db.connection`, which imports psycopg. If psycopg is not installed on the local Python 3.14, run this test in CI (Python 3.11) rather than locally. It is a unit test (monkeypatches out all I/O) but has a psycopg import dependency.

- [ ] **Step 3: Write minimal implementation**

`src/lambda_migrate.py`:

```python
"""In-VPC Lambda: apply pending SQL migrations to Aurora. Wired by Phase 2B."""
import os
from db.connection import get_connection
from db.migrate import apply_migrations

_DEFAULT_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def lambda_handler(event, context):
    migrations_dir = os.environ.get("MIGRATIONS_DIR", _DEFAULT_DIR)
    conn = get_connection(autocommit=True)
    try:
        applied = apply_migrations(conn, migrations_dir)
    finally:
        conn.close()
    return {"applied": applied}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_lambda_migrate.py -v` (locally if psycopg available, else in CI)
Expected: PASS.

- [ ] **Step 5: Push and confirm the full CI suite is green**

```bash
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" add src/lambda_migrate.py tests/unit/test_lambda_migrate.py
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" commit -m "feat: migration Lambda handler (ready for 2B wiring)"
git -C "C:/Users/camil/Dropbox/fieldsight-pipeline" push -u origin feature/phase2a-data-layer
```

Open a PR to `develop`; confirm the `Tests` workflow is green (all unit + integration tests pass against the pgvector service).

---

## What Phase 2A deliberately excludes (→ Phase 2B / later)

- **Provisioning:** Aurora Serverless v2, RDS Proxy, VPC/subnets/SG, Secrets Manager `DATABASE_URL`, and wiring `lambda_migrate` into SAM — all Phase 2B, entangled with the IaC reconciliation flagged in recon (two divergent `template.yaml` files + imperative `src/deploy_frontend.sh` bootstrap). Reconcile which template is authoritative before adding Aurora resources.
- **Embedding generation (Bedrock Titan V2):** Phase 2A stores/searches caller-provided vectors only. Bedrock `bedrock-runtime` + IAM are Phase 5.
- **Write/admin API endpoints** (projects/members/roles/uploads, presigned PUT): Phase 3, a new in-VPC Lambda consuming these repositories + a shared `auth.py` factored from `lambda_fieldsight_api.py`.
- **Populating the read model / chunks from real reports:** Phase 4 event-driven extraction calls `topics.upsert_topic` and `chunks.insert_chunk`.

## Notes for the executor

- **Local loop:** `python -m pytest -m "not integration"` (fast, no DB; Python 3.14 OK — pure modules `db/migrate.py`, `repositories/acl.py`, `repositories/search_sql.py` are driver-free). **Full loop:** push → the `Tests` CI workflow runs everything against `pgvector/pgvector:pg16`. No local Docker, so DB tests are CI-driven unless you install a local Postgres+pgvector and export `TEST_DATABASE_URL`.
- **psycopg on Python 3.14:** binary wheels may be unavailable on the local interpreter. That is why pure logic is kept driver-free — those unit tests pass locally regardless. Tests that import psycopg (`test_lambda_migrate` and all integration tests) run in CI (Python 3.11).
- **CLAUDE.md drift observed during recon (not fixed here):** `ENABLE_DYNAMODB` is described as OFF but `src/template.yaml` sets it `true`; and BUG-29 ("no local Python") is now stale (Python 3.14 is installed). Flag these to the user; update CLAUDE.md separately.
```