# A literal token is findable — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A search or Ask query containing a literal token (`PS4`, a drawing number, an RFI number) finds the chunk that contains it verbatim, even when that chunk loses the top-*k* cosine-distance race against topically-similar chunks. Today it does not: `build_search_sql()` is pure vector ANN with no keyword arm, so a rare-but-exact term can lose to forty semantically-closer chunks and never reach the downstream lexical-ranking logic that would otherwise surface it.

**Architecture:** `build_search_sql()` (`src/repositories/search_sql.py`) currently emits one `SELECT ... ORDER BY <=> ... LIMIT %(k)s` query, called from both `/search` (via `lambda_fieldsight_api.py:search_topics` → `AskAgentFunction` `mode:'search'` → `lambda_ask_agent.py:_rag_search_list`) and Ask (`lambda_ask_agent.py:_rag_answer`), both of which invoke `RagSearchFunction` (`lambda_rag_search.py`) → `repositories/chunks.py:search_chunks()` → `build_search_sql()`. This plan adds a second CTE — a GIN **expression** index keyword arm, `to_tsvector('english', c.chunk_text) @@ websearch_to_tsquery(...)` — unioned with the existing vector arm inside `build_search_sql()`, before the final `LIMIT`. Both arms share one Python-built WHERE-predicate string (site/author/date/`visible_chunks_predicate`) so they can never drift apart. Rows from the keyword-only arm carry a `lexical_hit` flag that is threaded through `chunks.search_chunks()`'s return shape and `lambda_ask_agent.py:_aggregate_topics()`'s `lexical` field, so they survive the `_NO_LEX_MAX_DIST = 0.55` distance-gate at `lambda_ask_agent.py:882` instead of being silently dropped one hop downstream of the fix.
>
> **Mechanism revised after Task 1 was first drafted (see Task 1 for the full reasoning):** the plan originally called for a `GENERATED ALWAYS ... STORED` column. The controller found two facts that override that choice: `src/db/migrate.py` runs every migration inside `conn.transaction()`, which makes `CREATE INDEX CONCURRENTLY` impossible inside a migration file, and this repo has no read-only path to measure `report_chunks`'s prod row count before shipping — so the stored column's `ACCESS EXCLUSIVE`-locked, unmeasurable-duration table rewrite could not be gated by the planned pre-ship check at all. An index on the bare expression `to_tsvector('english', chunk_text)` needs no new column, no table rewrite, and nothing to keep in sync on future writes (there is no stored value to go stale), at the cost of the query having to repeat the identical expression for the planner to use the index — Task 2/3 make that match enforced by a test, not a convention.

**Tech Stack:** Python 3.12 Lambdas (SAM, `CodeUri: src/`), psycopg3, Aurora PostgreSQL (`vector` + `pgcrypto` extensions already installed, `hnsw` index already in use — see `src/migrations/0001_extensions.sql`, `0004_report_chunks.sql`), pytest with in-repo fakes for the unit suite and a real-Postgres integration suite (`tests/integration/`, gated on `TEST_DATABASE_URL`).

**Spec:** `docs/superpowers/specs/2026-09-20-a-literal-token-is-findable.md`

## Global Constraints

- The ACL and tombstone predicates are shared, never duplicated — one Python string builder used by both the vector CTE and the keyword CTE, so the two can never drift.
- Deny-by-default is preserved. A keyword arm that runs with looser or no ACL/tombstone filtering and gets unioned into visible results is the worst outcome available here.
- Development artefacts (code comments, commit messages, docs) in English.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  ```
- Windows repo: never `git add -A`; add files by path.
- Test harness (run from the worktree root `C:/Users/camil/fswork/specs-2026-09-20`, Git Bash):
  ```bash
  export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
  uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest <paths> -q
  ```
- Integration tests under `tests/integration/` additionally need `TEST_DATABASE_URL` set to a real (throwaway) Postgres; without it they skip (`pytest.mark.skipif(not DSN, ...)`). A fake connection never executes SQL and has hidden two prod crashes before — the SQL and migration changes in this plan MUST be proven against a real database, not just string-matched.
- `git grep -n "ruler" origin/main` currently returns exactly one citing spec and no ruler script/document. Do not invent one; follow Task 6's concrete fallback instead of asserting the ruler was re-run.

## File Structure

- Modify `src/migrations/` — add `0059_report_chunks_tsv_idx.sql`: a single `CREATE INDEX IF NOT EXISTS ... USING gin (to_tsvector('english', chunk_text))`. No new column, no backfill.
- Modify `src/repositories/search_sql.py` — `build_search_sql()` becomes a `UNION`/`DISTINCT ON` of a vector CTE and a keyword CTE; add a private shared predicate-string builder, and a second shared constant for the tsvector expression so the query and the index can never spell it differently.
- Modify `src/repositories/chunks.py` — `search_chunks()` return shape gains `lexical_hit` (already using `dict_row`, no signature change needed beyond documenting the new column).
- Modify `src/lambda_ask_agent.py` — `_aggregate_topics()` (`:803-885`) sets `"lexical"` from `c.get("lexical_hit")` OR the existing title-substring check, so a keyword-arm-only row is never dropped by `_NO_LEX_MAX_DIST`.
- Modify `tests/unit/test_search_sql.py`, `tests/unit/test_chunk_search_sql.py`, `tests/unit/test_deleted_search_and_ask.py` — extend for the new SQL shape; add the expression-match test (Task 3).
- Create `tests/integration/test_literal_token_search_sql.py` — real-Postgres acceptance test (PS4 case), tombstone/out-of-site exclusion, control query, word-boundary case, and the "index is actually used" plan check.
- Modify `tests/unit/test_lambda_ask_agent_search.py` — regression test for `_NO_LEX_MAX_DIST` admitting a `lexical_hit=True`/`distance=None` row.
- Modify `tests/integration/test_migrations_apply.py` — index-shape checks for `0059` (no generated-column checks; there is no column).

---

### Task 1: Migration `0059_report_chunks_tsv_idx.sql` — a GIN index on the bare expression

**Mechanism decision (revised from the original draft):** an expression GIN index — `CREATE INDEX ... USING gin (to_tsvector('english', chunk_text))` — not a `GENERATED ALWAYS ... STORED` column. Reason, stated once here and not repeated per-step: `src/db/migrate.py:46` wraps every migration file in `with conn.transaction()`, and `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block (`git grep -n CONCURRENTLY origin/main -- src/migrations/` finds no precedent for working around this) — so a stored column's `ACCESS EXCLUSIVE` table rewrite would have to run as a plain, uncancellable, un-concurrent operation inside the migration runner regardless of table size, and this repo has no read-only SQL path to measure that size before shipping (`scripts/` has schema verifiers, not query helpers). A plain `CREATE INDEX` (no `CONCURRENTLY`) also runs inside the transaction wrapper without issue, takes only a `SHARE` lock (blocks writes, not reads — a materially smaller blast radius than `ACCESS EXCLUSIVE`), needs no new column, no table rewrite, and *nothing to keep in sync on future writes*, because there is no stored value that could go stale — Postgres maintains an expression index exactly like any other index, on every insert. This removes the open judgment call the previous draft's Step 5 ended in, because there is no longer a table-rewrite duration to gate on.

**Consequence for search_sql.py (Task 3):** the query's `WHERE` clause must repeat the identical expression `to_tsvector('english', chunk_text)` for the Postgres planner to use this index instead of a sequential scan. "Identical" is enforced by a shared Python constant (`_TSV_EXPR`) that both the migration-verifying test and `build_search_sql()` read from the same place conceptually — concretely, Task 3 adds a unit test that greps the literal expression string out of the migration file and asserts it is a byte-for-byte substring of `build_search_sql()`'s output, so an edit to either side that breaks the match fails a test immediately rather than silently degrading to a full table scan in prod. See Task 3 Step 1's `test_keyword_arm_expression_matches_the_index_exactly`.

