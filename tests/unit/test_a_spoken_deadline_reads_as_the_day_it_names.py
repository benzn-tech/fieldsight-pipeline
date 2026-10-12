"""Unit: every surface shows a deadline as the day it names (owner, 2026-10-12).

"Tomorrow morning", "before Wednesday", "this arvo" are true only on the day
they were said. The email, the checklist and daily reports, and the Timeline
API now say "Tue 13 Oct 2026, morning" -- anchored on the RECORDING's date,
never today -- and keep the words when no day can be read from them.

No clock time is invented for display: "morning" stays "morning". The end of
that part of the day is used only in `by`, the moment it is overdue after.

The anchor below is the owner's pre-pour walk, Monday 2026-10-12.
"""
import pytest

import checklist
import deadline_parse
import due_display
import report_sections

MON = "2026-10-12"


@pytest.mark.parametrize("said, shown, by", [
    ("tomorrow morning", "Tue 13 Oct 2026, morning", "2026-10-13T12:00"),
    ("before Wednesday", "before Wed 14 Oct 2026", "2026-10-14T00:00"),
    ("this arvo", "Mon 12 Oct 2026, afternoon", "2026-10-12T17:00"),
    ("this afternoon", "Mon 12 Oct 2026, afternoon", "2026-10-12T17:00"),
    ("Wednesday arvo", "Wed 14 Oct 2026, afternoon", "2026-10-14T17:00"),
    ("Thursday 7am", "Thu 15 Oct 2026, 7am", "2026-10-15T07:00"),
    ("by lunchtime", "by Mon 12 Oct 2026, lunchtime", "2026-10-12T13:00"),
    ("EOD Friday", "Fri 16 Oct 2026, end of day", "2026-10-16T17:00"),
    ("by Friday", "by Fri 16 Oct 2026", "2026-10-16T23:59"),
    ("end of the week", "by Fri 16 Oct 2026", "2026-10-16T23:59"),
    ("2026-10-20", "Tue 20 Oct 2026", "2026-10-20T23:59"),
])
def test_a_spoken_deadline_reads_as_the_day_it_names(said, shown, by):
    d = due_display.describe(said, MON)
    assert (d["display"], d["by"]) == (shown, by)


def test_no_clock_time_is_invented_for_a_part_of_the_day():
    assert "11" not in due_display.display("tomorrow morning", MON)
    assert ":" not in due_display.display("this arvo", MON)


def test_what_names_no_day_is_shown_as_said():
    d = due_display.describe("before the pour", MON)
    assert d["display"] == "before the pour" and d["date"] is None and d["by"] is None
    assert due_display.describe("", MON) is None
    assert due_display.describe("TBC", MON) is None


def test_after_is_a_start_not_a_limit():
    d = due_display.describe("after Wednesday", MON)
    assert d["display"] == "after Wed 14 Oct 2026" and d["by"] is None


def test_the_anchor_is_the_day_it_was_said_not_today():
    assert due_display.display("tomorrow", "2026-02-27") == "Sat 28 Feb 2026"


@pytest.mark.parametrize("said, day", [
    # Each was NULL or wrong on 2026-10-12 and is what a site actually says.
    ("this arvo", "2026-10-12"),
    ("lunchtime", "2026-10-12"),
    ("in two days", "2026-10-14"),
    ("in a couple of days", "2026-10-14"),
    ("within a fortnight", "2026-10-26"),
    ("end of next week", "2026-10-23"),     # was Mon 19: four days early
    ("by 2:30pm", "2026-10-12"),
])
def test_the_parser_reads_what_a_site_says(said, day):
    assert deadline_parse.resolve_deadline(said, MON) == day


def test_a_weak_site_word_does_not_steal_a_named_day():
    assert deadline_parse.resolve_deadline("Wednesday arvo", MON) == "2026-10-14"
    assert deadline_parse.resolve_deadline("lunch on Friday", MON) == "2026-10-16"


def test_the_checklist_due_column_names_the_day():
    items = ["Release agent applied"]
    section = {"paragraphs": [
        "Item no | Answer | Comment | Responsible | Due | Evidence",
        "1 | No | Not on yet | formworkers | tomorrow morning | release agent's not on"],
        "line_refs": [[], []]}
    paragraphs, _refs, report = checklist.rebuild(
        section, items, "release agent's not on", report_date=MON)
    assert report["answered"] == 1
    assert paragraphs[2].endswith("| formworkers | Tue 13 Oct 2026, morning")


def test_the_daily_report_actions_name_the_day():
    rows = report_sections._actions(
        [{"action_items": [{"action": "Spare vibrator from Hirepool",
                            "responsible": "Ben Lin", "deadline": "this arvo"}]}], MON)
    assert rows["rows"][0]["due"] == "Mon 12 Oct 2026, afternoon"
