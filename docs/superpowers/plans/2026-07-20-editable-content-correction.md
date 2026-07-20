# Editable Content Correction (A + D) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users free-text-edit extracted structured content (topic title/summary, action-item text/responsible, finding observation/recommended_action/entity, safety observation) at the Aurora layer; materialize + audit every edit; never mutate the raw transcript; re-index the edited topic into RAG; and turn confirmed corrections into a scoped `name_aliases` glossary — plus fix D so report-sourced topics expose their durable Aurora ids and become editable.

**Architecture:** A new `PATCH /api/org/content/{table}/{id}` endpoint on the in-VPC org-api writes the corrected text into the structured Aurora row and appends a before/after row to a new `content_edits` audit table (atomic, synchronous). After commit it writes a per-topic **reindex-request artifact** to S3; an S3-event chain (non-VPC embed lambda adds DashScope vectors → in-VPC ingest lambda `delete_chunks_for_topic` + `insert_chunk`) re-embeds the edited topic — honoring BUG-36 (DashScope is non-VPC, Aurora is in-VPC). The transcript S3 object is read + alias-normalized for its vectors but never rewritten (D4). The D fix broadens the `/timeline` shim so any day with Aurora topics renders the id-carrying `render_report_shape` output instead of S3-verbatim. Confirmed diff candidates become `name_aliases` rows; a pure `normalize()` applies them at re-embed and at RAG synthesis.

**Tech Stack:** Python 3.12, psycopg3, AWS SAM (Lambda + API Gateway + Aurora PostgreSQL/pgvector), DashScope `text-embedding-v4`; frontend is no-build browser React (`React.createElement`, `.fs-*` BEM, `?v=` cache busters). Backend tests: `uv run pytest` with the `FakeConn`/`monkeypatch` pattern in `tests/unit/test_lambda_org_api.py`. Frontend checks: `node --check`.

## Global Constraints

- Backend TDD only (`uv run pytest`); use the `FakeConn`/`monkeypatch` fixture pattern from `tests/unit/test_lambda_org_api.py` (`make_event`, `wired`, `body_of`).
- **test and prod SHARE one Aurora cluster.** Migrations run on shared Aurora — **additive tables/columns only, no destructive changes** (no DROP/ALTER-TYPE/rename of existing objects).
- Migrations are numbered `NNNN_*.sql` in `src/migrations/`, applied by `lambda_migrate`/`db.migrate.apply_migrations`, idempotent via `schema_migrations`, one file per `conn.transaction()`. Highest existing = `0018`; new files are `0019`, `0020`.
- **DashScope embedding is NON-VPC (BUG-36).** In-VPC lambdas (org-api, ingest, rag-search) have NO internet/AWS egress. The edit endpoint must ENQUEUE re-index by writing an S3 artifact (S3 has a gateway endpoint); it must NEVER call DashScope or `lambda.invoke` inline. Re-embed runs on the non-VPC embed lambda.
- **The raw transcript S3 artifact is never written** — only read + normalized-copy embedded (D4).
- English only for all comments/commits/docs. Windows CRLF: stage by explicit path, **never `git add -A`** on pipeline develop.
- platform_admin cross-company: reuse `is_cross_company` / `_allowed_site_ids` (already correct) for the content-edit ACL.
- Deploy: `develop`→test (`deploy.yml`, ignores `docs/**`), `main`→prod (`deploy-prod.yml`, ignores `tests/**`+`docs/**`, `production` required-reviewer gate). S3 event triggers on the (external, hand-assembled) `IngestBucketName` lake are wired MANUALLY via `scripts/wire-s3-events.sh` (BUG-33), not SAM `Events`.
- Frontend: no build step, no npm/webpack; `node --check` every changed `.js`; bump `?v=N` cache busters in the preview HTML for every changed loaded file; `.fs-*` BEM; tokens only (`var(--...)`, never JS hex).

### Binding design decisions (spec §2, copied verbatim where they constrain a task)

- **D1** Correction model = free-text field editing (rewrite the whole field), NOT term-level select-and-replace.
- **D2** Glossary capture = diff + confirm; the endpoint returns diff candidate terms, the user/site_manager confirms which become aliases.
- **D3** Structured content is materialized in place (corrected text written into the Aurora row); display/analytics read it directly, no read-time normalization of structured content.
- **D4** Raw transcript is immutable; the transcript S3 artifact is never written; its vector representation is re-embedded from an alias-normalized copy.
- **D5** Alias store = the glossary; a confirmed correction becomes a scoped `name_aliases` row; affects FUTURE reads/embeds/synthesis; NOT retroactively find-replaced across historic content by default.
- **D6** Re-index granularity = per topic (`delete_chunks_for_topic`), not the whole report.
- **D7** Two-tier authority: per-item correction mirrors `patch_action_item` (author / the site's pm/site_manager / admin/gm / platform_admin cross-company); promoting a correction to a `name_aliases` row requires **site_manager+**.

---

## File Structure

**Backend — create:**
- `src/migrations/0019_content_edits.sql` — before/after audit table for content edits.
- `src/migrations/0020_name_aliases.sql` — scoped glossary alias store.
- `src/text_normalize.py` — PURE (no psycopg) `normalize(text, aliases)` + `diff_candidates(before, after)`.
- `src/repositories/aliases.py` — `name_aliases` store reads/writes (psycopg).
- `src/repositories/content.py` — generalized editable-field allow-list, `get_content_row`, `update_content_field`.
- `src/repositories/content_edits.py` — `append_content_edit`, `list_content_edits` (audit).
- `src/reindex.py` — per-topic reindex artifact build/apply (S3 keys, `enqueue_topic_reindex`, `apply_vectors`).
- Tests: `tests/unit/test_text_normalize.py`, `tests/unit/test_repo_aliases.py`, `tests/unit/test_repo_content.py`, `tests/unit/test_reindex.py`, `tests/unit/test_lambda_embed_report_reindex.py`, `tests/unit/test_lambda_ingest_reindex.py`. New cases appended to `tests/unit/test_lambda_org_api.py`.

**Backend — modify:**
- `src/repositories/chunks.py` — add `delete_chunks_for_topic`.
- `src/repositories/topics.py` — add `get_topic_full`.
- `src/lambda_org_api.py` — content-edit + history + alias routes/handlers; extend `render_report_shape` with durable ids; broaden `_render_timeline_for_user` (D fix); post-commit reindex enqueue.
- `src/lambda_embed_report.py` — reindex-request S3-event mode.
- `src/lambda_ingest.py` — reindex-vectors S3-event mode.
- `src/lambda_rag_search.py` — synthesis-time `normalize()` safety net.
- `src/template.yaml` + `scripts/wire-s3-events.sh` — reindex prefixes, IAM grants, S3 notifications.

**Frontend — modify (`C:/Users/camil/Dropbox/fieldsight-ui`):**
- `scripts/roles.js` — add `content:edit` permission.
- `scripts/api/actions.js` — add `updateContent`, `getContentHistory`, `confirmAlias`.
- `scripts/pages/timeline.js` — inline content editors, content History, glossary confirm; thread durable ids.
- `app-shell-preview.html` — bump `?v=` cache busters.

---

# Phase A — Data model + pure helpers

### Task 1: Migration — `content_edits` audit table

**Files:**
- Create: `src/migrations/0019_content_edits.sql`
- Test: `tests/unit/test_migrations_content_edits.py`

**Interfaces:**
- Produces: table `content_edits(id, company_id, table_name, row_id, field, before_text, after_text, actor_user_id, actor_role, created_at)`.

**Context:** Migration `0017_action_item_audit.sql` did NOT create a history table — it only added `updated_at`/`updated_by` columns to `action_items`. There is no existing before/after audit table to mirror, so this creates a genuinely new one, generalized across all editable tables. `table_name`/`row_id` are a soft polymorphic reference (no FK — the row it points at can be re-superseded by nightly ingest and vanish; audit history must survive that).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_migrations_content_edits.py
import os
import re

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                   "0019_content_edits.sql")


