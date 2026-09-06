"""Unit: NZ wall clock, both paths, against the real tz database.

Seven call sites in this repo hardcode `timedelta(hours=13)`. That is right for
NZDT and wrong for the roughly six months of NZST, when it reports tomorrow
between 11:00 and 12:00 UTC. The bug this module exists to end is a date, not a
duration: a calendar dot on a day that has not started, a range closing on a day
nobody has reached, an S3 device-date prefix for the wrong day.

The load-bearing test is `test_the_fallback_agrees_with_the_tz_database`. The
fallback runs only where zoneinfo has no data -- i.e. never, here -- so nothing
would ever notice it being wrong until the one day it is reached. Checking it
against zoneinfo every hour across a whole year is the only way it stays honest.
"""
from datetime import datetime, timedelta, timezone

import pytest

nz = pytest.importorskip("nz_time")

# Built here rather than imported at module top: on a machine with no tz
# database this module must SKIP, not error during collection --
# importorskip guards the module under test, not the yardstick these tests
# measure it against.
AKL = pytest.importorskip("zoneinfo").ZoneInfo("Pacific/Auckland")


def test_a_naive_datetime_is_read_as_utc():
    """Every stored timestamp in this system is UTC. Reading a naive one as
    local would silently shift by half a day."""
    assert nz.to_nz(datetime(2026, 1, 15, 0, 0)) == datetime(2026, 1, 15, 13, 0)


def test_nzst_is_twelve_hours_not_thirteen():
    """September 6 is NZST. The hardcoded +13 this module replaces turns 11:30
    UTC into the NEXT NZ day."""
    assert nz.to_nz(datetime(2026, 9, 6, 11, 30)) == datetime(2026, 9, 6, 23, 30)


def test_nzdt_is_thirteen_hours():
    assert nz.to_nz(datetime(2026, 1, 15, 11, 30)) == datetime(2026, 1, 16, 0, 30)


@pytest.mark.parametrize("utc,expected", [
    # DST begins 02:00 NZST on the last Sunday of September 2026 (the 27th),
    # which is 14:00 UTC on the 26th.
    (datetime(2026, 9, 26, 13, 59), 12),
    (datetime(2026, 9, 26, 14, 0), 13),
    # DST ends 03:00 NZDT on the first Sunday of April 2026 (the 5th) = 14:00
    # UTC on the 4th.
    (datetime(2026, 4, 4, 13, 59), 13),
    (datetime(2026, 4, 4, 14, 0), 12),
])
def test_the_fallback_switches_at_the_statutory_instant(utc, expected):
    assert nz._fallback_offset_hours(utc) == expected


def test_the_fallback_agrees_with_the_tz_database():
    """Hourly across a full year, including both switchovers.

    This is the only thing standing between the fallback and being quietly
    wrong: it is dead code in every environment we deploy to, so it cannot be
    caught in production -- it would simply produce the wrong day on the first
    runtime that lacks tzdata.
    """
    t = datetime(2026, 1, 1, 0, 0)
    disagreements = []
    while t < datetime(2027, 1, 1):
        truth = t.replace(tzinfo=timezone.utc).astimezone(AKL).replace(tzinfo=None)
        mine = t + timedelta(hours=nz._fallback_offset_hours(t))
        if mine != truth:
            disagreements.append((t.isoformat(), mine.isoformat(), truth.isoformat()))
        t += timedelta(hours=1)
    assert disagreements == []


def test_today_is_the_nz_day_not_the_utc_one(monkeypatch):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 6, 12, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(nz, "datetime", _Frozen)
    assert nz.nz_today().isoformat() == "2026-09-07"   # 00:30 NZST on the 7th
    assert nz.nz_now() == datetime(2026, 9, 7, 0, 30)


def test_a_non_datetime_returns_none_rather_than_raising():
    """Callers hand this rows straight out of the database, where a NULL
    timestamp is ordinary. Raising would turn a missing time into a 500."""
    assert nz.to_nz(None) is None
    assert nz.to_nz("2026-09-06") is None


# --------------------------------------------------------------------------
# The branch that never runs in production
# --------------------------------------------------------------------------

def _no_tzdata(monkeypatch):
    """Make `from zoneinfo import ZoneInfo` raise, the way a runtime with no tz
    database does. A module object without the attribute is exactly that
    failure -- ImportError, caught by the same `except` the real one hits."""
    import sys
    import types
    monkeypatch.setitem(sys.modules, "zoneinfo", types.ModuleType("zoneinfo"))


@pytest.mark.parametrize("utc", [
    datetime(2026, 1, 15, 3, 0),      # NZDT
    datetime(2026, 6, 15, 3, 0),      # NZST
    datetime(2026, 9, 26, 14, 0),     # the instant NZDT starts
    datetime(2026, 4, 4, 14, 0),      # the instant NZDT ends
])
def test_to_nz_falls_back_to_the_same_answer(monkeypatch, utc):
    truth = utc.replace(tzinfo=timezone.utc).astimezone(AKL).replace(tzinfo=None)
    _no_tzdata(monkeypatch)
    assert nz.to_nz(utc) == truth


def test_from_nz_falls_back_to_the_same_answer_all_year(monkeypatch):
    """Every hour of a year, fallback vs the tz database.

    The truth values are computed BEFORE zoneinfo is broken -- computing them
    afterwards would compare the fallback against itself, which is the shape of
    a test that cannot fail. The two ambiguous hours each April are skipped:
    ZoneInfo resolves them by `fold` and no fixed rule can agree with both
    readings at once.
    """
    t = datetime(2026, 1, 1, 0, 0)
    expected = []
    while t < datetime(2027, 1, 1):
        a = t.replace(tzinfo=AKL, fold=0).utcoffset()
        b = t.replace(tzinfo=AKL, fold=1).utcoffset()
        if a == b:
            expected.append((t, t.replace(tzinfo=AKL).astimezone(timezone.utc)
                                .replace(tzinfo=None)))
        t += timedelta(hours=1)
    assert len(expected) > 8700          # the sweep really ran

    _no_tzdata(monkeypatch)
    bad = [(local.isoformat(), nz.from_nz(local).isoformat(), truth.isoformat())
           for local, truth in expected if nz.from_nz(local) != truth]
    assert bad == []


def test_from_nz_is_the_inverse_of_to_nz():
    """The pair is used across a write/read boundary -- the device writes NZ,
    the column stores UTC -- so a mismatch between them re-plants the 2026-08-10
    bug where the offset was applied twice."""
    for utc in (datetime(2026, 1, 15, 3, 0), datetime(2026, 6, 15, 3, 0)):
        assert nz.from_nz(nz.to_nz(utc)) == utc


def test_an_aware_datetime_is_converted_not_relabelled():
    aware = datetime(2026, 6, 15, 3, 0, tzinfo=timezone.utc)
    assert nz.from_nz(aware) == datetime(2026, 6, 15, 3, 0)