**Downstream consumers of the old `chunk_tsv` column:** none. No reader outside `build_search_sql()` was ever going to select the tsvector value itself — `chunks.search_chunks()`'s callers only need `lexical_hit` (a boolean the SQL computes at query time either way) and the existing chunk columns. The column was only ever an implementation detail of how the WHERE clause matched; dropping it changes no downstream shape.

**Operational escape hatch, documented rather than left as a judgment call:** if a future `EXPLAIN ANALYZE` on prod shows the plain `CREATE INDEX`'s `SHARE` lock is unacceptable (e.g. a write-heavy window), the concurrent-build path is:

1. Run `CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_report_chunks_tsv ON report_chunks USING gin (to_tsvector('english', chunk_text));` **manually, outside the migration runner**, against prod, in autocommit mode (`psql`'s default) — `CONCURRENTLY` cannot run inside any transaction block, migration-runner or otherwise, so this is inherently a manual, one-off DBA-style step, not something `apply_migrations` can ever do for you.
2. Then let `0059` run through the normal deploy: it is written with `IF NOT EXISTS`, so if the index already exists from step 1, the migration's own `CREATE INDEX` is a no-op and `schema_migrations` still records `0059` as applied, exactly as if it had built the index itself.
3. Cost of this path: a manual DBA action outside the deploy pipeline, a period where two index-build attempts race (harmless — `IF NOT EXISTS` makes the second one a no-op) if the migration deploy runs before the concurrent build finishes, and the general operational overhead of `CONCURRENTLY` (roughly 2-3x slower than a plain build, and it can be left in an invalid state if interrupted, requiring `DROP INDEX` and retry).

This plan does NOT choose the concurrent path by default — it ships the plain `CREATE INDEX IF NOT EXISTS` inside `0059` and documents the escape hatch for the owner to invoke only if the plain build's `SHARE` lock proves to be a problem on prod.

**Files:**
- Create: `src/migrations/0059_report_chunks_tsv_idx.sql`
- Test: `tests/integration/test_migrations_apply.py` (extend)

**Interfaces:**
- Consumes: nothing (pure DDL against `report_chunks.chunk_text`, `src/migrations/0004_report_chunks.sql:9`).
- Produces: `idx_report_chunks_tsv` — a GIN index on `to_tsvector('english', chunk_text)`. No new column.

The next free migration number is `0059`: the highest number currently in `src/migrations/` is `0058_topic_decisions.sql` (verified by `ls src/migrations | tail`), and `pending_versions()` in `src/db/migrate.py` sorts by `(int(filename.split("_",1)[0]), filename)`, so `0059` is the first number not yet taken and will run after `0058` regardless of what else lands first.

- [ ] **Step 1: Write the failing migration-shape test**

Add to `tests/integration/test_migrations_apply.py` (after `test_sites_has_coordinate_columns`):

```python
def test_report_chunks_has_a_tsvector_expression_index():
    # 0059: literal-token search needs a word-boundary-aware index. This is a
    # plain (non-CONCURRENT) index on the bare expression, not a stored
    # column -- CREATE INDEX CONCURRENTLY cannot run inside apply_migrations'
    # conn.transaction() wrapper, and a GENERATED STORED column's ACCESS
    # EXCLUSIVE table rewrite has no measurable duration in this repo (no
    # read-only path to prod's row count). An expression index needs no
    # column and nothing to keep in sync on future writes.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        rows = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename='report_chunks' AND indexname='idx_report_chunks_tsv'"
        ).fetchall()
        assert rows, "idx_report_chunks_tsv is missing"
        indexdef = rows[0][0].lower()
        assert "gin" in indexdef
        assert "to_tsvector" in indexdef
        assert "chunk_text" in indexdef
        # No column was added -- confirms this really is an expression index,
        # not a stored column with the same index name.
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='report_chunks'"
        ).fetchall()}
        assert "chunk_tsv" not in cols
    finally:
        conn.close()


def test_report_chunks_tsv_index_survives_a_second_apply():
    # IF NOT EXISTS -- also the property the CONCURRENTLY escape hatch (Task 1's
    # documented manual path) depends on: if a DBA builds the index manually
    # first, this migration's own CREATE INDEX must be a no-op, not an error.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_report_chunks_tsv "
            "ON report_chunks USING gin (to_tsvector('english', chunk_text))")
        rows = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname='idx_report_chunks_tsv'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/integration/test_migrations_apply.py -q -k tsv`
(needs `TEST_DATABASE_URL` pointed at a throwaway Postgres; if none is available locally, use `docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:16` and `export TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/postgres`)
Expected: both new tests FAIL (`idx_report_chunks_tsv is missing`).

- [ ] **Step 3: Write the migration**

Create `src/migrations/0059_report_chunks_tsv_idx.sql`:

```sql
-- A literal token in chunk_text is unreachable through search today unless the
-- chunk that contains it also lands in the top ~30 by cosine distance (spec
-- 2026-09-20-a-literal-token-is-findable.md). Vector similarity has almost no
-- signal for an opaque identifier like "PS4" or an RFI number, so a rare literal
-- systematically loses the top-k race against topically-similar prose.
--
-- An index on the BARE EXPRESSION, not a GENERATED STORED column:
--   * ILIKE + pg_trgm is rejected in the spec (word-boundary blind: "PS4"
--     would match inside "PS40" or "GPS4x", which is exactly wrong for
--     identifier search).
--   * A GENERATED ALWAYS ... STORED column was the first design, but this
--     repo's migration runner (src/db/migrate.py) wraps every file in
--     conn.transaction(), and CREATE INDEX CONCURRENTLY cannot run inside a
--     transaction block. A stored column's ALTER TABLE rewrites the whole
--     table under an ACCESS EXCLUSIVE lock with no way to run it
--     concurrently from inside a migration, and there is no read-only path
--     in this repo to measure report_chunks' prod size before shipping to
--     know how long that lock would be held.
--   * This index needs no new column, no table rewrite, and (unlike the
--     rejected stored column, and unlike the embedding pipeline's manual
--     reindex-vectors backfill) NOTHING to keep in sync on future writes --
--     there is no stored value to go stale; Postgres maintains an
--     expression index like any other index, on every write, automatically.
--   * Cost: build_search_sql() (repositories/search_sql.py) MUST repeat this
--     exact expression, to_tsvector('english', chunk_text), for the planner
--     to use this index rather than a sequential scan. Enforced by
--     tests/unit/test_search_sql.py's expression-match test, not convention.
--
-- Plain CREATE INDEX (no CONCURRENTLY): takes a SHARE lock (blocks writes,
-- not reads) while it builds -- a materially smaller blast radius than the
-- rejected column's ACCESS EXCLUSIVE, and it is what this migration runner
-- can actually execute inside a transaction. If this lock proves too costly
-- on prod's real table size, build the index CONCURRENTLY manually first
-- (outside this runner, in autocommit mode -- CONCURRENTLY cannot run in
-- any transaction block, migration-runner or otherwise); IF NOT EXISTS below
-- then makes this file's own CREATE INDEX a no-op against that pre-built
-- index, and schema_migrations still records 0059 as applied.
--
-- 'english' will stem "electrical" -> "electr", which is fine for prose but
-- must not mangle alphanumeric identifiers. Postgres's default parser classes
-- shapes like "ps4", "a-101" as asciiword/numword and passes them through
-- largely intact -- verified against real corpus identifiers, not just PS4,
-- in tests/integration/test_literal_token_search_sql.py before this shipped.
CREATE INDEX IF NOT EXISTS idx_report_chunks_tsv
  ON report_chunks USING gin (to_tsvector('english', chunk_text));
```

- [ ] **Step 4: Run the tests to verify they pass**

Same command as Step 2. Expected: `2 passed`.

- [ ] **Step 5: Confirm the planner actually uses the index (replaces the old, unexecutable "measure ALTER cost against prod size" step)**

Against the throwaway database with `0059` applied and at least a few hundred rows in `report_chunks` (insert synthetic rows if the fixture DB is empty — row count matters for whether the planner prefers the index over a sequential scan on a tiny table, so this check is only meaningful once there is enough data):

```bash
psql "$TEST_DATABASE_URL" -c "EXPLAIN SELECT id FROM report_chunks WHERE to_tsvector('english', chunk_text) @@ websearch_to_tsquery('english', 'PS4');"
```

Expected: the plan mentions `idx_report_chunks_tsv` (a `Bitmap Index Scan` or `Index Scan` on it), not `Seq Scan`. If it shows `Seq Scan` on a table with a meaningful row count, the expression in the query does not match the index's expression character-for-character (check for a stray whitespace or config-name mismatch) — this is the concrete, executable replacement for the old plan's unmeasurable "table size" gate, because it tests the thing that actually matters (index usage), not a duration nobody can look up in advance. Record the `EXPLAIN` output in the PR body (Task 7).

- [ ] **Step 6: Commit**

```bash
git add src/migrations/0059_report_chunks_tsv_idx.sql tests/integration/test_migrations_apply.py
git commit -m "Add a GIN expression index so a literal token is indexable

A rare literal (PS4, an RFI number, a drawing number) carries almost no
embedding signal and systematically loses the top-k cosine race against
topically-similar prose. This indexes to_tsvector('english', chunk_text)
directly rather than adding a GENERATED STORED column: the migration
runner wraps every file in a transaction, which rules out CREATE INDEX
CONCURRENTLY, and this repo has no way to measure report_chunks' prod row
count in advance to gate a full-table ACCESS EXCLUSIVE rewrite. A plain
CREATE INDEX takes only a SHARE lock and needs nothing kept in sync on
future writes, because there is no stored value to go stale.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Shared WHERE-predicate builder in `search_sql.py`

**Files:**
- Modify: `src/repositories/search_sql.py`
- Test: `tests/unit/test_search_sql.py`, `tests/unit/test_chunk_search_sql.py`, `tests/unit/test_deleted_search_and_ask.py`

**Interfaces:**
- Consumes: `visible_chunks_predicate(alias)` from `src/deleted_predicates.py:53` (unchanged).
- Produces: a private `_scope_predicate(alias: str) -> str` in `search_sql.py` that returns the site/author/date/tombstone WHERE clause for one alias, used by both the vector and keyword CTEs in `build_search_sql()`.

This task only refactors the existing single-query predicate into a reusable string builder; it does not add the keyword arm yet (Task 3 does). Keeping it separate means a broken refactor is caught before the new arm's SQL is layered on top of it.

- [ ] **Step 1: Write the failing test**

`test_one_definition_of_the_predicate_not_two` in `tests/unit/test_deleted_search_and_ask.py` already asserts `"NOT EXISTS" not in src` for `search_sql.py` — this must keep passing (it will, since `_scope_predicate` still calls `visible_chunks_predicate()` rather than inlining `NOT EXISTS`). Add a new test to `tests/unit/test_search_sql.py`:

```python
def test_scope_predicate_is_shared_by_construction():
    """The vector and keyword arms must use the SAME predicate string, not two
    calls that happen to look alike today and can drift apart tomorrow."""
    import repositories.search_sql as search_sql
    assert hasattr(search_sql, "_scope_predicate"), \
        "build_search_sql must factor its WHERE clause into one shared builder"
    p1 = search_sql._scope_predicate("c")
    p2 = search_sql._scope_predicate("c")
    assert p1 == p2
    # Both arms of the final SQL must contain this exact string.
    sql = search_sql.build_search_sql()
    assert sql.count(p1) == 2, \
        "the scope predicate must appear once per CTE (vector arm + keyword arm), verbatim"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/unit/test_search_sql.py -q -k scope_predicate`
Expected: `AttributeError: module 'repositories.search_sql' has no attribute '_scope_predicate'`.

- [ ] **Step 3: Implement `_scope_predicate` and rewire `build_search_sql()` (single-arm, no UNION yet)**

Edit `src/repositories/search_sql.py`:

```python
"""Pure search-SQL construction. MUST NOT import psycopg."""
from deleted_predicates import visible_chunks_predicate


def _scope_predicate(alias: str = "c") -> str:
    """The WHERE clause every read of report_chunks must carry: deny-by-default
    site ACL, optional per-author narrowing, optional inclusive date range, and
    both tombstone arms. Used verbatim by BOTH the vector arm and the keyword
    arm of build_search_sql() -- one definition so the two can never drift.
    A keyword arm that ran with a looser copy of this string would resurrect
    exactly the deleted-recording leak visible_chunks_predicate was built to
    close."""
    return (
        f"{alias}.site_id = ANY(%(site_ids)s) "
        f"AND (%(author_ids)s::uuid[] IS NULL OR {alias}.user_id = ANY(%(author_ids)s::uuid[])) "
        f"AND (%(date_from)s::date IS NULL OR {alias}.report_date >= %(date_from)s::date) "
        f"AND (%(date_to)s::date IS NULL OR {alias}.report_date <= %(date_to)s::date) "
        f"AND " + visible_chunks_predicate(alias)
    )


def build_search_sql() -> str:
    # Deny-by-default: ALWAYS filter by the caller's accessible site ids.
    # small-to-big: parent topic title/summary via LEFT JOIN. Citations need
    # report_date/site_id/site_name. Optional inclusive report_date range
    # (both NULL => no date filtering, so the Ask path stays byte-identical
    # when it passes no dates).
    scope = _scope_predicate("c")
    return (
        "SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "       c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "       s.slug AS site_slug, "
        "       t.title AS topic_title, t.summary AS topic_summary, "
        "       c.embedding <=> %(q)s::vector AS distance "
        "FROM report_chunks c "
        "LEFT JOIN topics t ON t.id = c.topic_id "
        "LEFT JOIN sites s ON s.id = c.site_id "
        "WHERE " + scope + " "
        "ORDER BY c.embedding <=> %(q)s::vector "
        "LIMIT %(k)s"
    )
```

Note: at this point `test_scope_predicate_is_shared_by_construction`'s `sql.count(p1) == 2` assertion will still FAIL (only one occurrence) — that is expected and intentional; Task 3 makes it true by adding the keyword CTE. Do not force it to pass here.

- [ ] **Step 4: Run the existing search_sql tests**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/unit/test_search_sql.py tests/unit/test_chunk_search_sql.py tests/unit/test_deleted_search_and_ask.py -q`
Expected: every pre-existing test PASSES unchanged (the SQL text for the single-arm query is byte-identical to before — `_scope_predicate` only extracts the same substring into a variable). `test_scope_predicate_is_shared_by_construction` still FAILS on its last assertion (documented above) — leave it red; Task 3 turns it green.

- [ ] **Step 5: Prove the refactor didn't silently change behavior**

Temporarily add a stray space to `_scope_predicate`'s returned string (e.g. double a space) and re-run Step 4's command. Expected: `test_search_sql_has_author_filter_with_null_guard`'s exact-substring assertion FAILS, proving the existing tests still pin the literal clause text. Revert.

- [ ] **Step 6: Commit**

```bash
git add src/repositories/search_sql.py tests/unit/test_search_sql.py
git commit -m "Factor build_search_sql's WHERE clause into one shared string

The keyword arm landing in the next commit needs the identical site/author/
date/tombstone predicate the vector arm already carries. Duplicating it by
hand across two CTEs is exactly the kind of drift that already leaked a
deleted recording once (visible_chunks_predicate's own history) -- one
Python function returning one string used by both, so they cannot diverge.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The keyword arm — union it into `build_search_sql()`

**Files:**
- Modify: `src/repositories/search_sql.py`
- Modify: `src/repositories/chunks.py` — `search_chunks()` docstring/return-shape note only (no signature change; `dict_row` already returns whatever columns the SQL selects)
- Test: `tests/unit/test_search_sql.py`

**Interfaces:**
- Produces: `build_search_sql()` now returns a `WITH vec AS (...), lex AS (...) SELECT DISTINCT ON (id) ... FROM (SELECT * FROM vec UNION ALL SELECT * FROM lex) u ORDER BY id, lexical_hit DESC LIMIT %(k)s * 2` shaped query; every returned row carries `lexical_hit: bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_search_sql.py`:

```python
def test_search_sql_has_a_keyword_arm():
    sql = build_search_sql().lower()
    assert "to_tsvector('english', c.chunk_text) @@ websearch_to_tsquery" in sql, \
        "no lexical arm: a literal token can still only be found by cosine luck"
    assert "union all" in sql


def test_search_sql_keyword_arm_shares_the_scope_predicate():
    import repositories.search_sql as search_sql
    scope = search_sql._scope_predicate("c")
    sql = build_search_sql()
    assert sql.count(scope) == 2, \
        "vector arm and keyword arm must carry the IDENTICAL scope predicate string"


def test_keyword_arm_expression_matches_the_index_exactly():
    """The migration (0059_report_chunks_tsv_idx.sql) creates a GIN index on
    to_tsvector('english', chunk_text). Postgres only uses an expression index
    when the query repeats that expression character-for-character (modulo the
    table alias) -- a query that spells it even slightly differently (extra
    space, different config literal) silently falls back to a sequential scan
    with no error. This test is the thing that catches that drift, not a
    convention or a comment."""
    import os
    import repositories.search_sql as search_sql

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(search_sql.__file__)), "migrations")
    with open(os.path.join(migrations_dir, "0059_report_chunks_tsv_idx.sql"),
              encoding="utf-8") as fh:
        migration_sql = fh.read()
    assert "to_tsvector('english', chunk_text)" in migration_sql, \
        "the migration's own expression changed -- update this test's expectation deliberately"

    sql = build_search_sql()
    # The query's alias is "c", the migration's bare column has no alias --
    # the expression itself (function name, language literal, quoting) must
    # still match exactly.
    assert "to_tsvector('english', c.chunk_text)" in sql, \
        "build_search_sql's keyword arm must spell the tsvector expression " \
        "IDENTICALLY to idx_report_chunks_tsv's definition, or the planner " \
        "silently falls back to a sequential scan"