def test_content_edits_migration_is_additive_and_complete():
    sql = open(MIG, encoding="utf-8").read().lower()
    assert "create table content_edits" in sql
    for col in ("company_id", "table_name", "row_id", "field",
                "before_text", "after_text", "actor_user_id", "actor_role",
                "created_at"):
        assert col in sql, col
    # additive only — never destructive on the shared cluster
    assert not re.search(r"\bdrop\b|\balter\b", sql)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/camil/Dropbox/fieldsight-pipeline && uv run pytest tests/unit/test_migrations_content_edits.py -v`
Expected: FAIL — `FileNotFoundError` (0019 does not exist yet).

- [ ] **Step 3: Write the migration**

```sql
-- src/migrations/0019_content_edits.sql
-- Editable content correction (spec §5.2): generalized before/after edit
-- history across the item-store tables (topics / action_items / findings /
-- safety_observations). NOT a mirror of 0017 (which only added last-writer
-- columns to action_items) -- this is a first-class history table.
--
-- (table_name, row_id) is a SOFT polymorphic reference with NO foreign key:
-- the structured row it points at can be superseded/deleted by nightly ingest
-- re-extraction, and the audit trail must outlive that. company_id gives the
-- tenant scope the /history endpoint filters on. before_text/after_text are
-- the whole-field values (D1 free-text editing), nullable because a field can
-- go from/to empty.
CREATE TABLE content_edits (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  table_name    text NOT NULL,
  row_id        uuid NOT NULL,
  field         text NOT NULL,
  before_text   text,
  after_text    text,
  actor_user_id uuid REFERENCES users(id),
  actor_role    text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_content_edits_row ON content_edits (table_name, row_id, created_at);
CREATE INDEX idx_content_edits_company ON content_edits (company_id, created_at);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_migrations_content_edits.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/migrations/0019_content_edits.sql tests/unit/test_migrations_content_edits.py
git commit -m "feat(db): add content_edits audit table (0019)"
```

---

### Task 2: Migration — `name_aliases` glossary store

**Files:**
- Create: `src/migrations/0020_name_aliases.sql`
- Test: `tests/unit/test_migrations_name_aliases.py`

**Interfaces:**
- Produces: table `name_aliases(id, company_id, site_id NULL, wrong_term, right_term, kind, source, status, created_by, created_at)` (spec §5.4).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_migrations_name_aliases.py
import os
import re

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                   "0020_name_aliases.sql")


def test_name_aliases_migration():
    sql = open(MIG, encoding="utf-8").read().lower()
    assert "create table name_aliases" in sql
    for col in ("company_id", "site_id", "wrong_term", "right_term", "kind",
                "source", "status", "created_by", "created_at"):
        assert col in sql, col
    # kind/source/status are CHECK-constrained enums (spec §5.4)
    assert "check" in sql
    assert not re.search(r"\bdrop\b|\balter\b", sql)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_migrations_name_aliases.py -v`
Expected: FAIL — `FileNotFoundError`.

- [ ] **Step 3: Write the migration**

```sql
-- src/migrations/0020_name_aliases.sql
-- Alias store = the glossary (spec §5.4, D5). A confirmed correction becomes a
-- scoped wrong->right alias. site_id NULL = company-wide. Affects FUTURE
-- normalize() at re-embed + RAG synthesis (and, later, B's Transcribe custom
-- vocabulary). NOT retroactively applied to historic content by default.
CREATE TABLE name_aliases (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id     uuid REFERENCES sites(id) ON DELETE CASCADE,   -- NULL = company-wide
  wrong_term  text NOT NULL,
  right_term  text NOT NULL,
  kind        text NOT NULL DEFAULT 'other'
                CHECK (kind IN ('person', 'product', 'company', 'other')),
  source      text NOT NULL DEFAULT 'correction'
                CHECK (source IN ('correction', 'manual')),
  status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'retired')),
  created_by  uuid REFERENCES users(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_name_aliases_scope ON name_aliases (company_id, site_id, status);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_migrations_name_aliases.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/migrations/0020_name_aliases.sql tests/unit/test_migrations_name_aliases.py
git commit -m "feat(db): add name_aliases glossary store (0020)"
```

---

### Task 3: Pure `normalize()` + `diff_candidates()`

**Files:**
- Create: `src/text_normalize.py`
- Test: `tests/unit/test_text_normalize.py`

**Interfaces:**
- Produces:
  - `normalize(text: str, aliases: list[dict]) -> str` — whole-word, case-aware substitution. Each alias dict has `"wrong_term"` and `"right_term"`. PURE, no psycopg. Used at re-embed (transcript) and RAG synthesis.
  - `diff_candidates(before: str, after: str) -> list[str]` — proper-noun-like tokens present in `after` but not `before` (D2 glossary candidates). PURE.

**Context:** spec §7 requires whole-word boundaries (no partial-token corruption), case handling, multiple aliases, and scope precedence (the caller orders `aliases` most-specific-first; `normalize` applies them in the given order). No psycopg import — this module is imported by both in-VPC and non-VPC code and must be trivially unit-testable.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_text_normalize.py
from text_normalize import normalize, diff_candidates


def _a(wrong, right):
    return {"wrong_term": wrong, "right_term": right}


def test_whole_word_only_no_partial_corruption():
    # "Mackon" must not be rewritten inside "Mackonsson"
    out = normalize("Mackonsson met Mackon today", [_a("Mackon", "McCahon")])
    assert out == "Mackonsson met McCahon today"


def test_case_aware_preserves_surface_casing():
    aliases = [_a("mackon", "mccahon")]
    assert normalize("mackon", aliases) == "mccahon"          # lower -> lower
    assert normalize("Mackon", aliases) == "Mccahon"          # Title -> Title
    assert normalize("MACKON", aliases) == "MCCAHON"          # UPPER -> UPPER


def test_multiple_aliases_applied_in_order():
    out = normalize("Fyfe poured the slab",
                    [_a("Fyfe", "Fife"), _a("slab", "raft")])
    assert out == "Fife poured the raft"


def test_no_aliases_is_identity():
    assert normalize("unchanged text", []) == "unchanged text"


def test_diff_candidates_surfaces_new_proper_nouns_only():
    cands = diff_candidates("the crew from Mackon arrived",
                            "the crew from McCahon arrived early")
    assert "McCahon" in cands
    assert "arrived" not in cands       # already present in before
    assert "the" not in cands           # not proper-noun-like
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_text_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'text_normalize'`.

- [ ] **Step 3: Write the implementation**

```python
# src/text_normalize.py
"""Pure alias substitution + diff-candidate extraction for editable content
correction (spec §5.4 / §7). NO psycopg, NO I/O -- imported by both the in-VPC
re-embed path and the non-VPC embed lambda, and unit-tested in isolation.

normalize() is whole-word (regex \b boundaries, so 'Mackon' never rewrites
inside 'Mackonsson') and case-aware (the replacement adopts the surface casing
of the matched token: lower/Title/UPPER). Aliases are applied in the order the
caller supplies them (the caller sorts site-scoped before company-scoped, so
the more specific alias wins -- spec §7 scope precedence)."""
import re

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def _match_case(surface: str, replacement: str) -> str:
    if surface.isupper():
        return replacement.upper()
    if surface[:1].isupper() and surface[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def normalize(text, aliases):
    if not text or not aliases:
        return text
    out = text
    for a in aliases:
        wrong = (a.get("wrong_term") or "").strip()
        right = a.get("right_term") or ""
        if not wrong:
            continue
        pattern = re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE)
        out = pattern.sub(lambda m: _match_case(m.group(0), right), out)
    return out


def _proper_nounish(tok: str) -> bool:
    # Capitalized or ALLCAPS multi-char token -- a plausible name/product.
    return len(tok) > 1 and (tok[0].isupper())


def diff_candidates(before, after):
    """Tokens present in `after` but not in `before` that look like proper
    nouns -- the D2 glossary candidates surfaced after an edit. De-duplicated,
    order-preserving."""
    before_tokens = set(_TOKEN_RE.findall(before or ""))
    seen, out = set(), []
    for tok in _TOKEN_RE.findall(after or ""):
        if tok in before_tokens or tok in seen or not _proper_nounish(tok):
            continue
        seen.add(tok)
        out.append(tok)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_text_normalize.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add src/text_normalize.py tests/unit/test_text_normalize.py
git commit -m "feat: pure normalize() + diff_candidates() for content correction"
```

---

### Task 4: `name_aliases` store repository

**Files:**
- Create: `src/repositories/aliases.py`
- Test: `tests/unit/test_repo_aliases.py`

**Interfaces:**
- Consumes: nothing (psycopg only).
- Produces:
  - `list_active(conn, company_id, site_ids=None) -> list[dict]` — active aliases for the company, site-scoped rows first (so `normalize()` applies site over company). Returns dicts with `wrong_term`/`right_term`/`kind`/`site_id`.
  - `create_alias(conn, company_id, site_id, wrong_term, right_term, kind, created_by, source='correction') -> dict` — insert + return the row.

**Context:** mirrors `repositories/observations.py` style (module-level SQL, `conn.cursor(row_factory=dict_row).execute(...).fetchone()/.fetchall()`). `list_active` orders `site_id NULLS LAST` so site-scoped aliases precede company-wide ones — the precedence `normalize()` relies on.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_repo_aliases.py
import pytest

aliases = pytest.importorskip("repositories.aliases",
                              reason="requires psycopg (installed in CI)")


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self, *a, **k):
        return self.cur


def test_list_active_orders_site_scoped_first():
    conn = FakeConn([{"wrong_term": "Mackon", "right_term": "McCahon",
                      "site_id": "s-1", "kind": "person"}])
    rows = aliases.list_active(conn, "co-1", site_ids=["s-1"])
    assert rows[0]["right_term"] == "McCahon"
    assert "status = 'active'" in conn.cur.sql.lower() or "status='active'" in conn.cur.sql.lower()
    assert "nulls last" in conn.cur.sql.lower()


def test_create_alias_binds_all_columns():
    conn = FakeConn([{"id": "a-1", "wrong_term": "Fyfe", "right_term": "Fife"}])
    row = aliases.create_alias(conn, "co-1", "s-1", "Fyfe", "Fife", "person",
                               "u-1", source="correction")
    assert row["right_term"] == "Fife"
    assert conn.cur.params == ("co-1", "s-1", "Fyfe", "Fife", "person",
                               "correction", "u-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repo_aliases.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'repositories.aliases'`.

- [ ] **Step 3: Write the implementation**

```python
# src/repositories/aliases.py
"""Repository for the name_aliases glossary store (migration 0020, spec §5.4).
Style mirrors repositories/observations.py. list_active feeds text_normalize.
normalize(); create_alias is written by the D2 glossary-confirm endpoint."""
from psycopg.rows import dict_row

_COLS = ("id, company_id, site_id, wrong_term, right_term, kind, source, "
         "status, created_by, created_at")


def list_active(conn, company_id, site_ids=None):
    """Active aliases for the company. Site-scoped rows first (site_id NULLS
    LAST puts company-wide last), so a caller feeding these to normalize()
    applies the more specific site alias before the company-wide one (spec §7
    scope precedence). site_ids optionally narrows the site-scoped rows to the
    caller's reach; company-wide rows (site_id IS NULL) are always included."""
    if site_ids is not None:
        rows = conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLS} FROM name_aliases "
            f"WHERE company_id=%s AND status='active' "
            f"AND (site_id IS NULL OR site_id = ANY(%s::uuid[])) "
            f"ORDER BY site_id NULLS LAST, created_at",
            (company_id, list(site_ids)),
        ).fetchall()
    else:
        rows = conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLS} FROM name_aliases "
            f"WHERE company_id=%s AND status='active' "
            f"ORDER BY site_id NULLS LAST, created_at",
            (company_id,),
        ).fetchall()
    return rows


def create_alias(conn, company_id, site_id, wrong_term, right_term, kind,
                 created_by, source="correction"):
    """Insert one alias (D5) and return it. site_id None = company-wide."""
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO name_aliases (company_id, site_id, wrong_term, "
        f"right_term, kind, source, created_by) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
        (company_id, site_id, wrong_term, right_term, kind, source, created_by),
    ).fetchone()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_repo_aliases.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/aliases.py tests/unit/test_repo_aliases.py
git commit -m "feat(repo): name_aliases store (list_active/create_alias)"
```

---

# Phase B — Content-edit endpoint, audit, and the D fix

### Task 5: Editable-field allow-list + content repository

**Files:**
- Create: `src/repositories/content.py`
- Test: `tests/unit/test_repo_content.py`

**Interfaces:**
- Produces:
  - `EDITABLE: dict[str, set[str]]` — table_name → editable field names (spec §3): `topics`→`{title,summary}`, `action_items`→`{text,responsible}`, `findings`→`{observation,recommended_action,entity_name,entity_trade}`, `safety_observations`→`{observation}`.
  - `is_editable(table, field) -> bool`.
  - `get_content_row(conn, table, row_id) -> dict | None` — returns `id, site_id, company_id, author_user_id` + the current editable field values; `None` if not found / bad table / malformed uuid.
  - `update_content_field(conn, table, row_id, field, value) -> dict | None` — whitelisted single-field UPDATE returning the updated row.

**Context:** All four editable tables reach `company_id` via `site_id → sites.company_id` (see `0003_dashboard_readmodel.sql`), and all relate to a topic (so `author_user_id` = the owning topic's `user_id`; for `topics` it is the row's own `user_id`). Table names come only from `EDITABLE` keys (never raw user input), so interpolating them into SQL is safe. Field names are validated against `EDITABLE[table]` before interpolation. This generalizes `action_items.update_action_item_fields` / `get_action_item`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_repo_content.py
import pytest

content = pytest.importorskip("repositories.content",
                              reason="requires psycopg (installed in CI)")


class FakeCursor:
    def __init__(self, row):
        self._row = row
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, row):
        self.cur = FakeCursor(row)

    def cursor(self, *a, **k):
        return self.cur


def test_editable_allow_list_matches_spec():
    assert content.EDITABLE["topics"] == {"title", "summary"}
    assert content.EDITABLE["action_items"] == {"text", "responsible"}
    assert content.EDITABLE["findings"] == {
        "observation", "recommended_action", "entity_name", "entity_trade"}
    assert content.EDITABLE["safety_observations"] == {"observation"}


def test_is_editable_rejects_enum_and_unknown_tables():
    assert content.is_editable("topics", "title")
    assert not content.is_editable("topics", "category")     # enum, excluded (§3)
    assert not content.is_editable("action_items", "status")  # task metadata
    assert not content.is_editable("recordings", "title")     # not an item-store table


def test_get_content_row_joins_company_and_author():
    conn = FakeConn({"id": "t-1", "site_id": "s-1", "company_id": "co-1",
                     "author_user_id": "u-9", "title": "Slab pour", "summary": "x"})
    row = content.get_content_row(conn, "topics", "t-1")
    assert row["company_id"] == "co-1"
    assert row["author_user_id"] == "u-9"
    assert "join sites" in conn.cur.sql.lower()


def test_update_content_field_only_writes_whitelisted_column():
    conn = FakeConn({"id": "t-1", "title": "Corrected"})
    row = content.update_content_field(conn, "topics", "t-1", "title", "Corrected")
    assert row["title"] == "Corrected"
    assert "update topics set title" in conn.cur.sql.lower()
    assert conn.cur.params == ("Corrected", "t-1")


def test_update_content_field_rejects_non_whitelisted_field():
    conn = FakeConn(None)
    assert content.update_content_field(conn, "topics", "t-1", "category", "x") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repo_content.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'repositories.content'`.

- [ ] **Step 3: Write the implementation**

```python
# src/repositories/content.py
"""Generalized editable free-text content fields for the item store
(spec §3 / §5.2). One allow-list + two dumb accessors, shared by
PATCH /api/org/content/{table}/{id}. Excludes categorical/enum fields
(domain/severity/category/status/priority/deadline) -- those are extraction
judgments or task metadata, not transcription errors (spec §3). Generalizes
action_items.get_action_item / update_action_item_fields."""
import psycopg
from psycopg.rows import dict_row

# table_name -> editable free-text columns (spec §3)
EDITABLE = {
    "topics": {"title", "summary"},
    "action_items": {"text", "responsible"},
    "findings": {"observation", "recommended_action", "entity_name", "entity_trade"},
    "safety_observations": {"observation"},
}

# Per-table SELECT that returns id, site_id, company_id, author_user_id, plus
# every editable field's current value. Every table reaches company_id via
# site_id -> sites.company_id; author_user_id is the owning topic's user_id
# (for `topics` the row IS the topic). Table names come only from EDITABLE
# keys -- never raw request input -- so the interpolation is injection-safe.
_SELECT = {
    "topics": (
        "SELECT x.id, x.site_id, s.company_id, x.user_id AS author_user_id, "
        "x.title, x.summary "
        "FROM topics x JOIN sites s ON s.id = x.site_id WHERE x.id=%s"),
    "action_items": (
        "SELECT x.id, x.site_id, s.company_id, tp.user_id AS author_user_id, "
        "x.text, x.responsible "
        "FROM action_items x JOIN sites s ON s.id = x.site_id "
        "JOIN topics tp ON tp.id = x.topic_id WHERE x.id=%s"),
    "findings": (
        "SELECT x.id, x.site_id, s.company_id, tp.user_id AS author_user_id, "
        "x.observation, x.recommended_action, x.entity_name, x.entity_trade "
        "FROM findings x JOIN sites s ON s.id = x.site_id "
        "JOIN topics tp ON tp.id = x.topic_id WHERE x.id=%s"),
    "safety_observations": (
        "SELECT x.id, x.site_id, s.company_id, tp.user_id AS author_user_id, "
        "x.observation "
        "FROM safety_observations x JOIN sites s ON s.id = x.site_id "
        "JOIN topics tp ON tp.id = x.topic_id WHERE x.id=%s"),
}


def is_editable(table, field):
    return table in EDITABLE and field in EDITABLE[table]


