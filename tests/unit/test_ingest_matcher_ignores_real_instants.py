"""Unit: the report-topic matcher must not read a UTC instant as a local clock.

`_overlap_or_title_score` pairs a report topic with an extraction topic on an
authority-flip defer day, and the pairing decides which topic a report's RAG
chunks -- and therefore Ask's citations -- are attached to. Its docstring calls
time the PRIMARY signal: a temporal hit always beats any title match.

In production that signal has never fired. It reads `topics.occurred_at`, and no
writer has ever set that column, so every call fell through to title similarity.
The matcher has been correct by coincidence.

The coincidence ends the day anything writes the column. `occurred_at` is a
timestamptz; psycopg hands back an AWARE datetime in the session zone, which on
this connection is UTC. `_occurred_at_seconds` took `.hour` off it and compared
that with the report's `time_range`, which is the DEVICE's wall clock -- twelve
or thirteen hours away in New Zealand. The outcome is either a signal that stays
dead (nothing looks different, nobody finds out) or a confident wrong pairing:
a report topic at 01:40 beating every title match for an extraction topic that
happened at 13:40.

So an aware datetime now yields no time signal at all, and the matcher keeps
doing exactly what it has always done in production. Turning the time signal ON
correctly -- converting through the zone that stamped the clock -- is a separate
change with its own measurement, because it alters which topic Ask cites.

Wall-clock values keep working: strings and naive times are already in the
report's units, and they are what every existing test in test_lambda_ingest.py
feeds this function.
"""
import datetime

import pytest

ing = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")

UTC = datetime.timezone.utc
NZST = datetime.timezone(datetime.timedelta(hours=12))


def test_an_aware_utc_instant_gives_no_time_signal():
    """THE test. 01:40 UTC is 13:40 in Christchurch; reading its .hour as a
    wall clock is the defect."""
    instant = datetime.datetime(2026, 9, 11, 1, 40, tzinfo=UTC)
    assert ing._occurred_at_seconds(instant) is None


def test_any_aware_instant_gives_no_time_signal_whatever_its_zone():
    """Not a UTC special case. Which zone psycopg returns depends on the
    connection's TimeZone setting, and a matcher that is right only for one
    server setting is the same coincidence in a different place."""
    instant = datetime.datetime(2026, 9, 11, 13, 40, tzinfo=NZST)
    assert ing._occurred_at_seconds(instant) is None


def test_a_wall_clock_string_still_works():
    assert ing._occurred_at_seconds("13:40:00") == 13 * 3600 + 40 * 60


def test_a_naive_time_still_works():
    """No zone attached means it is already a wall clock, the report's units."""
    assert ing._occurred_at_seconds(datetime.time(13, 40)) == 13 * 3600 + 40 * 60
    assert (ing._occurred_at_seconds(datetime.datetime(2026, 9, 11, 13, 40))
            == 13 * 3600 + 40 * 60)


def test_a_utc_instant_cannot_beat_a_title_match():
    """The user-visible consequence, stated as behaviour. The extraction topic
    whose UTC hour happens to fall inside the report window must not be scored
    as a temporal hit -- that is how a citation ends up on the wrong topic."""
    report_topic = {"time_range": "01:30 – 01:50", "topic_title": "Slab pour"}
    wrong = {"title": "Unrelated site walk",
             "occurred_at": datetime.datetime(2026, 9, 11, 1, 40, tzinfo=UTC)}
    assert ing._overlap_or_title_score(report_topic, wrong) is None


def test_a_populated_column_changes_nothing_about_title_matching():
    """With the instant ignored, the pair scores exactly as it does today on
    prod, where the column is NULL."""
    report_topic = {"time_range": "13:30 – 13:50", "topic_title": "Slab pour"}
    today = {"title": "Slab pour", "occurred_at": None}
    tomorrow = {"title": "Slab pour",
                "occurred_at": datetime.datetime(2026, 9, 11, 1, 40, tzinfo=UTC)}
    assert (ing._overlap_or_title_score(report_topic, tomorrow)
            == ing._overlap_or_title_score(report_topic, today))