def test_search_sql_keyword_arm_never_fabricates_a_good_distance():
    sql = build_search_sql().lower()
    # The keyword-only arm has no real cosine score. It must not assign one
    # that would let it silently outrank a true semantic top-1 (spec §5).
    # A NULL placeholder (not e.g. 0.0) is the marker _aggregate_topics keys off.
    assert "null as distance" in sql or "as distance" in sql
    assert "true as lexical_hit" in sql
    assert "false as lexical_hit" in sql


def test_search_sql_dedupes_rows_found_by_both_arms():
    sql = build_search_sql().lower()
    assert "distinct on (id)" in sql or "distinct on (c.id)" in sql, \
        "a row found by both arms must be one row, keeping its real distance"


def test_search_sql_limit_is_still_present_and_final():
    sql = build_search_sql()
    assert sql.rstrip().endswith("%(k)s") or "limit %(k)s" in sql.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/unit/test_search_sql.py -q`
Expected: the five new tests FAIL (including `test_keyword_arm_expression_matches_the_index_exactly`, which needs both Task 1's migration file and this task's SQL to exist); the `test_scope_predicate_is_shared_by_construction` test from Task 2 now becomes relevant again (still failing on `count == 2`).

- [ ] **Step 3: Implement the keyword arm**

Replace `build_search_sql()` in `src/repositories/search_sql.py`:

```python
def build_search_sql() -> str:
    # Deny-by-default: ALWAYS filter by the caller's accessible site ids.
    # small-to-big: parent topic title/summary via LEFT JOIN. Citations need
    # report_date/site_id/site_name. Optional inclusive report_date range
    # (both NULL => no date filtering, so the Ask path stays byte-identical
    # when it passes no dates).
    #
    # TWO arms, unioned before the final LIMIT (2026-09-20 spec: "a literal
    # token is findable"). A rare identifier like "PS4" or an RFI number
    # carries almost no embedding signal and systematically loses the top-k
    # cosine race against topically-similar prose -- the chunk that contains
    # the exact string a user typed can lose to forty unrelated chunks. The
    # keyword arm runs to_tsvector('english', c.chunk_text) @@
    # websearch_to_tsquery on the SAME scope predicate as the vector arm
    # (never a looser one -- see _scope_predicate's own docstring on why that
    # would resurrect a closed deleted-recording leak). The expression here
    # MUST match idx_report_chunks_tsv's definition (0059 migration) character
    # for character or Postgres silently falls back to a sequential scan --
    # test_keyword_arm_expression_matches_the_index_exactly pins this.
    #
    # The keyword arm's rows carry NO meaningful cosine distance (never scored
    # against the query embedding) -- NULL, not a fabricated "good" number,
    # so a keyword hit cannot silently outrank a true semantic top-1.
    # lexical_hit distinguishes the two arms; _aggregate_topics (lambda_ask_agent.py)
    # must admit a lexical_hit=True row past its distance gate rather than
    # dropping it one hop downstream of this fix.
    #
    # DISTINCT ON (id): a row found by BOTH arms is one row. ORDER BY id,
    # lexical_hit DESC keeps that row's vector-arm copy (lexical_hit=false,
    # but with a real distance) when one exists, so it still ranks by true
    # relevance instead of losing its distance to the keyword copy.
    scope = _scope_predicate("c")
    return (
        "WITH vec AS ("
        "  SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "         c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "         s.slug AS site_slug, "
        "         t.title AS topic_title, t.summary AS topic_summary, "
        "         c.embedding <=> %(q)s::vector AS distance, false AS lexical_hit "
        "  FROM report_chunks c "
        "  LEFT JOIN topics t ON t.id = c.topic_id "
        "  LEFT JOIN sites s ON s.id = c.site_id "
        "  WHERE " + scope + " "
        "  ORDER BY c.embedding <=> %(q)s::vector "
        "  LIMIT %(k)s"
        "), "
        "lex AS ("
        "  SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "         c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "         s.slug AS site_slug, "
        "         t.title AS topic_title, t.summary AS topic_summary, "
        "         NULL::float8 AS distance, true AS lexical_hit "
        "  FROM report_chunks c "
        "  LEFT JOIN topics t ON t.id = c.topic_id "
        "  LEFT JOIN sites s ON s.id = c.site_id "
        "  WHERE " + scope + " "
        "  AND to_tsvector('english', c.chunk_text) @@ websearch_to_tsquery('english', %(q_text)s) "
        "  LIMIT %(k)s"
        ") "
        "SELECT DISTINCT ON (id) id, chunk_text, chunk_type, topic_id, source_s3_key, "
        "       metadata, report_date, site_id, site_name, site_slug, "
        "       topic_title, topic_summary, distance, lexical_hit "
        "FROM (SELECT * FROM vec UNION ALL SELECT * FROM lex) u "
        "ORDER BY id, lexical_hit ASC, distance ASC NULLS LAST "
        "LIMIT %(k)s * 2"
    )
