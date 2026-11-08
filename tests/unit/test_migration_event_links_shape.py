"""Unit: migration 0098 has the shape the evidence-chain writers rely on.

Text checks only -- the SQL is applied for real by
tests/integration/test_event_links_schema.py. The version guard matters because version
collisions have shipped before (0041, 0044) and the runner orders ties by filename.
"""
import os
import re

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
NAME = "0098_event_links.sql"


def _sql():
    with open(os.path.join(MIGRATIONS, NAME), encoding="utf-8") as fh:
        return fh.read()


def _flat():
    return " ".join(_sql().split())


def _create_table():
    sql = _sql()
    start = sql.index("CREATE TABLE IF NOT EXISTS event_links")
    end = sql.index("\n);", start)
    return sql[start:end]


def test_only_one_migration_uses_0098():
    assert [f for f in os.listdir(MIGRATIONS) if f.startswith("0098_")] == [NAME]


def test_event_links_has_no_company_id():
    """Tenancy follows the project (site_id), not the company."""
    assert "company_id" not in _create_table()


def test_every_src_fk_cascades():
    lines = [ln for ln in _create_table().splitlines()
             if re.match(r"\s*src_(action_item|finding|decision|question)_id\s", ln)]
    assert len(lines) == 4, lines
    for ln in lines:
        assert "REFERENCES" in ln and "ON DELETE CASCADE" in ln, ln


def test_link_types_are_exactly_the_four():
    flat = " ".join(_create_table().split())
    m = re.search(r"CHECK \(link_type IN \(([^)]*)\)\)", flat)
    assert m, "link_type CHECK missing"
    values = [v.strip().strip("'") for v in m.group(1).split(",")]
    assert values == ["supported_by", "resolves", "continues", "supersedes"]


def test_claim_columns_on_all_four_tables():
    sql = _flat()
    for table in ("action_items", "findings", "topic_decisions", "topic_questions"):
        assert f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS claim_type text;" in sql, table
        assert f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS claim_basis jsonb;" in sql, table
        assert (
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_claim_type_check "
            "CHECK (claim_type IN ('observed','stated','inferred'));" in sql
        ), table


def test_every_add_constraint_is_pg_constraint_guarded():
    """Postgres has no ADD CONSTRAINT IF NOT EXISTS; an unguarded one makes the file fail
    on a second execution. Each must sit in a DO block that checks pg_constraint for its
    own name first (the shape of 0092_company_status.sql)."""
    sql = re.sub(r"--[^\n]*", "", _sql())  # comments may mention the syntax
    blocks = [(m.start(), m.end(), m.group(0))
              for m in re.finditer(r"DO \$\$.*?END \$\$;", sql, re.S)]
    adds = list(re.finditer(r"ADD CONSTRAINT (\w+)", sql))
    assert len(adds) == 4, [m.group(1) for m in adds]
    for m in adds:
        name = m.group(1)
        inside = [b for b in blocks if b[0] < m.start() < b[1]]
        assert inside, f"{name} is not inside a DO block"
        flat = " ".join(inside[0][2].split())
        assert (f"IF NOT EXISTS ( SELECT 1 FROM pg_constraint WHERE conname = '{name}'"
                in flat), name
