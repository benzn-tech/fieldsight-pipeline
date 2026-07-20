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