def get_content_row(conn, table, row_id):
    """id/site_id/company_id/author_user_id + current editable values for one
    row. None on unknown table, missing row, or malformed uuid (404 semantics,
    same posture as observations.get_observation)."""
    if table not in _SELECT:
        return None
    try:
        return conn.cursor(row_factory=dict_row).execute(
            _SELECT[table], (row_id,)).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def update_content_field(conn, table, row_id, field, value):
    """Whitelisted single-field UPDATE (D3 materialize-in-place). Returns the
    updated row (id + the field), or None on non-whitelisted table/field or
    malformed uuid. No updated_at bump -- content_edits IS the audit trail."""
    if not is_editable(table, field):
        return None
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"UPDATE {table} SET {field}=%s WHERE id=%s RETURNING id, {field}",
            (value, row_id),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_repo_content.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add src/repositories/content.py tests/unit/test_repo_content.py
git commit -m "feat(repo): generalized editable content fields (allow-list + accessors)"
```

---

### Task 6: `content_edits` audit repository

**Files:**
- Create: `src/repositories/content_edits.py`
- Test: `tests/unit/test_repo_content_edits.py`

**Interfaces:**
- Produces:
  - `append_content_edit(conn, company_id, table_name, row_id, field, before_text, after_text, actor_user_id, actor_role) -> dict` — insert one history row, return it.
  - `list_content_edits(conn, company_id, table_name, row_id) -> list[dict]` — company-guarded history for one row, newest first.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_repo_content_edits.py
import pytest

ce = pytest.importorskip("repositories.content_edits",
                         reason="requires psycopg (installed in CI)")


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self, *a, **k):
        return self.cur


def test_append_binds_before_after_actor():
    conn = FakeConn([{"id": "e-1"}])
    ce.append_content_edit(conn, "co-1", "topics", "t-1", "title",
                           "Mackon", "McCahon", "u-1", "site_manager")
    assert conn.cur.params == ("co-1", "topics", "t-1", "title",
                               "Mackon", "McCahon", "u-1", "site_manager")


def test_list_is_company_guarded_newest_first():
    conn = FakeConn([{"id": "e-2"}, {"id": "e-1"}])
    rows = ce.list_content_edits(conn, "co-1", "topics", "t-1")
    assert len(rows) == 2
    assert "company_id=%s" in conn.cur.sql or "company_id = %s" in conn.cur.sql
    assert "order by created_at desc" in conn.cur.sql.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repo_content_edits.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/repositories/content_edits.py
"""Audit history for editable content correction (migration 0019, spec §5.2).
append on every successful edit; list backs GET /content/{table}/{id}/history.
Company-guarded reads (the endpoint already resolved the row's company)."""
from psycopg.rows import dict_row

_COLS = ("id, company_id, table_name, row_id, field, before_text, after_text, "
         "actor_user_id, actor_role, created_at")


def append_content_edit(conn, company_id, table_name, row_id, field,
                        before_text, after_text, actor_user_id, actor_role):
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO content_edits (company_id, table_name, row_id, field, "
        f"before_text, after_text, actor_user_id, actor_role) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
        (company_id, table_name, row_id, field, before_text, after_text,
         actor_user_id, actor_role),
    ).fetchone()


def list_content_edits(conn, company_id, table_name, row_id):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM content_edits "
        f"WHERE company_id=%s AND table_name=%s AND row_id=%s "
        f"ORDER BY created_at DESC",
        (company_id, table_name, row_id),
    ).fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_repo_content_edits.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/content_edits.py tests/unit/test_repo_content_edits.py
git commit -m "feat(repo): content_edits audit (append/list)"
```

---

### Task 7: `delete_chunks_for_topic` + `topics.get_topic_full`

**Files:**
- Modify: `src/repositories/chunks.py`
- Modify: `src/repositories/topics.py`
- Test: `tests/unit/test_repo_chunks_delete_topic.py`

**Interfaces:**
- Produces:
  - `chunks.delete_chunks_for_topic(conn, topic_id) -> int` — delete every `report_chunks` row for one topic (spec §5.3, sibling of `delete_chunks_for_source`).
  - `topics.get_topic_full(conn, topic_id) -> dict | None` — one topic row joined with `site_name`/`user_name`, plus its `action_items`/`safety_observations`/`findings`/`photos` children, shaped exactly like a `list_topics_for_source_prefix` element (so `render_report_shape` can consume `[row]`).

**Context:** `report_chunks.topic_id` is populated with the durable topic UUID for report-sourced topics (`lambda_ingest.ingest_report` maps `topic_seq_to_id` onto `insert_chunk(..., topic_id=...)`). `delete_chunks_for_topic` is the per-topic delete-and-replace mechanism (D6). `get_topic_full` reuses the batched-children shape of `list_topics_for_source_prefix` but for a single id, so the reindex builder can render just the edited topic.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_repo_chunks_delete_topic.py
import pytest

chunks = pytest.importorskip("repositories.chunks",
                             reason="requires psycopg (installed in CI)")


class FakeResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeConn:
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return FakeResult(3)


def test_delete_chunks_for_topic_deletes_by_topic_id():
    conn = FakeConn()
    n = chunks.delete_chunks_for_topic(conn, "t-1")
    assert n == 3
    assert "delete from report_chunks where topic_id=%s" in conn.sql.lower()
    assert conn.params == ("t-1",)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repo_chunks_delete_topic.py -v`
Expected: FAIL — `AttributeError: module 'repositories.chunks' has no attribute 'delete_chunks_for_topic'`.

- [ ] **Step 3a: Add `delete_chunks_for_topic` to `chunks.py`**

In `src/repositories/chunks.py`, update `__all__` and append the function after `delete_chunks_for_source`:

```python
__all__ = ["build_search_sql", "insert_chunk", "search_chunks",
           "delete_chunks_for_source", "delete_chunks_for_topic"]
```

```python
def delete_chunks_for_topic(conn, topic_id) -> int:
    """Delete report_chunks rows for one topic (spec §5.3, D6 per-topic
    re-index). Sibling of delete_chunks_for_source, keyed on the durable
    topic_id that lambda_ingest stamps onto each chunk (topic_seq_to_id).
    Used by the reindex apply step: delete this topic's chunks, then
    re-insert the freshly-embedded corrected chunks."""
    cur = conn.execute(
        "DELETE FROM report_chunks WHERE topic_id=%s",
        (topic_id,),
    )
    return cur.rowcount
```

- [ ] **Step 3b: Add `get_topic_full` to `topics.py`**

In `src/repositories/topics.py`, append after `list_topics_for_source_prefix`:

```python
def get_topic_full(conn, topic_id) -> dict | None:
    """One topic row (joined site_name/user_name) plus its action_items /
    safety_observations / findings / photos children, shaped EXACTLY like a
    list_topics_for_source_prefix element so render_report_shape can consume
    [row]. Used by the per-topic reindex builder (reindex.enqueue_topic_
    reindex). Returns None if the id is missing/malformed."""
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TOPIC_COLS_JOINED}, "
        f"s.name AS site_name, (u.first_name || ' ' || u.last_name) AS user_name "
        f"FROM topics t "
        f"LEFT JOIN sites s ON s.id = t.site_id "
        f"LEFT JOIN users u ON u.id = t.user_id "
        f"WHERE t.id=%s",
        (topic_id,),
    ).fetchall()
    if not rows:
        return None
    t = rows[0]
    tids = [t["id"]]
    t["action_items"] = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, text, responsible, deadline, deadline_text, "
        "priority, status, created_at FROM action_items WHERE topic_id = ANY(%s) "
        "ORDER BY created_at", (tids,)).fetchall()
    t["safety_observations"] = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, observation, risk_level, location, status, "
        "created_at FROM safety_observations WHERE topic_id = ANY(%s) "
        "ORDER BY created_at", (tids,)).fetchall()
    t["findings"] = findings.list_for_topics(conn, tids)
    t["photos"] = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, s3_key, caption_text FROM topic_photos "
        "WHERE topic_id = ANY(%s) ORDER BY created_at", (tids,)).fetchall()
    return t
```

(`_TOPIC_COLS_JOINED`, `dict_row`, and `findings` are already imported/defined at the top of `topics.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_repo_chunks_delete_topic.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/chunks.py src/repositories/topics.py tests/unit/test_repo_chunks_delete_topic.py
git commit -m "feat(repo): delete_chunks_for_topic + topics.get_topic_full"
```

---

### Task 8: D fix — surface durable ids in `render_report_shape` + broaden the `/timeline` shim

**Files:**
- Modify: `src/lambda_org_api.py` — `render_report_shape` (~1512), `_render_timeline_for_user` (~1558)
- Test: append to `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `topics.has_topics_for_source_prefix`, `topics.list_topics_for_source_prefix`, `_allowed_site_ids`, `_get_lake_json`.
- Produces: `render_report_shape` output now carries per-topic durable ids (`topic_row_id`, `safety_flags[].id`, `safety_flags[].source_table`) so the frontend can address `PATCH /content/{table}/{id}`. `action_items[].id` and `findings[].id` already present.

**Spec §5.1 (binding):** "prefer the Aurora-rendered shape whenever Aurora topics exist for the caller's accessible sites on that date, so report-sourced content becomes editable exactly like extraction-sourced. The byte-identical-verbatim contract is retained only for days with **no** Aurora topics at all."

**Parity finding (verified in code):** `render_report_shape(rows, doc, date, folder)` is a pure function that rebuilds `topics[]` from Aurora rows and MERGES the S3 doc's top-level prose (`executive_summary`, `safety_observations`, `quality_and_compliance`, `critical_dates_and_deadlines`) via `doc.get(...)`. Report-sourced Aurora topics were ingested FROM that same `daily_report.json` (`lambda_ingest.ingest_report`), so title/summary/action_items/safety/findings/photos match, and passing the doc preserves the four prose blocks. The one real loss is per-topic `key_decisions` (hard-coded `[]` in `render_report_shape`, D3 v1 — decisions table deferred) and cosmetic `_report_metadata`/topic ordering. This is accepted as D3-consistent. The broadening therefore adds a **report-prefix fallback** (`reports/{date}/{user}/`) after the existing extraction-prefix branch; verbatim S3 is served only when NEITHER prefix has Aurora topics.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_lambda_org_api.py

def test_render_report_shape_exposes_durable_topic_and_safety_ids():
    rows = [{
        "id": "topic-uuid-1", "site_id": "s-1", "site_name": "Alpha",
        "user_name": "Ada L", "time_range": "09:00 - 09:30", "title": "Slab",
        "category": "progress", "participants": [], "summary": "poured slab",
        "action_items": [{"id": "ai-1", "text": "cure 7d", "responsible": "Sam",
                          "deadline": None, "deadline_text": None,
                          "priority": "medium", "status": "open"}],
        "safety_observations": [{"id": "so-1", "observation": "edge protection",
                                 "risk_level": "high"}],
        "findings": [], "photos": [],
    }]
    out = org.render_report_shape(rows, None, "2026-07-16", "Ada_L")
    t = out["topics"][0]
    assert t["topic_row_id"] == "topic-uuid-1"          # NEW durable id
    assert t["action_items"][0]["id"] == "ai-1"          # already present
    assert t["safety_flags"][0]["id"] == "so-1"          # NEW
    assert t["safety_flags"][0]["source_table"] == "safety_observations"


def test_timeline_report_sourced_day_renders_with_ids(wired, monkeypatch):
    # No extraction topics, but report-sourced Aurora topics DO exist -> must
    # render the id-carrying shape, not S3-verbatim (the SB1108 2026-07-16 case).
    def has_prefix(conn, prefix):
        return prefix.startswith("reports/")            # extraction miss, report hit

    report_rows = [{
        "id": "topic-uuid-9", "site_id": "s-1", "site_name": "Alpha",
        "user_name": "Ada L", "time_range": None, "title": "From report",
        "category": None, "participants": [], "summary": "s",
        "action_items": [], "safety_observations": [], "findings": [], "photos": [],
    }]
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix", has_prefix)
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix: report_rows)
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-1"})
    monkeypatch.setattr(org, "_get_lake_json", lambda key: {"executive_summary": ["ok"]})
    res = org._render_timeline_for_user(FakeConn(), CALLER, "2026-07-16", "Ada_L")
    body = body_of(res)
    assert res["statusCode"] == 200
    assert body["topics"][0]["topic_row_id"] == "topic-uuid-9"
    assert body["executive_summary"] == ["ok"]           # prose preserved