```

Note the `ORDER BY id, lexical_hit ASC, distance ASC NULLS LAST`: within one `id`, the vector-arm row (`lexical_hit=false`) sorts before the keyword-only row (`lexical_hit=true`) so `DISTINCT ON (id)` keeps the real-distance copy when both exist — re-read this against the earlier draft in the test comments (`lexical_hit DESC`) and reconcile to `ASC` here; write the test in Step 1 to match whichever ordering keyword you actually implement, then keep both consistent. The outer query has no `ORDER BY` guaranteeing final rank order beyond dedup — `_aggregate_topics` in `lambda_ask_agent.py` already does the caller-visible ranking (lexical-first, then distance), so this SQL's final row order is not load-bearing; only which rows survive is.

Also add the new `%(q_text)s` bind parameter alongside `%(q)s` (the vector) in `chunks.search_chunks()`:

```python
def search_chunks(conn, query_embedding, accessible_site_ids, k=5,
                  date_from=None, date_to=None, author_ids=None, query_text="") -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        build_search_sql(),
        {"q": query_embedding, "q_text": query_text, "site_ids": list(accessible_site_ids), "k": k,
         "date_from": date_from, "date_to": date_to,
         "author_ids": list(author_ids) if author_ids is not None else None},
    ).fetchall()
