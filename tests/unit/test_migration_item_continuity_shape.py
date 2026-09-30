"""Shape of migration 0076 (spec 2026-09-30 D1, §5): a nullable item_id with no default."""
import glob
import os
import re

HERE = os.path.dirname(__file__)
MIG_DIR = os.path.join(HERE, "..", "..", "src", "migrations")
PATH = os.path.join(MIG_DIR, "0076_item_continuity.sql")


def _flat():
    with open(PATH, encoding="utf-8") as f:
        sql = "\n".join(l for l in f.read().splitlines() if not l.strip().startswith("--"))
    return re.sub(r"\s+", " ", sql)


def test_no_other_migration_uses_version_0076():
    assert [os.path.basename(p) for p in glob.glob(os.path.join(MIG_DIR, "0076_*.sql"))] == [
        "0076_item_continuity.sql"]


def test_item_id_is_added_to_all_four_child_tables_nullable_without_default():
    sql = _flat()
    for table in ("action_items", "findings", "topic_decisions", "topic_questions"):
        assert f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS item_id uuid;" in sql


def test_no_default_and_no_index_so_there_is_no_table_rewrite():
    sql = _flat()
    assert "DEFAULT" not in sql.upper()
    assert "CREATE INDEX" not in sql.upper()
    assert "NOT NULL" not in sql.upper()