def test_timeline_no_aurora_topics_stays_verbatim(wired, monkeypatch):
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)       # neither prefix has topics
    monkeypatch.setattr(org, "_get_lake_json",
                        lambda key: {"_report_metadata": {"source": "nightly"},
                                     "topics": [{"topic_title": "verbatim"}]})
    res = org._render_timeline_for_user(FakeConn(), CALLER, "2026-07-10", "Ada_L")
    body = body_of(res)
    assert body["_report_metadata"]["source"] == "nightly"   # byte-verbatim
    assert body["topics"][0]["topic_title"] == "verbatim"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "durable_topic or report_sourced_day or no_aurora_topics_stays_verbatim" -v`
Expected: FAIL — `KeyError: 'topic_row_id'` (render) and the report-prefix branch not taken (timeline).

- [ ] **Step 3a: Extend `render_report_shape`**

In `src/lambda_org_api.py`, inside the `for i, t in enumerate(rows):` loop of `render_report_shape`, add durable ids to the safety flags and the topic dict. Replace the flag-building block:

```python
        flags = [{"observation": f["observation"],
                  "risk_level": _SEV_TO_RISK.get(f["severity"], "medium"),
                  "recommended_action": f["recommended_action"],
                  "id": str(f["id"]), "source_table": "findings"}
                 for f in t["findings"] if f["domain"] == "safety"]
        if not flags:                               # pre-#46 legacy extractions
            flags = [{"observation": s["observation"], "risk_level": s["risk_level"],
                      "recommended_action": None,
                      "id": str(s["id"]), "source_table": "safety_observations"}
                     for s in t["safety_observations"]]
```

and in the `topics_out.append({...})` dict add one line after `"topic_id": i,`:

```python
            "topic_row_id": str(t["id"]),           # durable topics.id (D fix — editable anchor)
```

- [ ] **Step 3b: Broaden `_render_timeline_for_user`**

In `src/lambda_org_api.py`, replace the body of `_render_timeline_for_user` between the docstring and the final `return` so it tries the extraction prefix, then the report prefix, before falling back:

```python
    allowed = _allowed_site_ids(conn, caller)

    def _aurora_shape(prefix):
        """Return the id-carrying rendered shape for `prefix` if it has
        Aurora topics inside the caller's site ACL, else None."""
        if not topics.has_topics_for_source_prefix(conn, prefix):
            return None
        rows = [r for r in topics.list_topics_for_source_prefix(conn, prefix)
                if str(r["site_id"]) in allowed]
        if not rows:
            return None
        # CRITICAL-1: cross-user graded view never merges the target's whole-day
        # prose (not site-clipped). Topic rows are already site-clipped above.
        doc = None if cross_user_clip else \
            _get_lake_json(f"reports/{date}/{user}/daily_report.json")
        return render_report_shape(rows, doc, date, user)

    # D fix (spec §5.1): prefer the Aurora-rendered shape whenever Aurora topics
    # exist for this (user, date) -- extraction-sourced OR report-sourced -- so
    # report-sourced content is editable exactly like extraction-sourced. Only
    # a day with NO Aurora topics at all keeps the byte-verbatim S3 contract.
    for prefix in (f"extractions/{user}/{date}/", f"reports/{date}/{user}/"):
        shape = _aurora_shape(prefix)
        if shape is not None:
            return ok(shape)
    if cross_user_clip:
        # No in-scope Aurora topics -> verbatim S3 is not site-clipped -> 404.
        return ok({"message": f"No in-scope report for {user} on {date}", "date": date}, 404)
    doc = _get_lake_json(f"reports/{date}/{user}/daily_report.json")
    if doc is not None:
        return ok(doc)                              # VERBATIM (byte-identical history)
    return ok({"message": f"No report for {user} on {date}", "date": date}, 404)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "durable_topic or report_sourced_day or no_aurora_topics_stays_verbatim" -v`
Expected: PASS. Then run the full existing timeline suite to prove no regression:
Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "timeline or render_report" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(D): surface durable ids + render report-sourced Aurora topics in /timeline"
```

---

### Task 9: Reindex artifact builder + apply (`src/reindex.py`)

**Files:**
- Create: `src/reindex.py`
- Test: `tests/unit/test_reindex.py`

**Interfaces:**
- Consumes: `topics.get_topic_full`, `render_report_shape` (imported from `lambda_org_api`), `chunking.chunk_report`, `chunks.delete_chunks_for_topic`, `chunks.insert_chunk`, `aliases.list_active`, `text_normalize.normalize`.
- Produces:
  - `request_key(date, folder, topic_id) -> str` = `reindex_requests/{date}/{folder}/{topic_id}.json`
  - `vectors_key(date, folder, topic_id) -> str` = `reindex_requests/{date}/{folder}/{topic_id}.vectors.json`
  - `enqueue_topic_reindex(s3_client, bucket, conn, topic_id, folder, date) -> str | None` — build the corrected topic's chunk texts + active aliases, write the request artifact, return its key (or `None` if the topic is gone). In-VPC, S3-only (no DashScope).
  - `apply_vectors(conn, result: dict) -> int` — `delete_chunks_for_topic` then `insert_chunk` for each embedded chunk; returns count.

**Context:** The endpoint (in-VPC) builds only the corrected TOPIC chunk texts (cheap, straight from Aurora) plus the transcript-window info the non-VPC embed lambda needs (`report_key`, `topic_seq`, active `aliases`) so the embed lambda rebuilds + normalizes the transcript windows itself (it can read the immutable `daily_report.json` + transcripts; the transcript S3 object is never rewritten — D4). This keeps the endpoint snappy (spec §6: the edit write must not block on re-embed) and keeps the embed lambda a dumb text→vector step matching the existing sidecar contract.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reindex.py
import json

import pytest

reindex = pytest.importorskip("reindex", reason="requires psycopg (installed in CI)")


class FakeS3:
    def __init__(self):
        self.puts = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts[Key] = json.loads(Body)


def test_keys():
    assert reindex.request_key("2026-07-16", "Ada_L", "t-1") == \
        "reindex_requests/2026-07-16/Ada_L/t-1.json"
    assert reindex.vectors_key("2026-07-16", "Ada_L", "t-1") == \
        "reindex_requests/2026-07-16/Ada_L/t-1.vectors.json"


def test_enqueue_writes_request_with_topic_chunks_and_aliases(monkeypatch):
    topic_row = {"id": "t-1", "site_id": "s-1", "user_id": "u-9",
                 "source_s3_key": "reports/2026-07-16/Ada_L/daily_report.json",
                 "report_date": "2026-07-16", "site_name": "Alpha",
                 "user_name": "Ada L", "time_range": "09:00 - 09:30",
                 "title": "Corrected slab", "category": "progress",
                 "participants": [], "summary": "poured raft",
                 "action_items": [], "safety_observations": [], "findings": [],
                 "photos": []}
    monkeypatch.setattr(reindex.topics, "get_topic_full", lambda conn, tid: topic_row)
    monkeypatch.setattr(reindex.aliases, "list_active",
                        lambda conn, cid, site_ids=None: [
                            {"wrong_term": "Mackon", "right_term": "McCahon"}])
    monkeypatch.setattr(reindex, "_company_id_for_site", lambda conn, sid: "co-1")

    s3 = FakeS3()
    key = reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-1", "Ada_L", "2026-07-16")
    assert key == "reindex_requests/2026-07-16/Ada_L/t-1.json"
    req = s3.puts[key]
    assert req["topic_id"] == "t-1"
    assert req["site_id"] == "s-1"
    assert req["report_key"] == "reports/2026-07-16/Ada_L/daily_report.json"
    assert req["aliases"] == [{"wrong_term": "Mackon", "right_term": "McCahon"}]
    assert any("Corrected slab" in c["chunk_text"] for c in req["topic_chunks"])


def test_apply_vectors_deletes_then_inserts(monkeypatch):
    deleted, inserted = {}, []
    monkeypatch.setattr(reindex.chunks, "delete_chunks_for_topic",
                        lambda conn, tid: deleted.setdefault("tid", tid))
    monkeypatch.setattr(reindex.chunks, "insert_chunk",
                        lambda conn, *a, **k: inserted.append((a, k)))
    result = {
        "topic_id": "t-1", "site_id": "s-1", "user_id": "u-9",
        "report_date": "2026-07-16",
        "source_s3_key": "reports/2026-07-16/Ada_L/daily_report.json",
        "chunks": [
            {"chunk_type": "topic", "chunk_text": "x", "metadata": {},
             "embedding": [0.1] * 1024},
        ],
    }
    n = reindex.apply_vectors(object(), result)
    assert n == 1
    assert deleted["tid"] == "t-1"
    assert inserted[0][1]["topic_id"] == "t-1"
    assert inserted[0][1]["source_s3_key"].endswith("daily_report.json")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_reindex.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'reindex'`.

- [ ] **Step 3: Write the implementation**

```python
# src/reindex.py
"""Per-topic RAG re-index (spec §5.3, D6). Split across the VPC boundary:
  - enqueue_topic_reindex  (in-VPC, org-api): reads the CORRECTED topic from
    Aurora, renders + chunks its 'topic' chunk texts, loads active aliases,
    and writes a request artifact to S3. It NEVER calls DashScope (BUG-36).
  - lambda_embed_report    (non-VPC): S3-event on the request -> embeds the
    topic chunks + this topic's alias-normalized transcript windows, writes a
    vectors result artifact. (Transcript S3 object is read, never written -- D4.)
  - apply_vectors          (in-VPC, ingest): S3-event on the vectors result ->
    delete_chunks_for_topic + insert_chunk with the durable topic_id.

The request/vectors handoff mirrors the existing embed->ingest sidecar
contract, just triggered by an edit instead of the nightly report and sourced
from corrected Aurora rows instead of daily_report.json."""
import json

from psycopg.rows import dict_row

from repositories import aliases, chunks, topics
from chunking import chunk_report

REQUEST_PREFIX = "reindex_requests/"


def request_key(date, folder, topic_id):
    return f"{REQUEST_PREFIX}{date}/{folder}/{topic_id}.json"


def vectors_key(date, folder, topic_id):
    return f"{REQUEST_PREFIX}{date}/{folder}/{topic_id}.vectors.json"


def _company_id_for_site(conn, site_id):
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT company_id FROM sites WHERE id=%s", (site_id,)).fetchone()
    return row["company_id"] if row else None


def enqueue_topic_reindex(s3_client, bucket, conn, topic_id, folder, date):
    """Build + write the reindex request for one corrected topic. Returns the
    S3 key, or None if the topic vanished (a concurrent supersession) -- the
    caller treats None as 'nothing to re-index', never an error (spec §6:
    re-index never rolls back the edit)."""
    # Imported here (not at module top) to avoid a circular import: reindex is
    # imported by lambda_org_api, which defines render_report_shape.
    from lambda_org_api import render_report_shape

    t = topics.get_topic_full(conn, topic_id)
    if t is None:
        return None
    site_id = str(t["site_id"])
    company_id = _company_id_for_site(conn, t["site_id"])
    active = aliases.list_active(conn, company_id, site_ids=[site_id]) if company_id else []
    alias_pairs = [{"wrong_term": a["wrong_term"], "right_term": a["right_term"]}
                   for a in active]

    shaped = render_report_shape([t], None, date, folder)
    topic_chunks = chunk_report(shaped)             # chunk_type='topic' only

    request = {
        "topic_id": str(topic_id),
        "site_id": site_id,
        "user_id": str(t["user_id"]) if t.get("user_id") is not None else None,
        "report_date": str(t["report_date"]),
        "source_s3_key": t["source_s3_key"],
        # report_key + topic_seq let the non-VPC embed lambda rebuild THIS
        # topic's transcript windows from the immutable daily_report.json.
        "report_key": t["source_s3_key"] if str(t["source_s3_key"]).startswith("reports/") else None,
        "topic_seq": shaped["topics"][0]["topic_id"] if shaped["topics"] else None,
        "folder": folder,
        "date": date,
        "aliases": alias_pairs,
        "topic_chunks": [{"chunk_type": c["chunk_type"], "chunk_text": c["chunk_text"],
                          "metadata": c["metadata"]} for c in topic_chunks],
    }
    key = request_key(date, folder, topic_id)
    s3_client.put_object(Bucket=bucket, Key=key,
                         Body=json.dumps(request), ContentType="application/json")
    return key


def apply_vectors(conn, result) -> int:
    """In-VPC: replace the topic's chunks with the freshly-embedded ones
    (D6 delete-and-replace). result['chunks'][*] each carry chunk_type,
    chunk_text, metadata, embedding (1024 floats)."""
    topic_id = result["topic_id"]
    chunks.delete_chunks_for_topic(conn, topic_id)
    n = 0
    for c in result.get("chunks", []):
        chunks.insert_chunk(
            conn, result["site_id"], result["report_date"],
            c["chunk_type"], c["chunk_text"], c["embedding"],
            user_id=result.get("user_id"),
            source_s3_key=result.get("source_s3_key"),
            topic_id=topic_id, metadata=c.get("metadata") or {},
        )
        n += 1
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_reindex.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add src/reindex.py tests/unit/test_reindex.py
git commit -m "feat: per-topic reindex request builder + apply (S3 handoff)"
```

---

### Task 10: Content-edit endpoint + audit + reindex enqueue + route

**Files:**
- Modify: `src/lambda_org_api.py` — imports, `dispatch` route table (~238), new `patch_content` handler (near `patch_action_item` ~1044)
- Test: append to `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `content.is_editable/get_content_row/update_content_field`, `content_edits.append_content_edit`, `text_normalize.diff_candidates`, `reindex.enqueue_topic_reindex`, `is_cross_company`, `resolve_scope`, `_allowed_site_ids`, `memberships.caller_site_roles`.
- Produces: `PATCH /api/org/content/{table}/{id}` → `{row, candidates}`; ACL is the D7 per-item tier (mirrors `patch_action_item`).

