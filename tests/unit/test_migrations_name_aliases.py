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
