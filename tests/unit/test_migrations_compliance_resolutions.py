"""0025_compliance_resolutions.sql is additive and carries the corrected
6-column natural key (spec 2026-07-26 §1). Static text check, same style as
test_migrations_content_edits.py (no live Postgres in unit CI)."""
import os
import re

MIG = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                   "0025_compliance_resolutions.sql")


def _sql():
    return open(MIG, encoding="utf-8").read().lower()


def test_compliance_resolutions_migration_is_additive_and_complete():
    sql = _sql()
    assert "create table compliance_resolutions" in sql
    for col in ("company_id", "site_id", "report_date", "user_folder", "domain",
                "content_hash", "content_sample", "resolved", "resolved_by",
                "resolved_at", "created_at", "updated_at"):
        assert col in sql, col
    # additive only — never destructive on the shared cluster.
    assert not re.search(r"\bdrop\b|\balter\b", sql)


def test_unique_key_includes_user_folder_1a():
    sql = _sql()
    # the 1a correction: user_folder IS in the UNIQUE key (not just site_id).
    assert ("unique (company_id, site_id, report_date, domain, user_folder, "
            "content_hash)") in sql


def test_domain_check_constraint_and_fks():
    sql = _sql()
    assert "check (domain in ('safety','quality'))" in sql
    assert "references companies(id) on delete cascade" in sql
    assert "references sites(id)" in sql
    assert "references users(id)" in sql          # resolved_by
    assert "resolved       boolean not null default true" in sql or \
           "resolved boolean not null default true" in sql


def test_range_index_present():
    assert "idx_compres_range" in _sql()
