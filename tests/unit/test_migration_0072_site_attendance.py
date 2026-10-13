"""Unit: migration 0072 has the shape the repository and the roster consumer rely on.

Text checks only -- the SQL is applied for real by tests/integration (CI Postgres). The
version guard matters because two version collisions have already shipped (0041, 0044) and
the runner orders ties by filename, so a second 0072 from a parallel branch would apply in
an order no one chose.
"""
import os

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
NAME = "0072_site_attendance.sql"


def _sql():
    with open(os.path.join(MIGRATIONS, NAME), encoding="utf-8") as fh:
        return fh.read()


def test_no_other_migration_uses_version_0072():
    assert [f for f in os.listdir(MIGRATIONS) if f.startswith("0072_")] == [NAME]


def test_it_creates_the_table_if_not_exists():
    sql = " ".join(_sql().split())
    assert "CREATE TABLE IF NOT EXISTS site_attendance" in sql


def test_it_has_every_required_column():
    sql = " ".join(_sql().split())
    for column in (
        "company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE",
        "site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE",
        "attend_date date NOT NULL",
        "display_name text NOT NULL",
        "user_id uuid REFERENCES users(id)",
        "voiceprint_id uuid REFERENCES speaker_voiceprints(id) ON DELETE SET NULL",
        "employer_name text",
        "source text NOT NULL CHECK (source IN",
        "source_ref text NOT NULL",
        "first_seen_at timestamptz NOT NULL DEFAULT now()",
        "last_seen_at timestamptz NOT NULL DEFAULT now()",
        "created_by uuid REFERENCES users(id)",
    ):
        assert column in sql, column


def test_it_checks_the_closed_set_of_sources():
    sql = " ".join(_sql().split())
    for source in ("graph_calendar", "signonsite", "hammertech", "1breadcrumb", "manual"):
        assert f"'{source}'" in sql, source


def test_it_has_the_idempotency_unique_and_the_lookup_index():
    sql = " ".join(_sql().split())
    assert "UNIQUE (company_id, source, source_ref, attend_date)" in sql
    assert "site_attendance (company_id, site_id, attend_date)" in sql
