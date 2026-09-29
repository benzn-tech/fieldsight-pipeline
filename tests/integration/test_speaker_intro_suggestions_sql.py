"""Integration: `speaker_intro_suggestions` (0071), against a database that parses SQL.

A connection double never enforces a UNIQUE constraint, a CHECK, or a foreign key -- see
CLAUDE.md's "Testing" section. These are the two facts a double cannot see: `ON CONFLICT
DO NOTHING` genuinely dedupes the second insert of the same cluster, and `state` really is
constrained to the three values the state machine defines.
"""
import uuid

import pytest


def _company(db):
    row = db.execute("SELECT id FROM companies LIMIT 1").fetchone()
    if not row:
        pytest.skip("no company in this database to hang a suggestion off")
    return row[0]


def _insert(db, company_id, source_filename="a_2026-09-29_09-00-00_sid" + "a" * 32 + ".json",
           speaker_label="spk_0", session_base="sid" + "a" * 32):
    return db.execute(
        "INSERT INTO speaker_intro_suggestions "
        "(company_id, session_base, source_filename, speaker_label, user_folder, "
        " session_date, start_sec, end_sec, heard_name, company_name, quote) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (company_id, session_base, source_filename, speaker_label) "
        "DO NOTHING RETURNING id",
        (company_id, session_base, source_filename, speaker_label, "Ben1",
         "2026-09-29", 0.0, 6.0, "Petros", "Cassidy", "Hi, this is Petros from Cassidy"),
    ).fetchone()


def test_the_same_cluster_inserted_twice_is_deduped_by_the_constraint(db):
    company_id = _company(db)
    first = _insert(db, company_id)
    assert first is not None, "the first insert of a new cluster must return a row"
    second = _insert(db, company_id)
    assert second is None, "ON CONFLICT DO NOTHING must swallow the second insert silently"


def test_state_is_constrained_to_the_three_values(db):
    company_id = _company(db)
    row = _insert(db, company_id, source_filename="b_2026-09-29_09-00-00_sid" + "b" * 32 + ".json",
                  session_base="sid" + "b" * 32)
    assert row is not None
    with pytest.raises(Exception):
        db.execute("UPDATE speaker_intro_suggestions SET state = 'maybe' WHERE id = %s",
                  (row[0],))
    db.rollback()


def test_decided_by_must_reference_a_real_user(db):
    company_id = _company(db)
    row = _insert(db, company_id, source_filename="c_2026-09-29_09-00-00_sid" + "c" * 32 + ".json",
                  session_base="sid" + "c" * 32)
    assert row is not None
    with pytest.raises(Exception):
        db.execute("UPDATE speaker_intro_suggestions SET decided_by = %s WHERE id = %s",
                  (str(uuid.uuid4()), row[0]))
    db.rollback()
