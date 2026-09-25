"""The four starter templates are bodies this code can actually consume.

They lived in the browser as a fixture for months (scripts/mock/templates.fixture.js)
and were never once fed to report_template. A fixture only has to look like a
template; these have to BE one -- render_prompt subscripts `catch_all`, and
every section by `title` and `purpose`, so a missing key is not a worse report,
it is a KeyError inside the non-VPC worker long after the person who picked the
template has gone home.

THE test is `every starter template validates and renders`. It reads the bodies
out of the migration rather than out of a copy kept beside it: a second copy
would be the thing tested, and the migration would be the thing that ships.

The fixture's shape differed from the real one in two ways worth pinning, both
of which would have failed silently:

  `prompt_hint` was the editor's name for the sentence `purpose` holds. A body
  that kept the old name would validate (the key is simply absent) -- no. It
  would not; validate_body refuses a section with no purpose, which is why
  this file can assert it rather than hope.

  `fields` was never read by anything server-side. On the table sections it
  held exactly what `columns` now needs, so it carries over there; everywhere
  else it is dropped. A `fields` left in the seeded bodies would be a field
  nothing consumes, seeded into every company -- which is the shape of defect
  this line has spent two days removing.
"""
import io
import json
import os
import re

import pytest

rt = pytest.importorskip("report_template")

# 0067, not 0066: `0066_speaker_name_proposals.sql` shipped from the voiceprint
# line while this was in review and is already applied on prod. The runner
# handles a duplicate number correctly -- schema_migrations is keyed on the full
# filename and pending_versions tie-breaks on it, so both apply in a defined
# order -- and two such collisions have shipped before. Renumbering costs
# nothing and does not add a third to a ledger that already carries two.
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "src",
                         "migrations", "0067_starter_report_templates.sql")

SCOPE = {"folder": "Ben_UCPK2", "date": "2026-09-24", "from": "00:00", "to": "23:59",
         "recordings": 3}


def starters():
    """The bodies as the migration will insert them."""
    sql = io.open(MIGRATION, encoding="utf-8").read()
    bodies = re.findall(r"\$body\$(.*?)\$body\$", sql, re.DOTALL)
    assert bodies, "the dollar-quoted bodies have moved"
    return [json.loads(b) for b in bodies]


def names():
    sql = io.open(MIGRATION, encoding="utf-8").read()
    return re.findall(r"^\s*\('([a-z0-9-]+)', '([^']+)', '([a-z]+)',", sql, re.MULTILINE)


# ---- THE test ---------------------------------------------------------------

def test_THE_test_every_starter_validates_and_renders():
    bodies = starters()
    assert len(bodies) == 4, "four templates, as specified"
    for body in bodies:
        assert rt.validate_body(body) is None, json.dumps(body)[:200]
        prompt = rt.render_prompt(body, SCOPE, [], "[09:00] Ben: morning",
                                  source=rt.SOURCE_LIBRARY)
        for section in body["sections"] + [body["catch_all"]]:
            assert "### " + section["title"] in prompt
            assert section["purpose"] in prompt


# ---- the shape the fixture had and the real one does not --------------------

def test_no_starter_carries_prompt_hint_or_fields():
    """Both were the browser's. `prompt_hint` is `purpose` under another name,
    and `fields` is read by nothing -- seeding it into every company would be
    seeding the very shape this line has spent two days removing: a value that
    can be stored and has no landing point in the output."""
    for body in starters():
        for section in body["sections"] + [body["catch_all"]]:
            assert "prompt_hint" not in section
            assert "fields" not in section


def test_no_starter_has_a_photos_section():
    """Nothing in the generated path inserts an image. Seeding one into every
    company would be seeding a promise this product cannot keep today."""
    for body in starters():
        for section in body["sections"] + [body["catch_all"]]:
            assert section.get("kind") != "photos"


def test_the_incident_evidence_section_is_prose_and_says_so():
    """It survives because what it is FOR is the written account of what
    evidence exists -- which the model can write. Named like the photo section
    it replaces, so this pins that it is not one."""
    incident = [b for b in starters()
                if any(s["title"] == "Root Cause" for s in b["sections"])][0]
    ev = [s for s in incident["sections"] if s["title"] == "Photos & Evidence"][0]
    assert ev["kind"] == "narrative"
    assert "Describe it" in ev["purpose"], "it must say what it is asking for"


# ---- columns ----------------------------------------------------------------

def test_every_table_section_names_its_own_columns():
    """The fallback exists for bodies written before the field did. These were
    written after, so they decide -- and a starter that fell back would teach
    every company that Item | Assigned | Due is what a table is."""
    found = 0
    for body in starters():
        for section in body["sections"]:
            if section.get("kind") == "table":
                found += 1
                assert section.get("columns"), section["title"]
                assert section["columns"] != rt.DEFAULT_TABLE_COLUMNS
    assert found == 3, "daily, weekly and incident each have one table section"


