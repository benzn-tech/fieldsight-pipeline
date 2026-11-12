"""Integration: migration 0103 -- who answered a decision card (decision card inbox spec rev 2 §3).

Adds four nullable columns to decision_cards with no default and no backfill; the file is
safe to run twice. Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import os

import pytest

pytestmark = pytest.mark.integration

MIGRATION_0103 = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                              "0103_decision_card_answers.sql")

_COLS = {"answered_by": "uuid", "answered_option": "text",
         "answered_at": "timestamp with time zone", "answer_reason": "text"}


def test_0103_applies_twice_and_columns_nullable(db):
    with open(MIGRATION_0103, encoding="utf-8") as fh:
        sql = fh.read()
    db.execute(sql)
    db.execute(sql)
    rows = db.execute(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name='decision_cards' AND column_name = ANY(%s)", (list(_COLS),)).fetchall()
    got = {r[0]: r[1:] for r in rows}
    assert set(got) == set(_COLS)
    for col, typ in _COLS.items():
        assert got[col] == (typ, "YES", None), col
    # answered_by references users(id)
    fk = db.execute(
        "SELECT count(*) FROM pg_constraint c JOIN pg_attribute a "
        "ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "WHERE c.conrelid='decision_cards'::regclass AND c.contype='f' "
        "AND c.confrelid='users'::regclass AND a.attname='answered_by'").fetchone()[0]
    assert fk == 1
