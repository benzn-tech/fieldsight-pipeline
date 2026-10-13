"""The same window of the same recording is stored once, however many times it arrives.

On TEST (2026-09-27) six windows of one profile were each held twice with identical
vectors: a passage renamed twice, a cluster propagated twice, and a harvested window later
renamed on its own. Profiles match on the MEAN of their samples, so each of those counted
double.

The fix is a partial unique index (migration 0069) plus an upsert in `add_sample`. The
double below never parses SQL, so these tests pin the TEXT: the conflict target must
repeat the index's predicate exactly, or Postgres refuses to infer the index and every
enrolment fails -- which no fake connection would notice.
"""
import re
from pathlib import Path

from repositories import voiceprints

CO = "11111111-1111-1111-1111-111111111111"
VP = "22222222-2222-2222-2222-222222222222"
MIGRATIONS = Path(__file__).resolve().parents[2] / "src" / "migrations"
#: The migration that owns the CURRENT index. 0069 created an exact-window index; 0070
#: replaced it with one on the window rounded to the second, because a passage renamed
#: twice gives windows a few hundredths apart (7.22 vs 7.26 s on TEST).
MIGRATION = MIGRATIONS / "0070_one_sample_per_rounded_window.sql"


def _flat(s):
    return " ".join(s.split())


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.sqls.append(_flat(sql))
        return self

    def fetchone(self):
        return {"status": "tentative", "id": "s1"}

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.sqls = []

    def cursor(self, row_factory=None):
        return _Cur(self)


def _insert_sql():
    conn = _Conn()
    voiceprints.add_sample(conn, CO, VP, [0.1] * 192, source="correction", s3_key="k",
                           window=(1.0, 11.0))
    [sql] = [s for s in conn.sqls if s.startswith("INSERT INTO speaker_voiceprint_samples")]
    return sql


def _index_sql():
    text = "\n".join(line.split("--")[0] for line in MIGRATION.read_text().splitlines())
    m = re.search(r"CREATE UNIQUE INDEX[^;]*;", text)
    assert m, "0069 no longer creates the unique index"
    return _flat(m.group(0))


def test_a_repeat_window_updates_instead_of_inserting_a_second_copy():
    assert "ON CONFLICT" in _insert_sql()


def test_the_conflict_target_is_the_index_that_0069_creates():
    index = _index_sql()
    # Between the table name and WHERE; the columns now contain round(...) themselves.
    cols = re.search(r"ON speaker_voiceprint_samples \((.*)\) WHERE", index).group(1)
    predicate = index.split(" WHERE ", 1)[1].rstrip(";").strip()
    assert f"ON CONFLICT ({cols}) WHERE {predicate} DO UPDATE" in _insert_sql()


def test_a_human_assertion_outranks_a_propagation_of_the_same_window():
    # `source` is what separates "a person said so" from "the clustering suggested it" when
    # a bad batch is deleted. A repeat may promote a propagation, never demote a correction.
    sql = _insert_sql()
    for col in ("source", "created_by", "correction_ref"):
        assert (f"{col} = CASE WHEN EXCLUDED.source = 'correction' "
                f"AND speaker_voiceprint_samples.source <> 'correction' "
                f"THEN EXCLUDED.{col} ELSE speaker_voiceprint_samples.{col} END") in sql


def test_the_collapse_keeps_the_correction_copy():
    text = _flat(MIGRATION.read_text())
    assert "ORDER BY (source = 'correction') DESC" in text


def test_windows_a_few_hundredths_apart_are_one_sample():
    """The case 0069 missed: 7.22-17.22 s and 7.26-17.26 s of one recording."""
    index = _index_sql()
    assert "round(window_start_s), round(window_end_s)" in index
    assert "ON CONFLICT (voiceprint_id, s3_key, round(window_start_s), "            "round(window_end_s))" in _insert_sql()


def test_a_repeat_window_does_not_overwrite_recording_conditions_with_null():
    """Design 2026-09-30 step 1: an older producer that never computed level_dbfs/noise_dbfs/
    snr_db must not blank out values a newer producer already stored for the same window --
    COALESCE keeps whichever side has a value, unlike `source`/`created_by`/`correction_ref`
    above, which only upgrade in one direction (propagation -> correction)."""
    sql = _insert_sql()
    for col in ("level_dbfs", "noise_dbfs", "snr_db"):
        assert col in sql, f"{col} is missing from the INSERT"
        assert f"{col} = COALESCE(EXCLUDED.{col}, speaker_voiceprint_samples.{col})" in sql


def test_the_exact_window_index_is_dropped():
    """Two unique indexes would each have to be satisfied; the exact one would not be
    inferred by the conflict target and would raise instead of updating."""
    text = _flat(MIGRATION.read_text())
    assert "DROP INDEX IF EXISTS speaker_voiceprint_samples_one_per_window" in text
