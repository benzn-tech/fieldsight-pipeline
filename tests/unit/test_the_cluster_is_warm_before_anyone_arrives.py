"""The cluster is awake before the first person of the week arrives.

Once `SWEEP_REQUIRE_PENDING` is on in prod, Aurora sleeps at night and at
weekends. A resume after a long pause can take longer than API Gateway's 29 s
ceiling, so the first request of a quiet Monday can fail while nothing is
wrong. One unconditional connection on a weekday morning moves that resume to a
time when nobody is looking at it.

WHY THIS IS A WALL-CLOCK TEST INSIDE THE HANDLER, NOT A SECOND EVENTBRIDGE RULE

A `rate()` rule's phase is fixed by whenever it happened to be created, and the
two stacks deploy separately. Two unaligned unconditional wakes against ONE
shared cluster halve the effective idle window -- which is precisely how the
first version of the scale-to-zero work was set to save exactly nothing (a
15-minute safety sweep against a 900 s auto-pause). A minute-of-the-wall-clock
aligns the stages by construction.

WHY NZ LOCAL TIME AND NOT A UTC HOUR

New Zealand is UTC+12 for half the year and UTC+13 for the other half. A fixed
UTC cron would fire at 06:45 in one season and 05:45 or 07:45 in the other. The
January and July cases below are the whole point of this file; delete them and
a daylight-saving bug ships silently, because nothing else in the repo would
notice until a Monday in April.
"""
import datetime as dt

import pytest

fc = pytest.importorskip("lambda_finalize_claim", reason="requires psycopg (installed in CI)")


def utc(y, m, d, hh, mm):
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc)


# --- the two offsets ------------------------------------------------------

def test_it_fires_at_0645_local_in_daylight_saving():
    """January: NZDT, UTC+13. 06:45 local is 17:45 UTC the PREVIOUS day."""
    assert fc._is_prewarm_minute(utc(2027, 1, 17, 17, 45)) is True   # Mon 06:45 NZDT


def test_it_fires_at_0645_local_in_standard_time():
    """July: NZST, UTC+12. 06:45 local is 18:45 UTC the previous day."""
    assert fc._is_prewarm_minute(utc(2026, 7, 19, 18, 45)) is True   # Mon 06:45 NZST


def test_a_fixed_utc_hour_would_have_been_wrong_in_one_of_them():
    """The bug this file exists to prevent, stated as an assertion: the two
    correct instants above are one hour apart in UTC, so no single UTC hour can
    serve both seasons."""
    summer = utc(2027, 1, 17, 17, 45)
    winter = utc(2026, 7, 19, 18, 45)
    assert fc._is_prewarm_minute(summer) and fc._is_prewarm_minute(winter)
    assert summer.hour != winter.hour


# --- the boundaries -------------------------------------------------------

def test_the_window_is_two_minutes_wide():
    """`rate(1 minute)` is "about every 60 seconds", not a wall-clock alignment.
    Ticks drift, so one at :44:59 followed by one at :46:01 would skip minute 45
    entirely -- and a missed pre-warm IS the Monday-morning failure this exists
    to prevent. The second minute costs nothing: two connections 60 seconds
    apart wake the cluster once."""
    assert fc._is_prewarm_minute(utc(2026, 7, 19, 18, 45)) is True
    assert fc._is_prewarm_minute(utc(2026, 7, 19, 18, 46)) is True


def test_the_window_does_not_creep():
    assert fc._is_prewarm_minute(utc(2026, 7, 19, 18, 44)) is False
    assert fc._is_prewarm_minute(utc(2026, 7, 19, 18, 47)) is False


def test_the_window_stays_far_from_an_hour():
    """Two adjacent minutes once a day is nothing next to the 600s auto-pause
    window. A window that crept wide enough to span the threshold would stop the
    cluster pausing at all, silently, exactly as the 15-minute safety sweep
    did."""
    assert len(fc.PREWARM_NZ_MINUTES) * 60 < 600


def test_the_same_local_time_on_saturday_and_sunday_does_not_fire():
    """A weekend resume costs a wake nobody needed. Anyone working then meets
    the same slow first request they would have met anyway."""
    # July (NZST): 18:45 UTC Fri = Sat 06:45 NZ; Sat = Sun 06:45 NZ.
    assert fc._is_prewarm_minute(utc(2026, 7, 24, 18, 45)) is False  # Sat local
    assert fc._is_prewarm_minute(utc(2026, 7, 25, 18, 45)) is False  # Sun local


def test_every_weekday_fires():
    """Sunday 18:45 UTC is Monday local, and Thursday 18:45 UTC is Friday
    local -- so this also pins that the weekday is read in NZ, not in UTC."""
    fired = [d for d in range(19, 26)
             if fc._is_prewarm_minute(utc(2026, 7, d, 18, 45))]
    # 2026-07-19 is a Sunday in UTC -> Monday 06:45 NZ.
    assert fired == [19, 20, 21, 22, 23], (
        "expected Mon-Fri NZ, which is Sun-Thu in UTC at this offset")


def test_the_utc_weekday_is_not_what_is_checked():
    """Sunday in UTC is a Monday in NZ at 06:45, and this must fire. If someone
    'simplifies' the function to read now.weekday(), this is the test that
    goes red rather than a Monday outage that nobody attributes."""
    sunday_utc = utc(2026, 7, 19, 18, 45)
    assert sunday_utc.weekday() == 6, "fixture drifted; pick a real Sunday"
    assert fc._is_prewarm_minute(sunday_utc) is True


# --- how it reaches the handler ------------------------------------------

def test_the_prewarm_minute_forces_a_connection_even_when_the_flag_is_idle(monkeypatch):
    """The whole point: the flag says there is no work -- and the sweep
    connects anyway, because warming the cluster IS the work."""
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", True)
    monkeypatch.setattr(fc.sweep_state, "is_pending", lambda *_a, **_k: False)

    assert fc._is_safety_minute(utc(2026, 7, 19, 18, 45)) is False, (
        "fixture must not land on the hourly safety minute, or this proves nothing")
    assert fc._is_prewarm_minute(utc(2026, 7, 19, 18, 45)) is True


def test_it_is_not_the_safety_minute_in_disguise():
    """SAFETY_SWEEP_MINUTE is 7; the pre-warm is :45. If they were ever set to
    the same minute the pre-warm would be untestable and, worse, indistinguishable
    in the log."""
    assert fc.SAFETY_SWEEP_MINUTE not in fc.PREWARM_NZ_MINUTES


def test_the_decision_is_logged(caplog):
    """A decision nobody can see is a decision nobody can show ran -- this repo
    lost a whole feature to INFO logs being dropped. The prod verification of
    the pre-warm is this line, not the capacity graph."""
    import logging
    src = open(fc.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    assert "pre-warm connect" in src
    assert logging.getLogger().level <= logging.INFO, (
        "the root logger must be at INFO or the line never reaches CloudWatch")