**D7 (binding):** "Per-item correction mirrors the existing task ACL (`patch_action_item`): author, the site's pm/site_manager, admin/gm, or platform_admin (cross-company)."

**Context:** `dispatch` runs inside `with get_connection() as conn:` (commits on clean return). Write the edit + audit inside that transaction (atomic + synchronous, spec §6), then enqueue re-index by writing the S3 artifact — a failure there is logged and swallowed (never rolls back the edit). Add `import reindex` and `from repositories import content, content_edits` and `from text_normalize import diff_candidates` to the module imports.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_lambda_org_api.py

def _wire_content(monkeypatch, row, *, cross=False):
    monkeypatch.setattr(org.content, "get_content_row", lambda conn, tbl, rid: dict(row))
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-1"})
    monkeypatch.setattr(org.memberships, "caller_site_roles",
                        lambda conn, uid: {"s-1": "site_manager"})
    monkeypatch.setattr(org, "is_cross_company", lambda role: cross)
    updated = {}
    monkeypatch.setattr(org.content, "update_content_field",
                        lambda conn, tbl, rid, f, v: updated.update({f: v}) or {"id": rid, f: v})
    audit = {}
    monkeypatch.setattr(org.content_edits, "append_content_edit",
                        lambda conn, *a, **k: audit.update({"args": a}) or {"id": "e-1"})
    monkeypatch.setattr(org.reindex, "enqueue_topic_reindex",
                        lambda *a, **k: "reindex_requests/x.json")
    return updated, audit


CONTENT_ROW = {"id": "t-1", "site_id": "s-1", "company_id": "c-uuid-1",
               "author_user_id": "u-9", "title": "Mackon slab", "summary": "s"}


def test_patch_content_writes_audit_and_returns_candidates(wired, monkeypatch):
    updated, audit = _wire_content(monkeypatch, CONTENT_ROW)
    res = org.dispatch(FakeConn(),
                       make_event("PATCH", "/api/org/content/topics/t-1",
                                  body={"title": "McCahon slab"}),
                       "PATCH", "/content/topics/t-1")
    body = body_of(res)
    assert res["statusCode"] == 200
    assert updated["title"] == "McCahon slab"
    # audit: (company_id, table, row_id, field, before, after, actor_user, actor_role)
    assert audit["args"][4] == "Mackon slab"
    assert audit["args"][5] == "McCahon slab"
    assert "McCahon" in body["candidates"]               # D2 diff candidate


def test_patch_content_rejects_non_whitelisted_field(wired, monkeypatch):
    _wire_content(monkeypatch, CONTENT_ROW)
    res = org.dispatch(FakeConn(),
                       make_event("PATCH", "/api/org/content/topics/t-1",
                                  body={"category": "safety"}),
                       "PATCH", "/content/topics/t-1")
    assert res["statusCode"] == 400


def test_patch_content_outsider_denied(wired, monkeypatch):
    _wire_content(monkeypatch, CONTENT_ROW)
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-OTHER"})
    res = org.dispatch(FakeConn(),
                       make_event("PATCH", "/api/org/content/topics/t-1",
                                  body={"title": "x"}),
                       "PATCH", "/content/topics/t-1")
    assert res["statusCode"] == 403


def test_patch_content_cross_company_platform_admin_allowed(wired, monkeypatch):
    other = dict(CONTENT_ROW, company_id="c-OTHER")
    _wire_content(monkeypatch, other, cross=True)
    res = org.dispatch(FakeConn(),
                       make_event("PATCH", "/api/org/content/topics/t-1",
                                  body={"title": "x"}),
                       "PATCH", "/content/topics/t-1")
    assert res["statusCode"] == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "patch_content" -v`
Expected: FAIL — route returns 404 (`patch_content` + route not wired).

- [ ] **Step 3a: Add imports** near the other `from repositories import ...` lines at the top of `src/lambda_org_api.py`:

```python
import reindex
from repositories import content, content_edits
from text_normalize import diff_candidates
```

- [ ] **Step 3b: Wire routes** in `dispatch`, immediately after the `/action-items/{id}` block (~line 240):

```python
    m_ce = re.match(r"^/content/([^/]+)/([^/]+)$", route)
    if m_ce and method == "PATCH":
        return patch_content(conn, caller, m_ce.group(1), m_ce.group(2), parse_body(event))
    m_ch = re.match(r"^/content/([^/]+)/([^/]+)/history$", route)
    if m_ch and method == "GET":
        return get_content_history(conn, caller, m_ch.group(1), m_ch.group(2))
```

- [ ] **Step 3c: Add the handler** near `patch_action_item`:

```python
def patch_content(conn, caller, table, row_id, body):
    """Edit one free-text content field (spec §3/§5.2, D1). ACL is the D7
    per-item tier -- mirrors patch_action_item exactly: platform_admin
    (cross-company) edits any tenant; company roles stay pinned; the row's
    site must be in the caller's reach; and the caller must be admin/gm, THIS
    site's pm/site_manager, or the item's author (the owning topic's user).
    Writes the corrected text + a content_edits audit row atomically, then
    enqueues a best-effort per-topic re-index (never rolls back the edit)."""
    if body is None:
        return error("malformed JSON body", 400)
    if table not in content.EDITABLE:
        return error(f"table must be one of {sorted(content.EDITABLE)}", 400)
    # Exactly one whitelisted field per request (D1 whole-field edit).
    fields = {k: v for k, v in body.items() if content.is_editable(table, k)}
    if len(fields) != 1:
        return error("exactly one editable field required", 400)
    field, value = next(iter(fields.items()))
    if not isinstance(value, str):
        return error("value must be a string", 400)

    row = content.get_content_row(conn, table, row_id)
    cross = is_cross_company(caller["global_role"])
    if row is None or (not cross and str(row["company_id"]) != str(caller["company_id"])):
        return error("content row not found", 404)      # incl. cross-company
    site_id = str(row["site_id"])
    if site_id not in _allowed_site_ids(conn, caller):
        return error("access denied to this content's site", 403)
    site_role = memberships.caller_site_roles(conn, caller["id"]).get(site_id)
    is_admin = resolve_scope(caller["global_role"]) == "ALL" or cross
    is_site_authority = site_role in ("pm", "site_manager")
    is_author = row.get("author_user_id") is not None and \
        str(row["author_user_id"]) == str(caller["id"])
    if not (is_admin or is_site_authority or is_author):
        return error("admin/gm, this site's pm/site_manager, or the author only", 403)

    before = row.get(field)
    updated = content.update_content_field(conn, table, row_id, field, value)
    if updated is None:
        return error("content row not found", 404)
    content_edits.append_content_edit(
        conn, row["company_id"], table, row_id, field, before, value,
        caller["id"], caller["global_role"])

    # Best-effort per-topic re-index (spec §6: async, never blocks/rolls back
    # the edit). Topic id + folder/date come from the row's owning topic.
    try:
        _enqueue_content_reindex(conn, table, row_id)
    except Exception:
        logger.exception("content edit %s/%s: reindex enqueue failed (edit kept)",
                          table, row_id)

    candidates = diff_candidates(before or "", value)
    return ok({"row": updated, "candidates": candidates})


def _enqueue_content_reindex(conn, table, row_id):
    """Resolve the edited row's owning topic + its (folder, date), then write
    the reindex request artifact. topic_id = the row itself for `topics`, else
    the child's topic_id."""
    if table == "topics":
        tid = row_id
    else:
        r = conn.cursor(row_factory=RealDictRow).execute(
            f"SELECT topic_id FROM {table} WHERE id=%s", (row_id,)).fetchone()
        if not r:
            return
        tid = r["topic_id"]
    meta = conn.cursor(row_factory=RealDictRow).execute(
        "SELECT t.report_date, u.folder_name FROM topics t "
        "LEFT JOIN users u ON u.id = t.user_id WHERE t.id=%s", (tid,)).fetchone()
    if not meta or not meta.get("folder_name"):
        return                                          # unattributed -> skip re-index
    # LAKE_BUCKET (IngestBucketName) is the lake the embed/ingest lambdas read;
    # org-api's S3_BUCKET is DataBucketName, a DIFFERENT bucket. The reindex
    # chain lives on the lake, so enqueue writes there (see Task 20 IAM grant).
    reindex.enqueue_topic_reindex(s3(), LAKE_BUCKET, conn, tid,
                                  meta["folder_name"], str(meta["report_date"]))
```

Add `from psycopg.rows import dict_row as RealDictRow` to the imports if not already present (it is used above for the two ad-hoc reads). `LAKE_BUCKET` is already a module constant in `lambda_org_api.py` (env `LAKE_BUCKET: !Ref IngestBucketName`, used by `_get_lake_json`).

- [ ] **Step 3d: Add the history handler**:

```python
def get_content_history(conn, caller, table, row_id):
    """content_edits trail for one row (spec §5.5 History view). Company-guarded
    via get_content_row (which also resolves cross-company for platform_admin)."""
    if table not in content.EDITABLE:
        return error(f"table must be one of {sorted(content.EDITABLE)}", 400)
    row = content.get_content_row(conn, table, row_id)
    cross = is_cross_company(caller["global_role"])
    if row is None or (not cross and str(row["company_id"]) != str(caller["company_id"])):
        return error("content row not found", 404)
    edits = content_edits.list_content_edits(conn, row["company_id"], table, row_id)
    return ok({"edits": edits})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "patch_content or content_history" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(api): PATCH /content/{table}/{id} + audit + history + reindex enqueue"
```

---

### Task 11: Glossary-confirm endpoint (`POST /api/org/aliases`, site_manager+)

**Files:**
- Modify: `src/lambda_org_api.py` — route (~285) + `create_alias_endpoint` handler
- Test: append to `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Consumes: `aliases.create_alias`, `_resolve_site_param` (for optional site scope), `hasMinimumRole`-equivalent role gate.
- Produces: `POST /api/org/aliases` body `{wrong_term, right_term, kind?, site?}` → the created alias row. D7 alias tier: **site_manager+**.

**D7 (binding):** "Promoting a correction to a company/site alias requires site_manager+ (higher stakes — affects RAG + future STT company-wide)."

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_lambda_org_api.py

def test_create_alias_requires_site_manager_plus(wired, monkeypatch):
    worker = dict(CALLER, global_role="worker")
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(worker))
    res = org.dispatch(FakeConn(),
                       make_event("POST", "/api/org/aliases",
                                  body={"wrong_term": "Mackon", "right_term": "McCahon"}),
                       "POST", "/aliases")
    assert res["statusCode"] == 403


def test_create_alias_company_wide_by_site_manager(wired, monkeypatch):
    sm = dict(CALLER, global_role="site_manager")
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(sm))
    monkeypatch.setattr(org.aliases, "create_alias",
                        lambda conn, cid, sid, w, r, kind, by, source="correction":
                        {"id": "a-1", "wrong_term": w, "right_term": r,
                         "site_id": sid, "kind": kind})
    res = org.dispatch(FakeConn(),
                       make_event("POST", "/api/org/aliases",
                                  body={"wrong_term": "Mackon", "right_term": "McCahon",
                                        "kind": "company"}),
                       "POST", "/aliases")
    body = body_of(res)
    assert res["statusCode"] == 200
    assert body["right_term"] == "McCahon"
    assert body["site_id"] is None                       # no ?site -> company-wide
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "create_alias" -v`
Expected: FAIL — 404 (route not wired).

- [ ] **Step 3a: Wire the route** in `dispatch`, after the `/content/...` routes:

```python
    if route == "/aliases" and method == "POST":
        return create_alias_endpoint(conn, caller, parse_body(event), event)
```

- [ ] **Step 3b: Add the handler** (place near `patch_content`); reuse the same `_ALIAS_KINDS` guard:

```python
_ALIAS_KINDS = ("person", "product", "company", "other")


def create_alias_endpoint(conn, caller, body, event):
    """Confirm a diff candidate into a scoped name_aliases row (spec §5.4, D5,
    D2 glossary confirm). D7 alias tier: site_manager+ only. Optional ?site=
    scopes it to one site; absent => company-wide (site_id NULL)."""
    if body is None:
        return error("malformed JSON body", 400)
    if caller["global_role"] not in ("site_manager", "pm", "gm", "admin", "platform_admin"):
        return error("site_manager or above required to add a glossary alias", 403)
    wrong = (body.get("wrong_term") or "").strip()
    right = (body.get("right_term") or "").strip()
    if not wrong or not right:
        return error("wrong_term and right_term are required", 400)
    kind = body.get("kind") or "other"
    if kind not in _ALIAS_KINDS:
        return error(f"kind must be one of {sorted(_ALIAS_KINDS)}", 400)
    site_id = None
    site_param = (event.get("queryStringParameters") or {}).get("site")
    if site_param:
        site_id, err = _resolve_site_param(conn, caller, site_param)
        if err is not None:
            return err
    row = aliases.create_alias(conn, caller["company_id"], site_id, wrong, right,
                               kind, caller["id"], source="correction")
    return ok(row)
