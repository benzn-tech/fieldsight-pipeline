"""Unit: the stem fallback behind find_items' first_seen_recorded_at.

A legacy (pre-session-id) session_base is the device's transcript stem,
`{device}_{YYYY-MM-DD}_{HH-MM-SS}`, named with the device's NZ wall clock. The parser
reads it as Pacific/Auckland local time and returns an aware UTC datetime, or None
when the base carries no timestamp. Expected values are written by hand: NZDT is
UTC+13, NZST UTC+12, and 2026's DST ends at 03:00 on Sunday 5 April.
"""
import datetime as _dt

import pytest

from repositories.item_history import recorded_at_from_stem

UTC = _dt.timezone.utc


@pytest.mark.parametrize("base, expected", [
    # Summer (NZDT, +13).
    ("Benl1_2026-01-15_10-00-00", _dt.datetime(2026, 1, 14, 21, 0, tzinfo=UTC)),
    # Winter (NZST, +12).
    ("Benl1_2026-06-15_10-00-00", _dt.datetime(2026, 6, 14, 22, 0, tzinfo=UTC)),
    # Either side of the April 2026 switch: same wall clock, an hour apart in UTC.
    ("Dev2_2026-04-04_10-00-00", _dt.datetime(2026, 4, 3, 21, 0, tzinfo=UTC)),
    ("Dev2_2026-04-06_10-00-00", _dt.datetime(2026, 4, 5, 22, 0, tzinfo=UTC)),
    # The hyphen-only device variant.
    ("Benl1_2025-12-17-20-30-46", _dt.datetime(2025, 12, 17, 7, 30, 46, tzinfo=UTC)),
    # A trailing source token after the timestamp does not change the start.
    ("Benl1_2026-03-20_12-18-34_srcwav", _dt.datetime(2026, 3, 19, 23, 18, 34, tzinfo=UTC)),
])
def test_stem_parsed_as_nz_local_and_returned_as_utc(base, expected):
    got = recorded_at_from_stem(base)
    assert got == expected
    assert got.tzinfo is not None and got.utcoffset() == _dt.timedelta(0)


@pytest.mark.parametrize("base", [
    None, "", "sid" + "a" * 32, "grp" + "b" * 12, "notes", "Benl1_2026-13-40_25-99-99",
])
def test_no_timestamp_is_none(base):
    assert recorded_at_from_stem(base) is None
