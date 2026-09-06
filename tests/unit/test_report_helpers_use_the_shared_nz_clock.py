"""Unit: the two report generators ask nz_time for "now", not a fixed +13.

Both helpers are named `get_nzdt_now`, and the name is the bug: NZ is on NZDT
for about half the year and NZST the rest. The old bodies added a constant
thirteen hours, so every date and time a report printed was an hour ahead for
six months -- and between 11:00 and 12:00 UTC, a DAY ahead.

Driven, not grepped. Asserting the absence of a literal would pass the moment
someone rewrote the same mistake as `timedelta(minutes=780)`; freezing the clock
and reading the answer would not.
"""
from datetime import datetime, timezone

import pytest

mm = pytest.importorskip("lambda_meeting_minutes")
rg = pytest.importorskip("lambda_report_generator")


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        # NZST. +13 would make this 00:15 on the 7th -- the wrong DAY, which is
        # what a daily report's date is derived from.
        return datetime(2026, 9, 6, 11, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize("mod", [mm, rg], ids=["meeting_minutes", "report_generator"])
def test_now_is_nzst_in_september_not_nzdt(monkeypatch, mod):
    monkeypatch.setattr(mod.nz_time, "datetime", _Frozen)
    assert mod.get_nzdt_now() == datetime(2026, 9, 6, 23, 15)


def test_yesterday_is_derived_from_that_same_clock(monkeypatch):
    """`get_yesterday_date` is what decides which day the daily report covers,
    so a clock an hour fast is a report for the wrong day."""
    monkeypatch.setattr(rg.nz_time, "datetime", _Frozen)
    assert rg.get_yesterday_date() == "2026-09-05"