```

Add `from repositories import aliases` to the imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_lambda_org_api.py -k "create_alias" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(api): POST /aliases glossary confirm (site_manager+)"
```

---

# Phase C — Re-index workers + RAG safety net

### Task 12: Embed lambda — reindex-request mode (non-VPC)

**Files:**
- Modify: `src/lambda_embed_report.py` — `lambda_handler` router + new `embed_reindex_request`
- Test: `tests/unit/test_lambda_embed_report_reindex.py`

**Interfaces:**
- Consumes: the request artifact from Task 9; `dashscope_utils.embed`, `lambda_ingest._load_turns`, `chunking.chunk_transcripts`, `text_normalize.normalize`, `reindex.vectors_key`.
- Produces: writes the vectors result artifact `reindex_requests/{date}/{folder}/{topic_id}.vectors.json` = `{topic_id, site_id, user_id, report_date, source_s3_key, chunks:[{chunk_type, chunk_text, metadata, embedding}]}`.

**Context:** This lambda is non-VPC (reaches DashScope) and already imports `lambda_ingest` + `chunking`. It embeds the request's `topic_chunks` (corrected text) plus this topic's transcript windows, which it rebuilds from the immutable `daily_report.json` (via `report_key`, filtered to `topic_seq`) and alias-normalizes with the request's `aliases` (D4 — transcript S3 read, never written). If `report_key` is absent (extraction-sourced edit) it embeds only the topic chunks.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lambda_embed_report_reindex.py
import json

import pytest

mod = pytest.importorskip("lambda_embed_report",
                          reason="requires psycopg (installed in CI)")


class FakeS3:
    def __init__(self, objects):
        self._objects = objects
        self.puts = {}

    def get_object(self, Bucket, Key):
        return {"Body": type("B", (), {"read": lambda s: self._objects[Key]})()}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts[Key] = json.loads(Body)


def test_reindex_event_embeds_topic_chunks_and_writes_vectors(monkeypatch):
    req = {
        "topic_id": "t-1", "site_id": "s-1", "user_id": "u-9",
        "report_date": "2026-07-16",
        "source_s3_key": "reports/2026-07-16/Ada_L/daily_report.json",
        "report_key": None, "topic_seq": 0, "folder": "Ada_L", "date": "2026-07-16",
        "aliases": [], "topic_chunks": [
            {"chunk_type": "topic", "chunk_text": "Corrected slab", "metadata": {}}],
    }
    key = "reindex_requests/2026-07-16/Ada_L/t-1.json"
    s3 = FakeS3({key: json.dumps(req).encode("utf-8")})
    monkeypatch.setattr(mod, "s3", lambda: s3)
    monkeypatch.setattr(mod.dashscope_utils, "embed", lambda texts: [[0.5] * 1024 for _ in texts])

    out = mod.lambda_handler({"Records": [{"s3": {"object": {"key": key}}}]}, None)
    vkey = "reindex_requests/2026-07-16/Ada_L/t-1.vectors.json"
    assert vkey in s3.puts
    result = s3.puts[vkey]
    assert result["topic_id"] == "t-1"
    assert result["chunks"][0]["chunk_text"] == "Corrected slab"
    assert len(result["chunks"][0]["embedding"]) == 1024
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_lambda_embed_report_reindex.py -v`
Expected: FAIL — the reindex key is skipped as a non-report key (`skipping non-report S3 key`), no `.vectors.json` written.

- [ ] **Step 3: Implement the reindex branch**

In `src/lambda_embed_report.py`, add imports and a router branch. At the top, add:

```python
import reindex
import text_normalize
from chunking import chunk_transcripts
```

Add the constant and function:

```python
REINDEX_REQUEST_RE = re.compile(
    r"^reindex_requests/[^/]+/[^/]+/[^/]+\.json$")


def embed_reindex_request(key):
    """Non-VPC reindex worker (spec §5.3). Embeds the corrected topic chunks +
    this topic's alias-normalized transcript windows, writes the vectors
    result artifact for the in-VPC apply step. Transcript S3 is read, never
    written (D4)."""
    req = json.loads(s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode("utf-8"))
    chunks_out = [dict(c) for c in req.get("topic_chunks", [])]

    # Rebuild THIS topic's transcript windows from the immutable report doc.
    if req.get("report_key") and req.get("topic_seq") is not None:
        try:
            raw = s3().get_object(Bucket=S3_BUCKET, Key=req["report_key"])["Body"].read()
            report = json.loads(raw.decode("utf-8"))
            one = [t for t in report.get("topics", [])
                   if t.get("topic_id") == req["topic_seq"]]
            if one:
                turns = lambda_ingest._load_turns(req["folder"], req["date"])
                for c in chunk_transcripts({"topics": one, **{k: report.get(k)
                                            for k in ("user_name", "site", "report_date")}}, turns):
                    text = text_normalize.normalize(c["chunk_text"], req.get("aliases") or [])
                    chunks_out.append({"chunk_type": c["chunk_type"], "chunk_text": text,
                                       "metadata": c["metadata"]})
        except Exception:
            logger.exception("reindex %s: transcript window rebuild failed (topic chunks only)", key)

    if not chunks_out:
        logger.info("reindex %s: no chunks -- skipping", key)
        return {"reindex": key, "chunks": 0}

    embeddings = dashscope_utils.embed([c["chunk_text"] for c in chunks_out])
    for c, e in zip(chunks_out, embeddings):
        c["embedding"] = e

    result = {
        "topic_id": req["topic_id"], "site_id": req["site_id"],
        "user_id": req.get("user_id"), "report_date": req["report_date"],
        "source_s3_key": req.get("source_s3_key"), "chunks": chunks_out,
    }
    vkey = reindex.vectors_key(req["date"], req["folder"], req["topic_id"])
    s3().put_object(Bucket=S3_BUCKET, Key=vkey,
                    Body=json.dumps(result), ContentType="application/json")
    logger.info("reindex embedded %s chunks=%d", key, len(chunks_out))
    return {"reindex": key, "chunks": len(chunks_out)}
```

In `lambda_handler`, route reindex-request keys BEFORE the existing report-key branch (skip `.vectors.json`, which is the apply lambda's input, not this one's):

```python
    for record in event.get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])
        if key.endswith(".vectors.json"):
            continue                                    # apply-side input, not ours
        if REINDEX_REQUEST_RE.match(key):
            results.append(embed_reindex_request(key))
            continue
        m = REPORT_KEY_RE.match(key)
        if not m:
            logger.warning("skipping non-report S3 key: %s", key)
            continue
        date, user_folder = m.group(1), m.group(2)
        results.append(embed_report(date, user_folder, key))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_lambda_embed_report_reindex.py -v`
Expected: PASS. Re-run the existing embed test to prove no regression:
Run: `uv run pytest tests/unit/test_lambda_embed_report.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_embed_report.py tests/unit/test_lambda_embed_report_reindex.py
git commit -m "feat(embed): reindex-request mode (embed corrected topic + normalized windows)"
```

---

### Task 13: Ingest lambda — reindex-vectors apply mode (in-VPC)

**Files:**
- Modify: `src/lambda_ingest.py` — `lambda_handler` router + `apply_reindex_vectors`
- Test: `tests/unit/test_lambda_ingest_reindex.py`

**Interfaces:**
- Consumes: the vectors result artifact from Task 12; `reindex.apply_vectors`, `get_connection`.
- Produces: on `reindex_requests/*.vectors.json` S3 events, replaces the topic's chunks in Aurora.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lambda_ingest_reindex.py
import json

import pytest

mod = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeS3:
    def __init__(self, objects):
        self._objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": type("B", (), {"read": lambda s: self._objects[Key]})()}


def test_vectors_event_applies_reindex(monkeypatch):
    result = {"topic_id": "t-1", "site_id": "s-1", "user_id": "u-9",
              "report_date": "2026-07-16",
              "source_s3_key": "reports/2026-07-16/Ada_L/daily_report.json",
              "chunks": [{"chunk_type": "topic", "chunk_text": "x",
                          "metadata": {}, "embedding": [0.1] * 1024}]}
    vkey = "reindex_requests/2026-07-16/Ada_L/t-1.vectors.json"
    s3 = FakeS3({vkey: json.dumps(result).encode("utf-8")})
    monkeypatch.setattr(mod, "s3", lambda: s3)
    monkeypatch.setattr(mod, "get_connection", lambda: FakeConn())
    applied = {}
    monkeypatch.setattr(mod.reindex, "apply_vectors",
                        lambda conn, res: applied.update({"topic": res["topic_id"]}) or 1)

    out = mod.lambda_handler({"Records": [{"s3": {"object": {"key": vkey}}}]}, None)
    assert applied["topic"] == "t-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_lambda_ingest_reindex.py -v`
Expected: FAIL — vectors key not handled (`apply_vectors` never called).

- [ ] **Step 3: Implement the apply branch**

In `src/lambda_ingest.py`, add `import reindex` and, in `lambda_handler`, route `.vectors.json` keys to a new function BEFORE the existing report-key handling:

```python
REINDEX_VECTORS_RE = re.compile(
    r"^reindex_requests/[^/]+/[^/]+/[^/]+\.vectors\.json$")


def apply_reindex_vectors(key):
    """In-VPC reindex apply (spec §5.3): read the embedded result artifact and
    replace the topic's chunks (delete_chunks_for_topic + insert_chunk)."""
    result = json.loads(s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode("utf-8"))
    with get_connection() as conn:
        n = reindex.apply_vectors(conn, result)
    logger.info("reindex applied %s chunks=%d", key, n)
    return {"reindex_applied": key, "chunks": n}
```

In the `lambda_handler` record loop, add the guard first:

```python
        key = unquote_plus(record["s3"]["object"]["key"])
        if REINDEX_VECTORS_RE.match(key):
            results.append(apply_reindex_vectors(key))
            continue
```

(`re` is already imported in `lambda_ingest`; confirm at the top.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_lambda_ingest_reindex.py -v`
Expected: PASS. Re-run existing ingest tests:
Run: `uv run pytest tests/unit/test_lambda_ingest.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_ingest.py tests/unit/test_lambda_ingest_reindex.py
git commit -m "feat(ingest): reindex-vectors apply mode (delete + re-insert topic chunks)"
```

---

### Task 14: RAG synthesis normalize safety net (`lambda_rag_search`)

**Files:**
- Modify: `src/lambda_rag_search.py`
- Test: `tests/unit/test_lambda_rag_search_normalize.py`

**Interfaces:**
- Consumes: `aliases.list_active`, `text_normalize.normalize`.
- Produces: retrieved `chunk_text` values are alias-normalized (in-VPC, where Aurora is reachable) before returning to the non-VPC ask-agent synthesizer.

**Spec §4 (binding):** "synthesis also applies `normalize()` to retrieved chunk text before the LLM (catches any chunk not yet re-embedded — eventual-consistency safety net)." `lambda_rag_search` is the correct home: it is in-VPC (has an Aurora connection to load aliases), whereas `lambda_ask_agent` is non-VPC (no Aurora). This is belt-and-suspenders over the per-topic re-embed and does not conflict with D5 (reads only; historic rows are not rewritten).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lambda_rag_search_normalize.py
import pytest

mod = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")


class Conn:
    pass


def test_retrieved_chunk_text_is_alias_normalized(monkeypatch):
    monkeypatch.setattr(mod, "get_cached_connection", lambda: Conn())
    monkeypatch.setattr(mod.users, "get_user_by_sub",
                        lambda conn, sub: {"id": "u-1", "company_id": "co-1",
                                           "global_role": "admin"})
    monkeypatch.setattr(mod.sites, "list_company_sites",
                        lambda conn, cid: [{"id": "s-1"}])
    monkeypatch.setattr(mod.chunks, "search_chunks",
                        lambda conn, qv, ids, k=8, date_from=None, date_to=None:
                        [{"id": "c-1", "chunk_text": "Mackon poured the slab",
                          "site_id": "s-1", "topic_id": "t-1", "report_date": "2026-07-16"}])
    monkeypatch.setattr(mod.aliases, "list_active",
                        lambda conn, cid, site_ids=None: [
                            {"wrong_term": "Mackon", "right_term": "McCahon"}])

    out = mod.lambda_handler({"sub": "sub-1", "query_embedding": [0.1] * 1024}, None)
    assert out["chunks"][0]["chunk_text"] == "McCahon poured the slab"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_lambda_rag_search_normalize.py -v`
Expected: FAIL — chunk text unchanged (`Mackon...`), `aliases` not imported.

- [ ] **Step 3: Implement**

In `src/lambda_rag_search.py`, add to the imports:

```python
from repositories import aliases
import text_normalize
```

Replace the tail (from `rows = chunks.search_chunks(...)` to the return) with:

```python
    rows = chunks.search_chunks(conn, qv, site_ids, k=k,
                                date_from=date_from, date_to=date_to)
    # Synthesis-time safety net (spec §4): normalize retrieved chunk text with
    # the company's active aliases, so a chunk not yet re-embedded still reads
    # corrected before the LLM. site_ids here are the caller's accessible sites.
    active = aliases.list_active(conn, caller["company_id"], site_ids=[str(s) for s in site_ids])
    alias_pairs = [{"wrong_term": a["wrong_term"], "right_term": a["right_term"]}
                   for a in active]
    if alias_pairs:
        for r in rows:
            if r.get("chunk_text"):
                r["chunk_text"] = text_normalize.normalize(r["chunk_text"], alias_pairs)

    # search_chunks returns raw psycopg rows: id/site_id/topic_id are uuid.UUID
    # and report_date is datetime.date -- Lambda's JSON marshaller can't
    # serialize either. Coerce to plain strings before returning.
    rows = json.loads(json.dumps(rows, default=str))
    return {"chunks": rows, "site_count": len(site_ids)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_lambda_rag_search_normalize.py -v`
Expected: PASS. Re-run existing rag-search tests:
Run: `uv run pytest tests/unit/test_lambda_rag_search.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_rag_search.py tests/unit/test_lambda_rag_search_normalize.py
git commit -m "feat(rag): synthesis-time normalize() safety net for retrieved chunks"
```

---

# Phase D — Frontend

> Frontend repo: `C:/Users/camil/Dropbox/fieldsight-ui`. No build step. Verify each changed file with `node --check`. Bump `?v=` cache busters (Task 19). All examples use `React.createElement` and `.fs-*` BEM; never JS hex tokens.

### Task 15: `content:edit` permission in the roles model

**Files:**
- Modify: `C:/Users/camil/Dropbox/fieldsight-ui/scripts/roles.js`

**Interfaces:**
- Produces: `P('content','edit', scope)` present for site_manager (SITE), project_manager (PROJECT), and every role above; the UI gate `FS.can(user, FS.P('content','edit'))` returns true for them and for `isAdmin`.

**Context:** mirrors the existing `P('task','edit',SCOPES.PROJECT)` line ("F1b — own-content edit UI gate parity"). This is a UX-only gate; the backend `patch_content` ACL is authoritative (author can edit even without the role perm — the page also ORs in `isOwnReport`, Task 17).

- [ ] **Step 1: Add the permission lines**

In `scripts/roles.js`, add `P('content', 'edit', SCOPES.SITE)` to the `site_manager` permissions array (next to `P('task','manage',SCOPES.SITE)`), and `P('content', 'edit', SCOPES.PROJECT)` to `project_manager` (next to its `P('task','edit',SCOPES.PROJECT)` line). Add the analogous `content:edit` at each role's own scope for gm/admin/platform_admin (or rely on `isAdmin === true` short-circuit in `can()` for admin). Example for `site_manager`:

```javascript
      P('task',      'manage',  SCOPES.SITE),
      P('content',   'edit',    SCOPES.SITE),   // editable content correction (spec §5.5)
```

- [ ] **Step 2: Syntax check**

Run: `cd C:/Users/camil/Dropbox/fieldsight-ui && node --check scripts/roles.js`
Expected: no output (valid).

- [ ] **Step 3: Manual gate check**

Add a temporary node smoke (or verify by inspection) that `getPermissionsForRole('site_manager')` includes `'content:edit:site'` and `getPermissionsForRole('worker')` does not. Remove the smoke after confirming.

- [ ] **Step 4: Commit**

```bash
git add scripts/roles.js
git commit -m "feat(roles): content:edit permission (site_manager+)"
```

---

### Task 16: API layer — `updateContent`, `getContentHistory`, `confirmAlias`

**Files:**
- Modify: `C:/Users/camil/Dropbox/fieldsight-ui/scripts/api/actions.js`

**Interfaces:**
- Produces (attached to `window.FS.api.actions`):
  - `updateContent(table, id, patch) -> Promise` — `orgRequest('/content/'+table+'/'+id, {method:'PATCH', body:patch})`; resolves `{row, candidates}` or `{_accessDenied}`/`{_notFound}`; mock merges the patch.
  - `getContentHistory(table, id) -> Promise` — `orgRequest('/content/'+table+'/'+id+'/history')`; resolves `{edits:[...]}`.
  - `confirmAlias(body) -> Promise` — `orgRequest('/aliases', {method:'POST', body})`.

**Context:** mirrors `updateAction` exactly (which already rides `orgRequest` and handles the `_accessDenied`/`_notFound` envelopes). Add alongside it and export from the `window.FS.api.actions` object.

- [ ] **Step 1: Add the functions** after `updateAction` in `scripts/api/actions.js`:

```javascript
  /* editable-content-correction — PATCH one free-text content field
     (topic title/summary, action_items.text/responsible, findings.*,
     safety_observations.observation) by its durable Aurora id. AURORA org
     write (PATCH /api/org/content/{table}/{id}), mirrors updateAction.
     Resolves {row, candidates} on success (candidates = D2 glossary diff
     terms), or {_accessDenied}/{_notFound}. Mock returns the merged patch. */
  async function updateContent(table, id, patch) {
    if (!window.FS.api.useMocks && !window.FS.api.writeMocks) {
      return window.FS.api.orgRequest(
        '/content/' + encodeURIComponent(table) + '/' + encodeURIComponent(id),
        { method: 'PATCH', body: patch || {} });
    }
    await window.FS.api.delay(60);
    return { row: Object.assign({ id: id }, patch || {}), candidates: [] };
  }

  /* editable-content-correction — content_edits trail for one row. */
  async function getContentHistory(table, id) {
    if (!window.FS.api.useMocks) {
      return window.FS.api.orgRequest(
        '/content/' + encodeURIComponent(table) + '/' + encodeURIComponent(id) + '/history');
    }
    await window.FS.api.delay(40);
    return { edits: [] };
  }

  /* editable-content-correction — confirm a glossary candidate into a scoped
     name_aliases row (site_manager+ enforced server-side). */
  async function confirmAlias(body) {
    if (!window.FS.api.useMocks && !window.FS.api.writeMocks) {
      return window.FS.api.orgRequest('/aliases', { method: 'POST', body: body || {} });
    }
    await window.FS.api.delay(60);
    return Object.assign({ id: 'mock-alias' }, body || {});
  }