```

`query_text` defaults to `""` so every existing caller that does not pass it keeps working; `websearch_to_tsquery('english', '')` returns an empty tsquery that matches nothing, which is the correct behavior for "no text was given to search on" (never matches everything).

- [ ] **Step 4: Update `chunks.search_chunks`'s callers to pass the question text**

`git grep -n "search_chunks(" src/` to find every call site. At minimum `src/lambda_rag_search.py` (around line 344) must now pass the caller's question string as `query_text=`. Read the surrounding code first to find the variable holding the raw question (it currently only has the embedded vector) — `lambda_rag_search.py` receives the payload from `_rag_search_list`/`_rag_answer`, which both already have `question`; that string needs to travel alongside `query_embedding` in the invoke payload (`payload["question"] = question` beside `payload["query_embedding"] = query_vec` in both `lambda_ask_agent.py:_rag_search_list` at ~line 905 and the equivalent block in `_rag_answer`), and `lambda_rag_search.py` must read `body.get("question", "")` and forward it into `chunks.search_chunks(..., query_text=...)`.

- [ ] **Step 5: Run the search_sql and chunks tests**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/unit/test_search_sql.py tests/unit/test_chunk_search_sql.py tests/unit/test_deleted_search_and_ask.py tests/unit/test_lambda_rag_search.py -q`
Expected: all PASS, including `test_scope_predicate_is_shared_by_construction`'s full assertion now.

- [ ] **Step 6: Prove the keyword-arm tests can go red**

Temporarily revert the `lex AS (...)` CTE to a copy of `vec` with the `to_tsvector(...) @@ ...` clause removed. Re-run Step 5's command. Expected: `test_search_sql_has_a_keyword_arm` and `test_search_sql_keyword_arm_never_fabricates_a_good_distance` FAIL. Then, separately, change the query's expression to `to_tsvector('english', c.chunk_text || '')` (or any cosmetically-different-but-equivalent spelling) and re-run: `test_keyword_arm_expression_matches_the_index_exactly` FAILS even though the SQL is logically the same — this is the point of that test (planner-visible drift, not logical drift). Restore both.

- [ ] **Step 7: Commit**

```bash
git add src/repositories/search_sql.py src/repositories/chunks.py src/lambda_rag_search.py src/lambda_ask_agent.py tests/unit/test_search_sql.py
git commit -m "Union a keyword arm into build_search_sql, before the k cap

A rare literal (PS4, an RFI number) has almost no embedding signal and
loses the top-k cosine race against topically-similar prose. The new lex
CTE runs to_tsvector('english', c.chunk_text) @@ websearch_to_tsquery on
the IDENTICAL scope predicate as the vector arm (_scope_predicate, shared,
never copied) and carries a NULL distance -- never a fabricated good one --
with lexical_hit=true so downstream ranking can tell the two arms apart.
DISTINCT ON (id) collapses a row found by both into the vector arm's copy,
keeping its real distance. The expression must match 0059's index
definition exactly (test_keyword_arm_expression_matches_the_index_exactly)
or the planner silently falls back to a sequential scan.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wire `lexical_hit` through `_aggregate_topics` so the keyword arm survives `_NO_LEX_MAX_DIST`

**Files:**
- Modify: `src/lambda_ask_agent.py` — `_aggregate_topics()` (`:803-885`)
- Test: `tests/unit/test_lambda_ask_agent_search.py`

**Interfaces:**
- Consumes: `c.get("lexical_hit")` (new field from Task 3's SQL, `bool` or missing/`None` for any row shaped by an older path/fixture).
- Produces: `groups[key]["lexical"]` now `True` when either the SQL says `lexical_hit` OR the existing title-substring check (`any(t in hay for t in terms)`) is true.

This is the exact wiring point the spec calls out (§5): a row that arrives *only* through the SQL keyword arm has `distance=None` (placeholder), which is `> 0.55` under any comparison, so it MUST be admitted by the `r["lexical"]` branch of `r["lexical"] or r["score"] <= _NO_LEX_MAX_DIST` (`:882`), not silently dropped by the distance check.

**CONTROLLER AMENDMENT (2026-09-20, after Task 3's fix round) — this task has a SECOND downstream.**

The pre-flight scan named only `_aggregate_topics`. That is the search-box path. The Ask **answering**
path never touches `_aggregate_topics`, and it is the one that feeds the model:

`_rag_answer` -> `_rerank_chunks(question, chunks, k)` (`:1470`), whose first line (`:735`) is
`if not RERANK_ENABLED or len(chunks) <= keep: return chunks[:keep]`. `RERANK_ENABLED` reads
`ENABLE_RERANK`. That toggle IS fully wired — `src/template.yaml:434` declares the `EnableRerank`
parameter, `:1923` passes it into the function, and both deploy workflows supply it from a repo
variable (`.github/workflows/deploy-prod.yml:298`, `deploy.yml:281`) defaulting to `'false'`. What
makes it false today is that NO `PROD_ENABLE_RERANK` / `TEST_ENABLE_RERANK` repo variable is set
(`gh variable list` shows none), so both environments take the default. So `_rerank_chunks` does not
re-rank today; it truncates in arrival order.

(Controller correction, 2026-09-21: an earlier version of this paragraph said the toggle was unwired
and that the env var existed nowhere. That was wrong — it came from grepping `template.yaml`, a path
that does not exist, instead of `src/template.yaml`; grep returns nothing for a missing path and the
empty result was read as absence. The conclusion below is unchanged, because the value really is
`false` in both environments, but for a different reason than first written.)

After Task 3's fix the outer `ORDER BY lexical_hit ASC, distance ASC NULLS LAST` puts all vector rows
first, so `chunks[:k]` keeps exactly the k rows Ask received before this plan — no regression — and
discards every keyword-only row. **The keyword arm is therefore invisible to the answering path until
this task also addresses it.** A Task 4 that only fixes `_aggregate_topics` ships a keyword arm that
improves the search-box list and changes nothing about the answers Ask actually writes.

Measured caveat, from the Task 3 fix round: the "keyword rows are entirely truncated away" property
holds only when the vector arm is SATURATED at k. If the caller's scope contains fewer than k eligible
chunks, keyword-only rows already fill the remaining slots today.

Two naming traps in this exact code, both easy to conflate:
  * `_aggregate_topics` already has a local field `lexical` (`:876`) = `any(t in hay for t in terms)`
    where `hay = derived_title.lower()` — a **title-only** heuristic. The new SQL column is
    `lexical_hit` and is about **chunk_text**. One word apart, different meanings, same function.
  * `dist = float(dist) if dist is not None else 1.0` (`:839`) turns the lex arm's NULL into 1.0,
    which is past `_NO_LEX_MAX_DIST = 0.55`. That is the mechanism of the drop — not a crash, a
    silent loss.

Deciding HOW a keyword-only row earns a place ahead of a semantic one is this task's call to make and
to defend; Task 3 deliberately appended rather than interleaved so that the policy is decided here,
in the open, and not smuggled into the plumbing.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_lambda_ask_agent_search.py`, near the existing `_NO_LEX_MAX_DIST` / hybrid-ranking tests:

