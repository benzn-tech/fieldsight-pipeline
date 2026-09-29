"""Assertions on `speaker_intro_suggestions`'s SQL TEXT -- mirrors
`test_candidate_sql_is_shaped_the_way_postgres_needs.py`: a `FakeConn` double records
statements and never parses them, so the Postgres-specific shape of the "already named"
exclusion is invisible to a behavioural test and has to be read out of the source instead.
"""
import inspect
import re

import pytest

sis = pytest.importorskip("repositories.speaker_intro_suggestions",
                          reason="requires psycopg (installed in CI)")


def _source_text():
    return re.sub(r"\s+", " ", inspect.getsource(sis))


def test_the_already_named_exclusion_uses_split_part_and_superseded_at():
    src = _source_text()
    assert "split_part(n.turn_ref, '@', 1)" in src
    assert "regexp_replace(%s, '[.]json$', '')" in src
    assert "n.superseded_at IS NULL" in src


def test_store_requires_company_id():
    with pytest.raises(ValueError):
        sis.store(conn=None, company_id=None, session_base="sid" + "a" * 32,
                 user_folder="Ben1", session_date="2026-09-29", intros=[])


def test_pending_requires_company_id():
    with pytest.raises(ValueError):
        sis.pending(conn=None, company_id=None)


def test_pending_count_requires_company_id():
    with pytest.raises(ValueError):
        sis.pending_count(conn=None, company_id=None)


def test_decide_requires_company_id():
    with pytest.raises(ValueError):
        sis.decide(conn=None, company_id=None, suggestion_id="x", state="confirmed")


def test_decide_rejects_a_state_other_than_confirmed_or_rejected():
    with pytest.raises(ValueError):
        sis.decide(conn=object(), company_id="c", suggestion_id="x", state="dismissed")