```

- [ ] **Step 2: Export** — extend the `window.FS.api.actions = {...}` object:

```javascript
  window.FS.api.actions = {
    getActions:      getActions,
    getActionsRange: getActionsRange,
    toggleAction:    toggleAction,
    createAction:    createAction,
    updateAction:    updateAction,
    updateContent:   updateContent,
    getContentHistory: getContentHistory,
    confirmAlias:    confirmAlias,
    actionKey:       actionKey,
    lookupAction:    lookupAction,
  };
```

- [ ] **Step 3: Syntax check**

Run: `node --check scripts/api/actions.js`
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add scripts/api/actions.js
git commit -m "feat(api): updateContent/getContentHistory/confirmAlias"
```

---

### Task 17: Timeline inline content editors + durable-id threading

**Files:**
- Modify: `C:/Users/camil/Dropbox/fieldsight-ui/scripts/pages/timeline.js` — `TopicDetail` (~1287) and title render (~1635)

**Interfaces:**
- Consumes: `FS.api.actions.updateContent`, `FS.can`, `FS.P`; the backend now stamps `topic.topic_row_id`, `topic.action_items[].id`, `topic.safety_flags[].id`+`.source_table`, `topic.findings[].id` (Task 8).
- Produces: inline free-text editors on the topic title, summary, each action-item text, each safety flag observation, and each finding observation/recommended_action, each committing via `updateContent(table, id, {field: value})`.

**Context:** mirror `tasks.js`'s editable-field pattern (`commitRowField`: optimistic draft → `updateAction` → toast on `_accessDenied`/`_notFound`/throw). Gate rendering on `canEditContent = FS.can(caller, FS.P('content','edit')) || isOwnReport` (UX-only; backend enforces). A small reusable `EditableText` (textarea, blur-to-commit) keeps it DRY.

- [ ] **Step 1: Add the `EditableText` helper** inside the timeline IIFE (near the other local components):

```javascript
  /* editable-content-correction — inline free-text editor. Blur (or Ctrl+Enter)
     commits via updateContent(table, id, {field: value}); optimistic, reverts +
     toasts on failure. Read-only fallback renders `display`. */
  function EditableText(props) {
    var editable = props.editable;
    var ref = React.useState(props.value || '');
    var value = ref[0], setValue = ref[1];
    var busyRef = React.useState(false);
    var busy = busyRef[0], setBusy = busyRef[1];

    if (!editable) {
      return React.createElement(props.tag || 'span',
        { className: props.className }, props.display != null ? props.display : (props.value || '—'));
    }
    function commit() {
      var next = value;
      if (next === (props.value || '')) return;
      setBusy(true);
      window.FS.api.actions.updateContent(props.table, props.id, (function () {
        var p = {}; p[props.field] = next; return p;
      })()).then(function (res) {
        setBusy(false);
        if (!res || res._accessDenied || res._notFound) {
          setValue(props.value || '');
          var toast = window.FS && window.FS.toast;
          if (toast) toast.show({ message: (res && res.error) || 'Could not save edit',
                                  tone: 'error', duration: 5000 });
          return;
        }
        if (props.onSaved) props.onSaved(res);
      }).catch(function () {
        setBusy(false);
        setValue(props.value || '');
        var toast = window.FS && window.FS.toast;
        if (toast) toast.show({ message: 'Could not save edit', tone: 'error', duration: 5000 });
      });
    }
    return React.createElement('textarea', {
      className: 'fs-content-edit' + (busy ? ' fs-content-edit--busy' : ''),
      value: value, rows: props.rows || 2, disabled: busy,
      'aria-label': props.ariaLabel || props.field,
      onChange: function (e) { setValue(e.target.value); },
      onBlur: commit,
      onKeyDown: function (e) { if (e.ctrlKey && e.key === 'Enter') commit(); },
    });
  }
```

- [ ] **Step 2: Compute the gate + thread it into `TopicDetail`.** At the top of `TopicDetail(props)`, add:

```javascript
    var caller = (window.AuthMock && window.AuthMock.currentUser) || null;
    var canEditContent = !!(window.FS && window.FS.can && window.FS.P
        && window.FS.can(caller, window.FS.P('content', 'edit'))) || !!props.isOwnReport;
    var topicRowId = topic.topic_row_id;   // durable topics.id (backend Task 8)
```

Replace the summary `<p>` (`topic.summary ? React.createElement('p', {className:'fs-topic-detail__summary'}, topic.summary) : null`) with:

```javascript
      React.createElement(EditableText, {
        editable: canEditContent && !!topicRowId, table: 'topics', id: topicRowId,
        field: 'summary', value: topic.summary || '', display: topic.summary,
        tag: 'p', className: 'fs-topic-detail__summary', rows: 3,
        ariaLabel: 'Edit topic summary',
      }),
```

- [ ] **Step 3: Make each action item text editable.** In the `actions.map(...)` block (~1319), wrap the rendered action text in an `EditableText` bound to `updateContent('action_items', a.id, {text})`, gated on `canEditContent && !!a.id`. Keep the existing check-off/owner rendering intact — only the text becomes editable.

- [ ] **Step 4: Make safety flags + findings editable.** For each `flags` entry render `EditableText` bound to `updateContent(flag.source_table, flag.id, {observation})` (gated on `canEditContent && !!flag.id`). For each finding, bind `observation` and `recommended_action` to `updateContent('findings', f.id, {...})`.

- [ ] **Step 5: Make the topic title editable** at the title render (~1635): replace `topic.topic_title || '(untitled)'` with an `EditableText` bound to `updateContent('topics', topicRowId, {title})` (single row), read-only fallback `topic.topic_title || '(untitled)'`.

- [ ] **Step 6: Syntax check**

