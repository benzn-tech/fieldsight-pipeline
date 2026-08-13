"""Unit: migration 0040 — what a turn's name came from, and whether it still stands.

Read as text, like the other migration tests: there is no database here, and the failures
this guards against are things being ABSENT, which text can see.

Numbering is part of the contract. 0038's header reserves 0039 for the migration that drops
its tables, so taking that number would quietly remove a recorded rollback path.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIG = os.path.join(REPO, "src", "migrations", "0040_turn_name_provenance.sql")


def _sql():
    return open(MIG, encoding="utf-8").read()


def test_0039_is_left_alone_for_0038s_revert():
    d = os.path.join(REPO, "src", "migrations")
    assert not [f for f in os.listdir(d) if f.startswith("0039")], (
        "0038 reserves 0039 as its drop-revert; taking the number removes a rollback path "
        "that was recorded so it would not have to be invented under pressure")


def test_the_profile_id_becomes_nullable():
    """A correction that creates no profile has nothing to point at, and inventing a named
    profile to satisfy the foreign key is the consent violation Phase 4 refuses."""
    assert re.search(r"ALTER COLUMN voiceprint_id DROP NOT NULL", _sql())


def test_the_source_column_exists_because_the_count_depends_on_it():
    """Without it, `confirmations_count` counts machine-propagated names as independent
    human confirmations and the system promotes profiles from its own output."""
    assert "ADD COLUMN IF NOT EXISTS source text" in _sql()


def test_supersession_is_marked_not_deleted():
    s = _sql()
    assert "superseded_at timestamptz" in s
    assert "DELETE FROM" not in s.upper(), (
        "the audit of a correction includes what it undid, so rows are marked, not removed")


def test_one_live_row_per_turn_is_enforced_in_the_database():
    s = _sql()
    assert "CREATE UNIQUE INDEX" in s
    assert "WHERE superseded_at IS NULL" in s, (
        "a plain unique index would collide with every superseded row ever written")


def test_everything_is_idempotent():
    """Migrations re-run on every deploy in this repo."""
    s = _sql()
    for stmt in re.findall(r"^\s*(ADD COLUMN|CREATE (?:UNIQUE )?INDEX)", s, re.M):
        pass
    adds = re.findall(r"ADD COLUMN(?! IF NOT EXISTS)", s)
    idx = re.findall(r"CREATE (?:UNIQUE )?INDEX(?! IF NOT EXISTS)", s)
    assert not adds and not idx, f"non-idempotent statements: {adds + idx}"


def test_the_threshold_that_produced_a_row_is_recorded_with_it():
    """Frozen at 0.85 and expected never to change — the column is what disqualifies stale
    rows from calibration automatically if it ever does."""
    assert "cluster_threshold double precision" in _sql()


# ---- 0041 ---------------------------------------------------------------


def test_a_named_turn_has_somewhere_to_put_the_name():
    """0038 read the name from the profile; 0040 allowed rows with no profile. Together they
    left the name homeless, and the chain carried it all the way to a writer that dropped it
    without a word. A real correction on TEST produced a row that named nobody."""
    path = os.path.join(REPO, "src", "migrations", "0041_turn_name_display.sql")
    assert os.path.exists(path), "no migration adds display_name"
    sql = open(path, encoding="utf-8").read()
    assert "ADD COLUMN IF NOT EXISTS display_name" in sql
    assert "NOT NULL" not in sql, (
        "an unnamed cluster is a real answer and must stay distinguishable from a named one")