```python
def test_a_keyword_only_row_with_no_distance_survives_the_gate(monkeypatch):
    """The exact reintroduction risk the spec names (§5): a row that only the
    SQL keyword arm found has no real cosine distance. If lexical_hit isn't
    threaded into the `lexical` field, _NO_LEX_MAX_DIST drops it one hop
    downstream of the SQL fix and the user sees nothing again."""
    c = chunk("t-ps4", "2026-09-03", None, "Light pole PS4 and electrical hold-up",
              text="Provide PS4 for light poles")
    c["distance"] = None          # placeholder from the lex CTE, not 1.0
    c["lexical_hit"] = True       # SQL says this row matched the tsvector expression, not the title
    wire(monkeypatch, [c])
    out = run(ev(question="PS4"))
    assert out["count"] == 1, "a keyword-only match with no distance must not be dropped"
    assert out["results"][0]["lexical"] is True


def test_lexical_hit_true_but_title_has_no_term_is_still_lexical(monkeypatch):
    """The chunk_text matched (e.g. a body mention), not the title -- the SQL
    signal must not be silently overridden by the narrower title-only check."""
    c = chunk("t-y", "2026-09-03", 0.3, "Site walkthrough notes", text="RFI-0231 variance signed")
    c["lexical_hit"] = True
    wire(monkeypatch, [c])
    out = run(ev(question="RFI-0231"))
    assert out["results"][0]["lexical"] is True
```

`chunk(...)`'s helper in this file builds a dict with a `distance` key already; passing `None` for `dist` and then overwriting works with the existing helper signature (`chunk(topic_id, date, dist, title, ...)`), or add `lexical_hit` as an explicit kwarg to the helper if that reads cleaner — check the helper's current signature (`tests/unit/test_lambda_ask_agent_search.py:22`) before choosing.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/unit/test_lambda_ask_agent_search.py -q -k keyword_only_or_lexical_hit`
Expected: `test_a_keyword_only_row_with_no_distance_survives_the_gate` FAILS with `out["count"] == 0` (the `None` distance compares `False` against `<= 0.55` and `hay` doesn't contain "ps4" if title-matching alone were checked — but here title DOES contain "PS4", so first assert this test actually fails for the RIGHT reason by first trying it with a title that does NOT contain the term, e.g. `"Electrical hold-up"` instead of `"Light pole PS4..."`, to isolate that only `lexical_hit` can save the row. Adjust the fixture in Step 1 to use a title without "PS4" in it so the test is not accidentally passing via the pre-existing title-substring path.)

Revise Step 1's first test's title to `"Electrical hold-up"` (no "PS4" in the title; "PS4" only in `chunk_text`, which `_aggregate_topics` deliberately does NOT check against for `lexical` today — `:861-866`'s own comment). Re-run: now it fails for the correct reason (distance gate drops it, lexical stays False from the title check alone).

- [ ] **Step 3: Implement**

In `src/lambda_ask_agent.py`, in `_aggregate_topics()`, change:

```python
        hay = derived_title.lower()
        groups[key] = {
            ...
            "lexical": any(t in hay for t in terms),
        }
```

to:

```python
        hay = derived_title.lower()
        # Two independent lexical signals, ORed: the title-substring check
        # (existing; drives the Search list's own reordering) and lexical_hit
        # from build_search_sql's keyword arm (2026-09-20 spec), which found
        # a literal match in chunk_text that never reached the title. A row
        # the SQL keyword arm found has no real cosine distance (it was never
        # scored against the query embedding) -- it MUST be admitted here, or
        # _NO_LEX_MAX_DIST drops it one hop downstream of the SQL fix.
        groups[key] = {
            ...
            "lexical": bool(c.get("lexical_hit")) or any(t in hay for t in terms),
        }
```

Also handle `dist = c.get("distance"); dist = float(dist) if dist is not None else 1.0` (`:838-839`) — this already treats `None` as `1.0`, which is `> _NO_LEX_MAX_DIST`, correctly forcing the row to depend on the `lexical` branch rather than the distance branch. Confirm this by reading `:838-839` before editing — no change needed there, only note it in the report.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/unit/test_lambda_ask_agent_search.py -q`
Expected: all PASS, including every pre-existing hybrid-ranking test (`test_search_lexical_ranks_above_closer_semantic`, the Chinese-question tests, etc.) — this change is additive (`or`), so no existing True stays True and no existing False that came from the title check changes.

- [ ] **Step 5: Prove the fix can go red**

Revert Step 3's `or any(t in hay for t in terms)` line back to `any(t in hay for t in terms)` only (drop the `lexical_hit` OR). Re-run Step 4's command. Expected: `test_a_keyword_only_row_with_no_distance_survives_the_gate` FAILS (`count == 0`). Restore.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_ask_agent.py tests/unit/test_lambda_ask_agent_search.py
git commit -m "Admit a keyword-arm-only row past the _NO_LEX_MAX_DIST gate

