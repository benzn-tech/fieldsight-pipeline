"""Unit: item_history.session_recorded_at -- this session's recording start, by the same
rule find_items uses for a candidate's first_seen_recorded_at."""
import datetime as _dt

from repositories import item_history

UTC = _dt.timezone.utc
SID = "a" * 32


class _Cur:
    def __init__(self, row, seen):
        self.row, self.seen = row, seen

    def execute(self, sql, params):
        self.seen.append((sql, params))

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row=None):
        self.row, self.seen = row, []

    def cursor(self, row_factory=None):
        return _Cur(self.row, self.seen)


def test_sid_session_reads_opened_at_as_utc():
    nz = _dt.timezone(_dt.timedelta(hours=13))
    conn = _Conn({"opened_at": _dt.datetime(2026, 11, 11, 17, 54, 13, tzinfo=nz)})
    got = item_history.session_recorded_at(
        conn, f"extractions/Synth_Folder/2026-11-11/sid{SID}.json")
    assert got == _dt.datetime(2026, 11, 11, 4, 54, 13, tzinfo=UTC)
    assert conn.seen and conn.seen[0][1] == (SID,)


def test_sid_without_a_session_row_is_none():
    assert item_history.session_recorded_at(
        _Conn(None), f"extractions/Synth_Folder/2026-11-11/sid{SID}.json") is None


def test_legacy_stem_uses_nz_wall_clock_without_a_query():
    conn = _Conn()
    got = item_history.session_recorded_at(
        conn, "extractions/Synth_Folder/2026-01-15/Benl1_2026-01-15_10-00-00.json")
    assert got == _dt.datetime(2026, 1, 14, 21, 0, tzinfo=UTC)
    assert conn.seen == []


def test_group_and_foreign_keys_are_none():
    conn = _Conn({"opened_at": _dt.datetime(2026, 11, 11, tzinfo=UTC)})
    assert item_history.session_recorded_at(
        conn, "extractions/Synth_Folder/2026-11-11/grpabcdef123456.json") is None
    assert item_history.session_recorded_at(conn, "reports/x.json") is None
    assert item_history.session_recorded_at(conn, None) is None
