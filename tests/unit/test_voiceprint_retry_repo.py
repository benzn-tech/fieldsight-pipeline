"""The three queries behind "voice not saved -- try again". Assert the SQL text.

A connection double never executes SQL (CLAUDE.md "Testing"), so these pin what the
statements say, not what Postgres does with them. The statements are also meant to be run
against a real database before release.
"""
import pytest

voiceprints = pytest.importorskip("repositories.voiceprints", reason="requires psycopg")
recordings = pytest.importorskip("repositories.recordings", reason="requires psycopg")

CO = "11111111-1111-1111-1111-111111111111"
VP = "22222222-2222-2222-2222-222222222222"
SID = "sid9db9293e82b94a4d9611572b1233f82d"


class Conn:
    def __init__(self, *results):
        self.calls = []
        self._results = list(results)

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return self._results.pop(0) if self._results else None

    def fetchall(self):
        return self._results.pop(0) if self._results else []


def test_latest_human_correction_is_a_live_correction_for_that_profile_or_its_name():
    conn = Conn({"session_base": SID, "turn_ref": "x_srcwav@12.5"})
    got = voiceprints.latest_human_correction(conn, CO, VP, "Ben Lin")
    sql, params = conn.calls[0]
    assert "FROM speaker_turn_names" in sql
    assert "source = 'correction'" in sql and "superseded_at IS NULL" in sql
    assert "company_id = %s" in sql
    # Matched by id, or -- for a correction that created no profile -- by the name alone.
    assert "(voiceprint_id = %s OR (voiceprint_id IS NULL AND display_name = %s))" in sql
    assert "ORDER BY created_at DESC LIMIT 1" in sql
    assert params == (CO, VP, "Ben Lin")
    assert got == {"session_base": SID, "turn_ref": "x_srcwav@12.5"}


def test_latest_human_correction_is_none_when_there_is_no_row():
    assert voiceprints.latest_human_correction(Conn(), CO, VP, "Ben Lin") is None


def test_refused_recently_count_is_one_company_scoped_live_profile_query():
    conn = Conn({"n": 3})
    assert voiceprints.refused_recently_count(conn, CO) == 3
    sql, params = conn.calls[0]
    assert "FROM speaker_voiceprints" in sql
    assert "company_id = %s" in sql
    assert "status <> 'withdrawn'" in sql
    assert "last_attempt_outcome = 'refused'" in sql
    assert "last_attempt_at > now() - make_interval(days => %s)" in sql
    assert params == (CO, 7)
    assert len(conn.calls) == 1


def test_refused_recently_count_needs_a_company():
    with pytest.raises(ValueError):
        voiceprints.refused_recently_count(Conn(), "")


def test_locate_session_reads_folder_and_date_off_the_recordings_key():
    key = f"users/Ben_UCPK/audio/2026-09-30/Benl1_2026-09-30_11-49-00_{SID}_c0000.wav"
    conn = Conn({"s3_key": key})
    assert recordings.locate_session(conn, CO, SID) == ("Ben_UCPK", "2026-09-30")
    sql, params = conn.calls[0]
    assert "FROM recordings" in sql and "company_id = %s" in sql
    assert "s3_key LIKE %s ESCAPE" in sql
    assert params[0] == CO and SID in params[1]


def test_locate_session_falls_back_to_proposals_then_suggestions():
    conn = Conn(None, {"user_folder": "Ben_UCPK", "session_date": "2026-09-30"})
    assert recordings.locate_session(conn, CO, SID) == ("Ben_UCPK", "2026-09-30")
    sql, params = conn.calls[1]
    assert "speaker_name_proposals" in sql and "speaker_intro_suggestions" in sql
    assert "company_id = %s" in sql and "session_base = %s" in sql


def test_locate_session_is_none_when_nothing_knows_the_session():
    assert recordings.locate_session(Conn(None, None), CO, SID) is None


def test_locate_session_refuses_a_non_session_id():
    # The id goes into a LIKE pattern; only the canonical sid<32 hex> may.
    assert recordings.locate_session(Conn(), CO, "%") is None
