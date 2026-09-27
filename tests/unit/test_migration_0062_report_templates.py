"""Unit: migration 0062 has the shape the repository and the routes rely on.

Text checks plus a real parse -- the SQL is EXECUTED for real by
tests/integration/test_report_templates_schema.py (CI Postgres). That split
matters here more than usual: a fake connection records SQL without running it,
and this repo has already shipped two production crashes that every unit test
was green for. Nothing in this file proves the database would accept a row.

The version guard matters because two version collisions have already shipped
(0041, 0044) and the runner orders ties by filename, so a second 0062 from a
parallel branch would apply in an order nobody chose.
"""
import os

import pytest

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
NAME = "0062_report_templates.sql"


def _sql():
    with open(os.path.join(MIGRATIONS, NAME), encoding="utf-8") as fh:
        return fh.read()


def _flat():
    return " ".join(_sql().split())


def _statements():
    """The SQL with `--` comment lines removed.

    The header comment names the two template systems this migration must not
    disturb, so a check for their absence has to look at what actually runs,
    not at the prose explaining why it does not run.
    """
    lines = [ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--")]
    return " ".join(" ".join(lines).split())


def test_no_other_migration_uses_version_0062():
    assert [f for f in os.listdir(MIGRATIONS) if f.startswith("0062_")] == [NAME]


def test_postgres_own_grammar_accepts_it():
    """Not a substitute for running it -- it proves the syntax, nothing about
    whether the constraints do what they say."""
    pglast = pytest.importorskip("pglast", reason="pglast not installed")
    stmts = pglast.parse_sql(_sql())
    kinds = [type(s.stmt).__name__ for s in stmts]
    assert kinds.count("CreateStmt") == 3, "three tables"


def test_the_three_tables_exist():
    sql = _flat()
    for table in ("CREATE TABLE IF NOT EXISTS report_templates",
                  "CREATE TABLE IF NOT EXISTS report_template_versions",
                  "CREATE TABLE IF NOT EXISTS report_template_bindings"):
        assert table in sql, table


def test_scope_and_owner_cannot_disagree():
    """An org template owned by a person, or a personal one owned by nobody,
    would silently change who can see the row."""
    sql = _flat()
    assert "CHECK (scope IN ('org', 'personal'))" in sql
    assert "CHECK ((scope = 'personal') = (owner_user_id IS NOT NULL))" in sql


def test_archiving_frees_the_slug():
    """The unique index is partial. Without the WHERE, a company that tidies up
    could never reuse a name it once used."""
    sql = _flat()
    assert "CREATE UNIQUE INDEX IF NOT EXISTS report_templates_slug_uq" in sql
    assert "WHERE archived_at IS NULL" in sql
    # NULLs do not compare equal, so org rows need one bucket to collide in.
    assert "coalesce(owner_user_id, '00000000-0000-0000-0000-000000000000'::uuid)" in sql


def test_versions_are_append_only_and_numbered_from_one():
    sql = _flat()
    assert "version integer NOT NULL CHECK (version > 0)" in sql
    assert "UNIQUE (template_id, version)" in sql
    # Nothing may edit a body in place: a report records template_id + version.
    assert "UPDATE report_template_versions" not in sql


def test_a_template_with_no_body_yet_is_version_zero():
    """Distinct from a template whose body is empty."""
    assert "current_version integer NOT NULL DEFAULT 0" in _flat()


def test_archiving_a_bound_template_must_fail_loudly():
    """ON DELETE RESTRICT, never CASCADE. A binding vanishing because somebody
    archived a template is exactly the silent fallback-to-default this design
    refuses."""
    sql = _flat()
    assert "template_id uuid NOT NULL REFERENCES report_templates(id) ON DELETE RESTRICT" in sql
    assert "ON DELETE CASCADE" in sql, "versions DO cascade"
    assert sql.count("ON DELETE CASCADE") == 1, "only the versions table cascades"


def test_one_binding_per_company_and_report_type():
    sql = _flat()
    assert "PRIMARY KEY (company_id, report_type)" in sql
    # The schedule has three kinds; 'session'/'day' are on-demand and unbindable.
    assert "CHECK (report_type IN ('daily', 'weekly', 'monthly'))" in sql


def test_it_does_not_touch_the_two_template_systems_already_shipping():
    """The disk templates (src/report_templates) and the S3 prompt templates
    stay exactly where they are. A report names the template it was written to,
    and a silent substitution would make that name a lie."""
    sql = _statements()
    for untouched in ("prompt_templates", "DROP ", "ALTER TABLE", "DELETE FROM", "TRUNCATE"):
        assert untouched not in sql, untouched