Run: `node --check scripts/pages/timeline.js`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add scripts/pages/timeline.js
git commit -m "feat(timeline): inline content editors + durable-id threading"
```

---

### Task 18: Content History panel + glossary confirm

**Files:**
- Modify: `C:/Users/camil/Dropbox/fieldsight-ui/scripts/pages/timeline.js`

**Interfaces:**
- Consumes: `FS.api.actions.getContentHistory`, `FS.api.actions.confirmAlias`; the `{candidates}` returned by `updateContent` (Task 16/17).
- Produces: (a) a History view per topic (reuses the Details/History tab pattern) showing the `content_edits` trail; (b) after a successful edit that returns diff candidates, a "Add to glossary" confirm affordance that calls `confirmAlias`.

**Context:** mirrors `tasks.js`'s `ActionHistoryPanel` (fetch on mount, render a list) and the `EvidenceTabs` Details/History split. The glossary confirm is site_manager+ (backend enforces; UX-gate on `FS.can(caller, FS.P('content','edit'))` plus role ≥ site_manager).

- [ ] **Step 1: Add a `ContentHistoryPanel`** in the timeline IIFE, mirroring `tasks.js` `ActionHistoryPanel`: on mount call `FS.api.actions.getContentHistory(table, id)`, render each edit as `field: "before" → "after"` with actor + timestamp. Empty state: "No edits yet."

```javascript
  function ContentHistoryPanel(props) {
    var dataRef = React.useState({ status: 'loading' });
    var data = dataRef[0], setData = dataRef[1];
    React.useEffect(function () {
      var alive = true;
      window.FS.api.actions.getContentHistory(props.table, props.id).then(function (res) {
        if (!alive) return;
        setData({ status: 'ok', edits: (res && res.edits) || [] });
      }).catch(function () { if (alive) setData({ status: 'error', edits: [] }); });
      return function () { alive = false; };
    }, [props.table, props.id]);
    if (data.status === 'loading') return React.createElement('div', { className: 'fs-muted' }, 'Loading…');
    if (!data.edits.length) return React.createElement('div', { className: 'fs-muted' }, 'No edits yet.');
    return React.createElement('ul', { className: 'fs-content-history' },
      data.edits.map(function (e) {
        return React.createElement('li', { key: e.id, className: 'fs-content-history__item' },
          React.createElement('span', { className: 'fs-content-history__field' }, e.field),
          React.createElement('span', { className: 'fs-content-history__diff' },
            '“' + (e.before_text || '') + '” → “' + (e.after_text || '') + '”'),
          React.createElement('span', { className: 'fs-content-history__meta' },
            (e.actor_role || '') + ' · ' + (e.created_at || '')));
      }));
  }
```

- [ ] **Step 2: Add the History tab** to the topic-detail view using `EvidenceTabs` (same shape as `tasks.js` ~983): tabs `[{key:'details'},{key:'history'}]`; the History tab renders `ContentHistoryPanel({table:'topics', id: topicRowId})`.

- [ ] **Step 3: Add glossary confirm.** In `EditableText.onSaved` (Task 17) surface `res.candidates` (when non-empty) as a small inline "Add to glossary: [McCahon] [✓]" affordance; clicking calls `FS.api.actions.confirmAlias({wrong_term, right_term, kind:'other'})` with the changed token as `right_term` and the prior surface form as `wrong_term`. Only render when `FS.can(caller, FS.P('content','edit'))` and the caller's role is site_manager or above (`FS.roles.hasMinimumRole(caller.role,'site_manager')` if available).

- [ ] **Step 4: Syntax check**

Run: `node --check scripts/pages/timeline.js`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add scripts/pages/timeline.js
git commit -m "feat(timeline): content History panel + glossary confirm"
```

---

### Task 19: Cache busters

**Files:**
- Modify: `C:/Users/camil/Dropbox/fieldsight-ui/app-shell-preview.html`

**Interfaces:** none (deploy hygiene).

**Context:** every changed loaded file (`scripts/roles.js`, `scripts/api/actions.js`, `scripts/pages/timeline.js`) needs its `?v=N` bumped so `file://` and dev servers pick up the change (frontend CLAUDE.md convention).

- [ ] **Step 1: Bump `?v=`** for the three changed scripts in `app-shell-preview.html` (increment each existing `?v=N` to `N+1`; if a script has no `?v=`, add `?v=1`). If a matching CSS was added for `.fs-content-edit`/`.fs-content-history`, bump the stylesheet too.

- [ ] **Step 2: Verify load order + syntax**

Run: `node --check scripts/pages/timeline.js && node --check scripts/api/actions.js && node --check scripts/roles.js`
Expected: no output. Confirm the three `<script>` tags still load in dependency order (`roles.js` before pages; `api/actions.js` after `api/index.js` + `_fetch.js`).

- [ ] **Step 3: Commit**

```bash
git add app-shell-preview.html
git commit -m "chore(ui): bump cache busters for content-correction scripts"
```

---

# Phase E — Deploy wiring

### Task 20: S3 event wiring + IAM + deploy note

**Files:**
- Modify: `src/template.yaml` — `EmbedReportFunction` IAM (put/get on `reindex_requests/*`), `IngestFunction` IAM (get on `reindex_requests/*`), `OrgApiFunction` IAM (put on `reindex_requests/*` on `DataBucketName`/`IngestBucketName` as appropriate)
- Modify: `scripts/wire-s3-events.sh` — add `reindex_requests/*.json` → embed lambda and `reindex_requests/*.vectors.json` → ingest lambda notifications

**Interfaces:** none (infra). This wires the S3-event handoff the Task 12/13 handlers already expect.

**Context (verified):** `EmbedReportFunction` and `IngestFunction` have **NO SAM `Events`** — their S3 triggers are wired MANUALLY via `scripts/wire-s3-events.sh` because the lake bucket (`IngestBucketName`, prod) is external/hand-assembled (BUG-33). Both operate on `IngestBucketName`. The org-api's `S3_BUCKET` is `DataBucketName`; the reindex chain lives on the lake, so Task 10's enqueue already writes to `LAKE_BUCKET` (= `IngestBucketName`). This task just grants the org-api `s3:PutObject` on `arn:aws:s3:::${IngestBucketName}/reindex_requests/*`.

- [ ] **Step 1: IAM grants (template.yaml).**
  - `EmbedReportFunction` policies: add `s3:GetObject` + `s3:PutObject` on `arn:aws:s3:::${IngestBucketName}/reindex_requests/*` (it reads the request and writes the vectors artifact).
  - `IngestFunction` policies: add `s3:GetObject` on `arn:aws:s3:::${IngestBucketName}/reindex_requests/*`.
  - `OrgApiFunction` policies: add `s3:PutObject` on `arn:aws:s3:::${IngestBucketName}/reindex_requests/*` (the enqueue write).

- [ ] **Step 2: S3 notifications (`scripts/wire-s3-events.sh`).** Add two notification configs on the lake bucket (following the existing `fs-embed-report` / ingest entries, which use `put-bucket-notification-configuration` with `MSYS_NO_PATHCONV=1` per BUG-28):
  - prefix `reindex_requests/`, suffix `.json` (but NOT `.vectors.json`) → `${PREFIX}-embed-report`.
  - prefix `reindex_requests/`, suffix `.vectors.json` → `${PREFIX}-ingest`.

  Because S3 suffix filters cannot express "ends with `.json` but not `.vectors.json`", the embed handler already guards this in code (Task 12 skips `.vectors.json`); configure both triggers on suffix `.json` and let the handlers' regexes (`REINDEX_REQUEST_RE` / `REINDEX_VECTORS_RE`) route correctly. Ensure `.vectors.json` events reaching the embed lambda are skipped (Task 12) and non-`.vectors.json` reaching ingest are ignored (add a fall-through `continue` for non-matching keys in `lambda_ingest.lambda_handler`).

- [ ] **Step 3: Migration ordering + shared-cluster note.** Migrations `0019`/`0020` are additive and run automatically on deploy via `lambda_migrate` (idempotent through `schema_migrations`). Because test and prod SHARE one Aurora cluster, the first stack to deploy (test, via `develop`) applies them for BOTH; the prod deploy (`main`) is then a no-op for these two migrations. No destructive change is introduced. Confirm `deploy.yml` (`paths-ignore: docs/**`) and `deploy-prod.yml` (`paths-ignore: tests/**, docs/**`) still trigger on the `src/**` changes in this plan.

- [ ] **Step 4: Verify the SAM template parses**

Run: `cd C:/Users/camil/Dropbox/fieldsight-pipeline && uv run python -c "import yaml,sys; yaml.safe_load(open('src/template.yaml'))" || sam validate --template src/template.yaml`
Expected: no parse error (note: `sam validate` may warn on intrinsic tags; a clean YAML load is sufficient here).

- [ ] **Step 5: Commit**

```bash
git add src/template.yaml scripts/wire-s3-events.sh
git commit -m "chore(infra): wire reindex_requests S3 events + IAM for content re-index"
```

---

## Self-Review — spec coverage map

| Spec item | Task(s) |
|---|---|
| §5.1 D fix — id surfacing + report-sourced render | 8 |
| §5.2 edit endpoint + audit + diff candidates | 5, 6, 10 + 3 (`diff_candidates`) |
| §3 editable-field allow-list (excludes enums) | 5 |
| §5.3 `delete_chunks_for_topic` + re-index hook (async, non-VPC embed) | 7, 9, 10 (enqueue), 12, 13 |
| §5.4 `name_aliases` store + `normalize()` | 2, 3, 4 |
| §4/§6 RAG synthesis normalize safety net | 14 |
| D2 glossary confirm (candidates → alias, site_manager+) | 10 (candidates), 11, 18 |
| D7 two-tier authority | 10 (per-item), 11 (alias tier) |
| §5.5 frontend inline editors + History + glossary confirm | 15, 16, 17, 18 |
| Cache busters / deploy | 19, 20 |
| D4 transcript never written (read + normalized copy only) | 9, 12 |
| Migrations on shared Aurora, additive | 1, 2, 20 |

**Out of scope (spec §9, intentionally not built):** B's AWS Transcribe Custom Vocabulary; C's content-filter/privacy; retroactive "apply alias to all existing content."

---

# Phase F — SAFETY/QUALITY single-source (D8 retirement)

> **EXECUTION ORDER:** run Phase F **BEFORE Phase D** (the frontend content
> editors). Rationale (spec §8): SAFETY/QUALITY currently read the legacy
> `safety_observations` copy; if the frontend lets users edit `findings` first,
> SAFETY/QUALITY would show stale text. Retiring the dual-write first makes the
> first real content edit propagate. Backend-only; ships with Phase C to prod.
> Anchors below are confirmed against the live file at execution (existing code
> may drift), same as every other phase.

### Task 21: rollup safety counts read `findings`-by-`domain`, not `safety_observations`
**Files:** Modify `src/repositories/rollup.py` (the `safety_rows` subquery, ~line 60); Test: `tests/unit/test_lambda_org_api.py` (existing portfolio_counts tests).
**Interfaces:** `portfolio_counts` return keys unchanged (`open_safety`, `open_high_safety`); only their SOURCE changes. `findings` has `domain`, `status DEFAULT 'open'`, `severity` (0010).
**Steps (TDD):** replace `FROM safety_observations WHERE site_id=ANY(%s)` with
`FROM findings WHERE site_id=ANY(%s) AND domain='safety'`, and `FILTER (WHERE status='open')` → same, `open_high_safety` FILTER → `status='open' AND severity='major'` (findings severity vocab is none/minor/major, NOT risk_level high). Update the merges-N-queries test's safety fixture + assertions. `uv run pytest -k portfolio`. Commit.

### Task 22: `topics.list_topics_for_date` attaches safety/quality `findings` to the SAFETY/QUALITY read slot
**Files:** Modify `src/repositories/topics.py` (both list shapes attach `safety_observations` at ~218/313 and already attach `findings` via `findings.list_for_topics`); Test: the topics read tests.
**Interfaces:** the SAFETY/QUALITY UI (Timeline/live-items) child slot must be sourced from `findings WHERE domain IN ('safety','quality')`. Determine which child key the UI reads for SAFETY/QUALITY; point it at the domain-filtered findings (findings are already fetched — reuse, don't add a query). Keep `safety_observations` attached only if a legacy consumer still needs it (verify none); otherwise stop attaching it.
**Steps (TDD):** write a test asserting a safety `finding` appears in the SAFETY read slot and a quality `finding` in QUALITY, with NO dependency on `safety_observations` rows. Implement. `uv run pytest`. Commit.

### Task 23: stop the `_derive_safety_flags` dual-write into `safety_observations`
**Files:** Modify the item-writer bridge (`src/lambda_item_writer.py` / wherever `_derive_safety_flags` writes `safety_observations`); Test: item-writer tests.
**Interfaces:** findings remain the source of truth; the `safety_observations` INSERT is removed. Leave the TABLE in place (unread, for rollback); a later migration drops it.
**Steps (TDD):** update the item-writer test to assert findings are written and `safety_observations` is no longer inserted (the write is gone). Implement. `uv run pytest`. Commit.

### Task 24: content allow-list — safety/quality edits target `findings`, drop `safety_observations`
**Files:** Modify `src/repositories/content.py` (`EDITABLE` map + `_SELECTS`); Test: `tests/unit/test_repo_content.py`.
**Interfaces:** remove `safety_observations` from `EDITABLE` (single editable source). `findings` already covers `observation`/`entity_name`/etc. for safety/quality content. So a SAFETY/QUALITY correction edits the `findings` row and — via Tasks 21–22 — the SAFETY/QUALITY views update automatically.
**Steps (TDD):** update the allow-list test (safety_observations no longer editable; findings is). Implement. `uv run pytest`. Commit.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-20-editable-content-correction.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task with review between tasks (REQUIRED SUB-SKILL: superpowers:subagent-driven-development). Phases A→E are ordered by dependency; within Phase A, Tasks 1–4 are independent and can run in parallel.
2. **Inline Execution** — execute in this session with checkpoints (REQUIRED SUB-SKILL: superpowers:executing-plans).

Which approach?
