"""New Zealand wall-clock time, in one place.

Every "today" in this system is a NEW ZEALAND day: the device names its
transcript keys with local time, the customer asks about "yesterday" in local
time, and a UTC date is the wrong day for the first eleven-to-thirteen hours of
every NZ morning (BUG-19/BUG-37, and the finalize "No summary" bug).

The offset is NOT constant. NZ is UTC+13 (NZDT) from the last Sunday of
September to the first Sunday of April, and UTC+12 (NZST) the rest of the year
-- about half the year each. A fixed +13 therefore runs an hour ahead for six
months, and between 11:00 and 12:00 UTC reports tomorrow's NZ date while it is
still today in Auckland. The direction is "a day starts early", which shows up
as a calendar offering a day that has not begun and a range query that closes on
a date nobody has reached.

The census when this module was written was ELEVEN sites, not the seven an
earlier draft of this comment claimed -- a reviewer counted:

    6 now call this module: lambda_org_api x4 (the calendar window, today, the
      report_date default, a programme updated_at), plus get_nzdt_now in
      lambda_meeting_minutes and lambda_report_generator.
    3 left in lambda_fieldsight_api: that gateway's lambda measured ZERO
      invocations in fourteen days while the live one served 78-509 a day.
    2 left in lambda_orchestrator (TIMEZONE_OFFSET = -780 and
      TIME_DIFFERENCE_MS = 46800000): those go out on the RealPTT wire, so
      correcting them changes an external contract and needs a verified
      before/after against the live API. Its own docstrings carry the detail.

Anyone extending this: count again before claiming the sweep is complete.

`ZoneInfo` is exact and is what runs; the arithmetic fallback exists only for a
runtime with no tz database, and it implements the same statutory rule rather
than pretending the offset is fixed. Both paths are covered by tests, because a
fallback nobody drives is a fallback that is wrong when it is finally reached.
"""
import calendar
from datetime import datetime, timedelta, timezone

TZ_NAME = "Pacific/Auckland"


def _switch_dates(year):
    """(NZDT starts, NZDT ends) as LOCAL midnights: the last Sunday of
    September and the first Sunday of April. One definition -- the forward and
    reverse conversions both read it, because a module whose whole point is
    "the rule lives in one place" should not hold two copies of the rule."""
    last_sep = datetime(year, 9, calendar.monthrange(year, 9)[1])
    last_sep -= timedelta(days=(last_sep.weekday() - 6) % 7)
    first_apr = datetime(year, 4, 1)
    first_apr += timedelta(days=(6 - first_apr.weekday()) % 7)
    return last_sep, first_apr


def _fallback_offset_hours(utc_naive):
    """NZDT (+13) from the last Sunday of September 02:00 NZST to the first
    Sunday of April 03:00 NZDT; NZST (+12) otherwise. Both boundaries are
    converted to UTC using the offset in force BEFORE the switch, which is why
    the two subtractions differ."""
    last_sep, first_apr = _switch_dates(utc_naive.year)
    dst_start = last_sep + timedelta(hours=2) - timedelta(hours=12)
    dst_end = first_apr + timedelta(hours=3) - timedelta(hours=13)
    return 13 if (utc_naive >= dst_start or utc_naive < dst_end) else 12


def to_nz(dt):
    """`dt` as NZ local time, naive. A naive `dt` is read as UTC, which is what
    every stored timestamp in this system is."""
    if not hasattr(dt, "strftime"):
        return None
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    try:
        from zoneinfo import ZoneInfo
        return aware.astimezone(ZoneInfo(TZ_NAME)).replace(tzinfo=None)
    except Exception:                                     # noqa: BLE001
        u = aware.astimezone(timezone.utc).replace(tzinfo=None)
        return u + timedelta(hours=_fallback_offset_hours(u))


def nz_now():
    """Now, as NZ local wall clock (naive)."""
    return to_nz(datetime.now(timezone.utc))


def nz_today():
    """Today's date in NZ. The only correct answer to "what day is it" for a
    customer-facing range, a calendar dot, or a device-date S3 prefix."""
    return nz_now().date()


def from_nz(dt):
    """The inverse of `to_nz`: a Pacific/Auckland LOCAL (naive) datetime -> UTC
    (naive). An already-aware `dt` is simply converted.

    The device names its chunk files with its own wall clock while
    `meeting_session.opened_at` is a UTC column, and a writer that stores the
    wall clock raw plants a value every reader then shifts AGAIN -- prod
    2026-08-10 dated a confirmation email TOMORROW and re-gathered an empty S3
    prefix that way.

    The boundaries here are LOCAL, not UTC, which is why they differ from
    `_fallback_offset_hours`.

    The two paths disagree by an hour on exactly two hours of the year, and
    only on a runtime with no tz database: the AMBIGUOUS hour each April (this
    takes the earlier, NZDT reading, matching ZoneInfo's `fold=0` default) and
    the NONEXISTENT hour each September, where a local time that never occurred
    is read as NZDT here and as NZST by ZoneInfo. Both are an hour's error in a
    window that cannot contain a real recording, against the twelve-hour error
    this function exists to remove.
    """
    if not hasattr(dt, "strftime"):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        from zoneinfo import ZoneInfo
        return dt.replace(tzinfo=ZoneInfo(TZ_NAME)).astimezone(
            timezone.utc).replace(tzinfo=None)
    except Exception:                                     # noqa: BLE001
        last_sep, first_apr = _switch_dates(dt.year)
        dst_start = last_sep + timedelta(hours=2)
        dst_end = first_apr + timedelta(hours=3)
        return dt - timedelta(hours=13 if (dt >= dst_start or dt < dst_end) else 12)
