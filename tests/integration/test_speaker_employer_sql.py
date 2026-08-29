"""Integration: the employer columns, against a database that parses the SQL.

The unit suite drives a connection double. A double records statements without preparing
them, and this exact INSERT has already produced a production 500 that way: a parameter whose
only use is `IS NULL` gives Postgres nothing to infer a type from, so `CASE WHEN %s IS NULL`
fails at PREPARE time for *every* value — `IndeterminateDatatype` — and naming any speaker
without an existing profile became a deterministic error the suite could not see.

`employer_set_at` adds a third such CASE. So does the pairing CHECK, which no double enforces.
These tests exist because both of those are invisible from Python.
"""
import uuid

import pytest


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def test_the_employer_columns_exist_with_the_shapes_the_writer_assumes(db):
    cols = {r[0]: r[1] for r in db.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'speaker_voiceprints' AND column_name LIKE 'employer%'"
    ).fetchall()}
    assert cols.get("employer_name") == "text"
    assert cols.get("employer_source") == "text"
    assert cols.get("employer_set_by") == "uuid"
    assert "timestamp" in (cols.get("employer_set_at") or "")


def test_a_real_insert_prepares_and_runs(db):
    """The whole point of this file. `upsert_profile` builds one statement with three
    `CASE WHEN %s IS NULL` parameters; a double accepts any of them and Postgres accepts only
    the cast ones."""
    from repositories import voiceprints as vp

    company = _one(db, "SELECT id FROM companies LIMIT 1")
    if not company:
        pytest.skip("no company in this database to hang a profile off")

    name = f"Employer Test {uuid.uuid4().hex[:8]}"
    row = vp.upsert_profile(db, str(company[0]), display_name=name,
                            consent_given=True, consented_by=None,
                            consent_basis="attestation", asserted_by=str(uuid.uuid4()),
                            employer_name="ABC Ltd", employer_source="typed")
    assert row and row.get("id")

    stored = _one(db, "SELECT employer_name, employer_source, employer_set_at "
                      "FROM speaker_voiceprints WHERE id = %s", (row["id"],))
    assert stored[0] == "ABC Ltd"
    assert stored[1] == "typed"
    assert stored[2] is not None, (
        "employer_set_at stayed NULL: the CASE that fills it is reading the wrong parameter, "
        "and every row would claim an employer nobody can date")


def test_the_pairing_check_is_enforced_by_the_database(db):
    """Not only by the repository. `speaker_voiceprints` has three writers — org-api's upsert,
    the voiceprint writer, and the Sign On Site adapter to come — and a rule enforced in one
    caller is a rule until somebody adds a second."""
    import psycopg

    company = _one(db, "SELECT id FROM companies LIMIT 1")
    if not company:
        pytest.skip("no company in this database to hang a profile off")

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO speaker_voiceprints (company_id, display_name, status, "
            "employer_name) VALUES (%s, %s, 'tentative', %s)",
            (str(company[0]), f"Unpaired {uuid.uuid4().hex[:8]}", "ABC Ltd"))


def test_an_unknown_source_is_rejected_by_the_database(db):
    import psycopg

    company = _one(db, "SELECT id FROM companies LIMIT 1")
    if not company:
        pytest.skip("no company in this database to hang a profile off")

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO speaker_voiceprints (company_id, display_name, status, "
            "employer_name, employer_source) VALUES (%s, %s, 'tentative', %s, %s)",
            (str(company[0]), f"Bad source {uuid.uuid4().hex[:8]}", "ABC Ltd", "guessed"))


def test_the_lookup_index_exists(db):
    """Task 3 asks "has anyone here already said who Andy M works for" on every rename. Without
    the partial index that is a sequential scan of every profile in the company, on a keystroke
    path."""
    idx = _one(db, "SELECT indexdef FROM pg_indexes "
                   "WHERE indexname = 'speaker_voiceprints_employer_lookup'")
    assert idx, "the employer lookup index is missing"
    assert "employer_name IS NOT NULL" in idx[0], (
        "the index is no longer partial; it now covers the rows that answer nothing, which "
        "is most of them")