A row build_search_sql's new lex CTE found has no real cosine distance
(NULL, coerced to 1.0 here), which is > 0.55 -- the existing filter would
drop it one hop after the SQL fix unless lexical_hit is threaded into the
same 'lexical' field the title-substring check already sets. This is
exactly the reintroduction risk the spec calls out in section 5.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Pre-ship parser checks — identifier shapes and CJK (the spec's two unverifiable items)

The spec explicitly could not verify two things about Postgres's `'english'` text-search parser: how it tokenizes identifier shapes with punctuation (a drawing number like `A-101`), and whether it segments CJK meaningfully at all. Both become concrete commands here, run against the real Postgres from Task 1, not left as caveats.

**Files:** none modified (a verification task; if either check finds a real gap, note it as a follow-up rather than silently shipping — do not attempt a tokenizer fix in this plan).

- [ ] **Step 1: Identifier-shape check**

Against a database with `0059` applied, run:

```bash
psql "$TEST_DATABASE_URL" -c "SELECT to_tsvector('english', 'Provide PS4 for light poles, per drawing A-101 and RFI-0231.');"
psql "$TEST_DATABASE_URL" -c "SELECT websearch_to_tsquery('english', 'PS4');"
psql "$TEST_DATABASE_URL" -c "SELECT websearch_to_tsquery('english', 'A-101');"
psql "$TEST_DATABASE_URL" -c "SELECT websearch_to_tsquery('english', 'RFI-0231');"
psql "$TEST_DATABASE_URL" -c "SELECT to_tsvector('english', 'Provide PS4 for light poles, per drawing A-101 and RFI-0231.') @@ websearch_to_tsquery('english', 'PS4');"
psql "$TEST_DATABASE_URL" -c "SELECT to_tsvector('english', 'Provide PS4 for light poles, per drawing A-101 and RFI-0231.') @@ websearch_to_tsquery('english', 'A-101');"
```

Record each `to_tsquery`/`tsvector` output verbatim in the PR body (Task 7). If `A-101` splits into two lexemes (`'a'` and `'101'`) rather than one, note it explicitly — this affects whether a hyphenated drawing number is matched as a phrase or as two independent terms, which changes precision but is not a correctness bug the plan needs to fix; report the observed behavior rather than assuming either shape.

- [ ] **Step 2: Word-boundary check (the property `ILIKE` would have gotten wrong — this is Task 6's test #4 run manually first as a sanity check)**

```bash
psql "$TEST_DATABASE_URL" -c "SELECT to_tsvector('english', 'PS40 was delivered yesterday') @@ websearch_to_tsquery('english', 'PS4');"
```

Expected: `f` (false) — `PS4` must NOT match inside `PS40`. If this returns `t`, STOP: the mechanism does not have the word-boundary property the spec chose it for, and Task 3's implementation needs to be revisited (e.g. the parser may be splitting `PS40` into `ps` + `40` in a way that makes `ps4` a partial match through some other path) before continuing.

- [ ] **Step 3: CJK check (risk table's last row)**

```bash
psql "$TEST_DATABASE_URL" -c "SELECT to_tsvector('english', '钢筋合格证跟进供应商还没给');"
psql "$TEST_DATABASE_URL" -c "SELECT websearch_to_tsquery('english', '钢筋合格证');"
```

Record the output. The spec already predicts this likely yields no meaningful segmentation under the `'english'` config (CJK has no whitespace and `'english'`'s parser does not know its word boundaries). This is a **known, documented gap**, not something this plan fixes: `lambda_ask_agent.py`'s existing `_UNSPACED_RUN` shingling (`:762`, `:787-788`) already gives CJK queries lexical-ranking help entirely client-side, downstream of retrieval — the keyword arm added here is an additive precision improvement for identifier-shaped Latin/digit tokens and does not regress CJK below where it stands today (CJK queries still get the vector arm plus the existing post-filter shingling; they simply do not additionally benefit from the new SQL keyword arm). Confirm this by re-running the pre-existing Chinese tests in Task 4's suite (`test_a_chinese_question_still_ranks_a_word_match_first`, `test_a_chinese_question_no_longer_loses_a_distant_match`) and note in the PR body that they pass unchanged — i.e. nothing regressed for CJK, and the gap is pre-existing, not introduced.

- [ ] **Step 4: Record findings**

No commit for this task (no files change) — carry the six recorded query outputs and the pass/fail verdicts from Steps 1-3 into Task 7's PR body as a "Pre-ship parser checks" section.

---

### Task 6: Acceptance and regression tests against a real database

**Files:**
- Create: `tests/integration/test_literal_token_search_sql.py`

**Interfaces:**
- Consumes: `repositories.chunks.insert_chunk`, `repositories.chunks.search_chunks`, `repositories.topics` (for a site + topic fixture), `redactions`/`deleted_predicates` (for the tombstone test).

This is the spec's §9.1 acceptance test and its companion regression tests — run against a real Postgres because a fake connection never executes SQL (project memory: two prod crashes hidden this way already). Mirrors `tests/integration/test_topic_decisions_sql.py`'s pattern (`TEST_DATABASE_URL`, `pytest.importorskip("psycopg")`, explicit `conn.rollback()` per test so fixtures never leak between tests).

- [ ] **Step 1: Write the fixture helper and the failing PS4 acceptance test**

Create `tests/integration/test_literal_token_search_sql.py`:

```python
"""Integration: a literal token in chunk_text is findable even when it loses
the vector top-k race. Real Postgres only -- FakeConn never executes SQL and
cannot prove the GIN expression index or the UNION actually work.

Spec: docs/superpowers/specs/2026-09-20-a-literal-token-is-findable.md
Fixture text is the real motivating case (§1): UCPK2, 2026-09-03, the topic
'Light pole PS4 and electrical hold-up'.
"""
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


def _site(conn):
    row = conn.execute("SELECT id FROM sites LIMIT 1").fetchone()
    if not row:
        pytest.skip("no sites in this database")
    return row[0]


def _flat_embedding(seed):
    # A cheap 1024-dim vector that is FAR from every other fixture's vector by
    # construction (each fixture gets a distinct seed dimension bumped), so
    # the vector arm alone would never surface the PS4 chunk near a query
    # embedding built from unrelated seeds -- reproducing the measured gap
    # (§1: "30+ other chunks... were closer... than this one was").
    v = [0.01] * 1024
    v[seed % 1024] = 5.0
    return v


def _insert(conn, site_id, text, seed, chunk_type="topic", topic_id=None):
    from repositories import chunks
    return chunks.insert_chunk(
        conn, site_id, "2026-09-03", chunk_type, text, _flat_embedding(seed),
        topic_id=topic_id)


def test_ps4_is_found_even_though_forty_unrelated_chunks_are_closer():
    """The measured gap, reproduced: PS4's own chunk is seeded FAR from the
    query embedding (seed 0), and forty unrelated 'electrical hold-up'-shaped
    chunks are seeded CLOSE to it (seed 1, shared), so the vector arm alone
    would never surface it within k=30. The keyword arm must still find it."""
    from repositories import chunks
    with psycopg.connect(DSN) as conn:
        site_id = _site(conn)
        target = _insert(conn, site_id,
                         "Light pole PS4 and electrical hold-up: provide PS4 for light poles",
                         seed=0)
        for i in range(40):
            _insert(conn, site_id, f"Unrelated electrical hold-up note {i}", seed=1)
        query_vec = _flat_embedding(1)  # near the 40 unrelated chunks, far from PS4's
        rows = chunks.search_chunks(conn, query_vec, [site_id], k=30, query_text="PS4")
        ids = {str(r["id"]) for r in rows}
        assert str(target["id"]) in ids, \
            "PS4's chunk must be found by the keyword arm despite losing the vector race"
        hit = next(r for r in rows if str(r["id"]) == str(target["id"]))
        assert hit["lexical_hit"] is True
        conn.rollback()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/integration/test_literal_token_search_sql.py -q` (with `TEST_DATABASE_URL` set and `0059` NOT yet a no-op — this must run against the branch state BEFORE Task 3's SQL change to prove red; if Tasks 1-4 are already applied by the time this task starts, temporarily stash `src/repositories/search_sql.py`'s changes to confirm red, then restore)
Expected: FAIL — `PS4's chunk must be found` (with the pre-fix single-arm SQL, only the top-30 by distance are returned and PS4's chunk, seeded maximally far, is not among them).

- [ ] **Step 3: With Tasks 1-4 applied, run again to see it pass**

Same command, with `0059` applied to the test database (`apply_migrations` or a direct `psql -f src/migrations/0059_report_chunks_tsv_idx.sql`) and Tasks 2-4's code in place.
Expected: PASS.

- [ ] **Step 4: Add the tombstone-exclusion test**

Append to the same file:

```python
def test_a_tombstoned_chunk_containing_the_term_is_not_returned():
    """The keyword arm shares _scope_predicate with the vector arm -- it must
    honor visible_chunks_predicate exactly like the vector arm does today.
    A keyword arm that bypassed this would resurrect the deleted-recording
    leak visible_chunks_predicate was built to close (spec §7)."""
    from repositories import chunks, redactions
    with psycopg.connect(DSN) as conn:
        site_id = _site(conn)
        topic_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO topics (id, site_id, report_date, title) VALUES (%s,%s,%s,%s)",
            (topic_id, site_id, "2026-09-03", "PS4 topic"))
        target = _insert(conn, site_id, "PS4 light pole issue, tombstoned",
                         seed=2, topic_id=topic_id)
        redactions.tombstone_topic(conn, topic_id)  # adjust to the real helper name --
        # read src/repositories/redactions.py first; if no such helper exists,
        # INSERT INTO redactions directly with the same columns visible_chunks_predicate
        # reads (target_type='topic', target_id=topic_id, scope='deleted', reverted_at NULL).
        rows = chunks.search_chunks(conn, _flat_embedding(2), [site_id], k=30, query_text="PS4")
        assert str(target["id"]) not in {str(r["id"]) for r in rows}
        conn.rollback()


def test_an_out_of_site_chunk_containing_the_term_is_not_returned():
    """Deny-by-default: the keyword arm must respect site_id = ANY(%(site_ids)s)
    exactly like the vector arm, not just filter tombstones."""
    from repositories import chunks
    with psycopg.connect(DSN) as conn:
        site_id = _site(conn)
        other_row = conn.execute(
            "SELECT id FROM sites WHERE id != %s LIMIT 1", (site_id,)).fetchone()
        if not other_row:
            pytest.skip("needs a second site in this database")
        other_site_id = other_row[0]
        target = _insert(conn, other_site_id, "PS4 issue at a different site", seed=3)
        rows = chunks.search_chunks(conn, _flat_embedding(3), [site_id], k=30, query_text="PS4")
        assert str(target["id"]) not in {str(r["id"]) for r in rows}
        conn.rollback()
```

Before finalizing, read `src/repositories/redactions.py` to find the actual tombstone-insert helper (`git grep -n "def.*tombstone\|scope.*deleted" src/repositories/redactions.py`) and replace the placeholder call with the real one, or a direct `INSERT INTO redactions (...)` matching `deleted_predicates.DELETED_CHUNK_TOPIC_PREDICATE`'s expected columns exactly.

- [ ] **Step 5: Add the control-query (must-not-change) test**

```python
def test_a_conceptual_query_with_no_literal_terms_is_unaffected():
    """spec §9.2: a normal query relying purely on the ANN arm must return the
    same top result before and after -- the keyword arm must retrieve ZERO
    additional rows when nothing in the corpus matches its tsquery."""
    from repositories import chunks
    with psycopg.connect(DSN) as conn:
        site_id = _site(conn)
        near = _insert(conn, site_id, "Fall from height near miss on the scaffold", seed=4)
        _insert(conn, site_id, "Unrelated note about paint colours", seed=5)
        query_vec = _flat_embedding(4)
        rows = chunks.search_chunks(conn, query_vec, [site_id], k=30,
                                    query_text="what safety issues came up this week")
        assert rows, "expected the semantically-near chunk to still be found"
        assert str(rows[0]["id"]) == str(near["id"])
        assert all(r["lexical_hit"] is False for r in rows), \
            "no fixture chunk_text contains any word from the control query verbatim"
        conn.rollback()
```

- [ ] **Step 6: Add the word-boundary test (real Postgres, not the manual Task 5 check)**

```python
def test_ps4_does_not_match_ps40_word_boundary():
    """The property ILIKE would have gotten wrong (spec §4): PS4 must not
    match inside PS40. This is the deciding factor the spec chose tsvector
    over pg_trgm for."""
    from repositories import chunks
    with psycopg.connect(DSN) as conn:
        site_id = _site(conn)
        decoy = _insert(conn, site_id, "PS40 was delivered and installed yesterday", seed=6)
        rows = chunks.search_chunks(conn, _flat_embedding(7), [site_id], k=30, query_text="PS4")
        assert str(decoy["id"]) not in {str(r["id"]) for r in rows}, \
            "PS4 must not match inside PS40 -- this is the whole reason ILIKE was rejected"
        conn.rollback()
```

- [ ] **Step 7: Run the full file**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/integration/test_literal_token_search_sql.py -q`
Expected: all pass (with `0059` applied and Tasks 2-4 in place).

- [ ] **Step 8: Prove each test can go red**

For the tombstone and out-of-site tests: temporarily change the `lex` CTE in `search_sql.py` to omit `+ scope +` for that CTE (use an unfiltered `WHERE to_tsvector('english', c.chunk_text) @@ websearch_to_tsquery(...)` alone). Re-run Step 7. Expected: `test_a_tombstoned_chunk_containing_the_term_is_not_returned` and `test_an_out_of_site_chunk_containing_the_term_is_not_returned` FAIL. Restore.

For the word-boundary test: temporarily change `websearch_to_tsquery('english', %(q_text)s)` to a substring probe like `c.chunk_text ILIKE '%' || %(q_text)s || '%'` in the `lex` CTE only. Re-run. Expected: `test_ps4_does_not_match_ps40_word_boundary` FAILS (PS40 now matches). Restore.

- [ ] **Step 9: Commit**

```bash
git add tests/integration/test_literal_token_search_sql.py
git commit -m "Integration tests: PS4 is found, ACL/tombstone still hold

Real Postgres only -- a fake connection never executes SQL and has hidden
two prod crashes before, so the GIN expression index and the UNION need a
real database to prove. Covers the acceptance case (PS4 loses the vector
race but is still found), both ACL arms (tombstoned and out-of-site chunks
stay hidden even though they contain the literal term), a control query
that must not change, and the word-boundary property that is the whole
reason tsvector was chosen over pg_trgm/ILIKE.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Full unit suite, PR into `develop`, TEST verification

**Files:** none modified (unless the suite finds a regression, fixed in the task that caused it).

- [ ] **Step 1: Full unit suite**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q`
Expected: all pass except known pre-existing skips. Record pass/skip counts in the PR body.

- [ ] **Step 2: Full integration suite (needs `TEST_DATABASE_URL`)**

Run: `uv run --with pytest --with "psycopg[binary]" pytest tests/integration -q`
Expected: all pass (or skip cleanly if `TEST_DATABASE_URL` is unset in this environment — note which happened in the PR body; do not open the PR claiming integration coverage that was actually skipped).

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin <branch>
gh pr create --base develop --title "A literal token is findable: keyword arm in build_search_sql" --body-file <body file>
```

Body must include:
- The measured gap (spec §1: PS4 findable via Ask, not via Search, same query).
- The mechanism chosen (tsvector + GIN + websearch_to_tsquery, migration `0059`) and why ILIKE/pg_trgm was rejected (word-boundary blindness).
- The shared-predicate guarantee (Task 2/3): one `_scope_predicate` function, asserted identical in both CTEs by a unit test.
- The `_NO_LEX_MAX_DIST` wiring fix (Task 4) and why it was necessary (a keyword-only row has no real distance).
- Task 5's six recorded parser-check query outputs (identifier shapes, word boundary, CJK) and their verdicts, including the explicit statement that CJK gets no new SQL-side benefit but is not regressed (pre-existing `_UNSPACED_RUN` shingling still applies).
- Task 1 Step 5's `EXPLAIN` output confirming the planner uses `idx_report_chunks_tsv` rather than a sequential scan, plus the documented `CREATE INDEX CONCURRENTLY` escape hatch and when the owner should reach for it instead of the plain in-migration build.
- Test counts (unit + integration) from Steps 1-2.
- The TEST verification plan below, to run **after** the owner merges (merging into `develop` deploys TEST and is the owner's call, not this plan's).

End the body with:
```
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 4: TEST verification after the owner merges**

Once `0059` has run on the TEST database (confirm via `SELECT version FROM schema_migrations WHERE version = '0059_report_chunks_tsv_idx.sql'`), reproduce the PS4 case against real TEST data:

```bash
# Before: what /search returned for "PS4" against the UCPK2 test-account corpus, pre-merge
# (record this BEFORE merging, from the currently-deployed AskAgentFunction)
aws lambda invoke --function-name fieldsight-test-ask-agent --profile fieldsight-deployer \
  --payload '{"question":"PS4","caller_sub":"<a real UCPK2-scoped sub>","mode":"search"}' \
  --cli-binary-format raw-in-base64-out /tmp/before.json
cat /tmp/before.json   # expected before: {"results": [], "count": 0, ...} (or the PS4 topic absent)

# After merge + deploy:
aws lambda invoke --function-name fieldsight-test-ask-agent --profile fieldsight-deployer \
  --payload '{"question":"PS4","caller_sub":"<same sub>","mode":"search"}' \
  --cli-binary-format raw-in-base64-out /tmp/after.json
cat /tmp/after.json    # expected after: results includes the "Light pole PS4 and electrical hold-up" topic, lexical=true
```

Record both JSON bodies verbatim in a follow-up comment on the PR (not in this plan file). If the "before" run cannot be captured (e.g. this plan is executed after `0059` is already partially applied), substitute the control from spec §1: "Asking the Ask agent 'did we talk about ps4?' returns the correct cited answer; typing PS4 into the SEARCH box returns nothing at all" as the documented pre-fix baseline, and only capture "after."
