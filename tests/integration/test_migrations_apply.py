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


def test_extensions_installed():
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        names = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        assert "vector" in names
        assert "pgcrypto" in names
    finally:
        conn.close()


def test_platform_company_seeded():
    # Phase 3 T3 / D6: 0015 seeds the dedicated FieldSight-platform company row
    # so a platform_admin's company_id stays NOT NULL.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        rows = conn.execute(
            "SELECT name, industry FROM companies WHERE name='FieldSight-platform'"
        ).fetchall()
        assert rows == [("FieldSight-platform", "platform")]
    finally:
        conn.close()


def test_sites_has_coordinate_columns():
    # 0018: nullable lat/lng for weather + map features.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='sites'"
            ).fetchall()
        }
        assert {"latitude", "longitude"} <= cols
    finally:
        conn.close()


def test_report_chunks_has_a_tsvector_expression_index():
    # 0059 created idx_report_chunks_tsv (a plain, non-CONCURRENT expression
    # index -- CREATE INDEX CONCURRENTLY cannot run inside apply_migrations'
    # conn.transaction() wrapper, and a GENERATED STORED column's ACCESS
    # EXCLUSIVE table rewrite has no measurable duration in this repo, no
    # read-only path to prod's row count). 0061 (2026-09-22, "the keyword arm
    # can actually fire") REPLACES it with idx_report_chunks_tsv_multi_config
    # -- the 'english' vector OR'd with a 'simple' vector over punctuation-
    # split text, needed so an identifier touching punctuation (PS4/light)
    # is still findable -- and DROPs the old one, since its expression no
    # longer appears anywhere in build_search_sql()'s output.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        rows = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename='report_chunks' AND indexname='idx_report_chunks_tsv_multi_config'"
        ).fetchall()
        assert rows, "idx_report_chunks_tsv_multi_config is missing"
        indexdef = rows[0][0].lower()
        assert "gin" in indexdef
        assert "to_tsvector" in indexdef
        assert "chunk_text" in indexdef
        assert "regexp_replace" in indexdef, \
            "the 'simple' half of the combined expression is missing -- an " \
            "identifier touching punctuation would not be findable again"

        old = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname='idx_report_chunks_tsv'"
        ).fetchall()
        assert not old, "idx_report_chunks_tsv should have been dropped by migration 0061"

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
    # IF NOT EXISTS -- also the property the CONCURRENTLY escape hatch (0061's
    # documented manual path) depends on: if a DBA builds the index manually
    # first, this migration's own CREATE INDEX must be a no-op, not an error.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_report_chunks_tsv_multi_config "
            "ON report_chunks USING gin ("
            "(to_tsvector('english', chunk_text) "
            "|| to_tsvector('simple', regexp_replace(chunk_text, '[^a-zA-Z0-9]+', ' ', 'g'))))")
        rows = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname='idx_report_chunks_tsv_multi_config'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_platform_company_seed_sql_is_idempotent_on_direct_rerun():
    # Belt-and-suspenders on top of the schema_migrations gate (which already
    # prevents a file from re-running via apply_migrations): even a manual
    # re-run of 0015's raw SQL (e.g. a recovery script) must not double-insert
    # the platform company row -- the file's own WHERE NOT EXISTS guard.
    conn = _fresh_conn()
    try:
        apply_migrations(conn, MIGRATIONS_DIR)
        path = os.path.join(MIGRATIONS_DIR, "0015_platform_company.sql")
        with open(path, "r", encoding="utf-8") as fh:
            sql = fh.read()
        conn.execute(sql)
        conn.execute(sql)
        count = conn.execute(
            "SELECT count(*) FROM companies WHERE name='FieldSight-platform'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()