def test_the_named_columns_are_what_reaches_the_prompt():
    for body in starters():
        prompt = rt.render_prompt(body, SCOPE, [], "x", source=rt.SOURCE_LIBRARY)
        for section in body["sections"]:
            if section.get("kind") == "table":
                assert " | ".join(section["columns"]) in prompt


# ---- the SQL itself ---------------------------------------------------------

def test_the_migration_parses_and_so_does_the_plpgsql_inside_it():
    """Two parsers, because the first one does not read the second's language.

    `parse_sql` sees `DO $$ ... $$` as a statement carrying a STRING; the
    PL/pgSQL inside is never looked at, so a loop with a typo in it parses
    perfectly and fails at deploy. `parse_plpgsql_json` is PostgreSQL's own
    PL/pgSQL parser and does read it.

    This is syntax, not semantics: no column name, no constraint and no
    idempotence claim is checked here. There is no PostgreSQL on this machine,
    so the first real execution is the TEST deploy -- which is the right place
    for it to happen and the wrong place to be surprised.
    """
    import json as _json

    from pglast import parser

    sql = io.open(MIGRATION, encoding="utf-8").read()
    parser.parse_sql(sql)
    blocks = _json.loads(parser.parse_plpgsql_json(sql))
    assert blocks, "the DO block stopped being PL/pgSQL"


# ---- what the rows say ------------------------------------------------------

def test_the_four_are_the_four_that_were_asked_for():
    got = {(slug, name, rtype) for slug, name, rtype in names()}
    assert got == {
        ("daily-report-standard", "Daily Report — Standard", "daily"),
        ("weekly-progress-report", "Weekly Progress Report", "weekly"),
        ("incident-report-standard", "Incident Report — Standard", "incident"),
        ("pm-daily-condensed", "PM Daily — Condensed", "daily"),
    }


def test_incident_is_added_to_the_report_type_check():
    """0062's CHECK predates any incident template. Seeding one without
    widening it inserts nothing and fails the deploy -- loudly, which is the
    good case, but only if somebody remembered."""
    sql = io.open(MIGRATION, encoding="utf-8").read()
    assert "report_templates_report_type_check" in sql
    assert "'incident'" in sql


def test_the_starters_are_org_scope_and_own_no_user():
    """report_templates_owner_matches_scope refuses an org row with an owner.
    The insert says NULL; this pins that it keeps saying NULL."""
    sql = io.open(MIGRATION, encoding="utf-8").read()
    assert "(p_company, 'org', NULL, starter.slug" in sql


def test_the_bodies_live_in_one_place_and_both_callers_call_it():
    """Two callers need these: this migration, for the companies that already
    exist, and lambda_org_seed, for every company made from now on.

    A copy in Python beside a copy in SQL is two copies, and the one that gets
    edited is not reliably the one that runs. So the bodies are in a function
    and both callers call it -- which is also why this file can read them out
    of the migration and know it is reading what ships."""
    sql = io.open(MIGRATION, encoding="utf-8").read()
    assert "CREATE OR REPLACE FUNCTION seed_starter_report_templates" in sql
    assert "PERFORM seed_starter_report_templates(co.id, author)" in sql

    seed = io.open(os.path.join(os.path.dirname(__file__), "..", "..", "src",
                                "lambda_org_seed.py"), encoding="utf-8").read()
    assert "seed_starter_report_templates" in seed or "seed_starters" in seed,         "a company created after this migration would get nothing"


def test_each_company_gets_its_own_copies_not_a_shared_row():
    """Per company, never shared. Every row carries its own company_id, so a
    company editing a starter cannot touch another company's -- which is the
    whole reason these are seeded per company rather than kept as one global
    template everyone points at."""
    sql = io.open(MIGRATION, encoding="utf-8").read()
    assert "p_company, 'org', NULL" in sql
    assert "rt.company_id = p_company" in sql, "and the idempotence check is per company too"


def test_a_company_with_nobody_in_it_is_skipped_rather_than_failing():
    """created_by is NOT NULL and references users(id). Seeding a company that
    has no users yet would raise, and it would raise inside a deploy."""
    sql = io.open(MIGRATION, encoding="utf-8").read()
    assert "IF p_company IS NULL OR p_author IS NULL THEN" in sql
    assert "RETURN 0;" in sql


def test_nothing_is_bound_or_activated():
    """A starter is something a company can pick. The nightly run and every
    existing report must be exactly as they were."""
    sql = io.open(MIGRATION, encoding="utf-8").read()
    assert "report_template_bindings" not in sql.split("-- WHAT THIS MIGRATION DOES NOT DO")[1] \
        or "INSERT INTO report_template_bindings" not in sql
    assert "INSERT INTO report_template_bindings" not in sql


def test_it_does_not_insert_twice_for_a_company_that_has_them():
    sql = io.open(MIGRATION, encoding="utf-8").read()
    assert "CONTINUE WHEN EXISTS" in sql
    assert "rt.archived_at IS NULL" in sql, \
        "a company that archived a starter must not have it reappear"
