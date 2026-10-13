"""A topic's photographs go to the most specific line that names it.

Owner, 2026-10-01 (TEST, Ben_Lin_test2): every inspection photograph landed
under the Daily Summary's prose, because the overview names every topic
first, and none in the section that listed the inspections. A photograph
still appears once; it goes to a table row before a list item before a
paragraph, the first of equals.

Also here, the same day's other two findings: the document's subtitle is
the date only, and Report Details says what was actually recorded rather
than the 00:00 - 23:59 window "Everything" asks for.

THE test is `a photograph cited in the summary and in a table goes in the table`.
"""
import datetime as dt

import lambda_session_report as sr
import report_facts


def sections(text):
    return sr._prose_sections(text)


SUMMARY_AND_TABLE = ("### Daily Summary\nLevel 1 inspection continued. [t2]\n\n"
                     "### Inspections\nArea | Finding\n---|---\nLevel 1 | Slab clean [t2]\n")


def test_THE_a_photograph_cited_in_the_summary_and_in_a_table_goes_in_the_table():
    secs = sections(SUMMARY_AND_TABLE)
    at_line, _, _ = sr._place_photos(secs, {"t2": ["p1", "p2"]})
    assert at_line == 2
    assert "photos_after" not in secs[0], "not under the summary"
    assert secs[1]["photos_after"] == {2: ["p1", "p2"]}, "in the table row"


def test_a_list_item_beats_a_paragraph():
    secs = sections("### Daily Summary\nWalked the ground floor. [t1]\n\n"
                    "### Quality\n- Ground floor inspected [t1]\n")
    sr._place_photos(secs, {"t1": ["p"]})
    assert "photos_after" not in secs[0] and secs[1]["photos_after"] == {0: ["p"]}


def test_of_equals_the_first_keeps_it_and_it_appears_once():
    secs = sections("### A\n- one [t1]\n\n### B\n- two [t1]\n")
    at_line, _, _ = sr._place_photos(secs, {"t1": ["p"]})
    assert at_line == 1 and secs[0]["photos_after"] == {0: ["p"]} and "photos_after" not in secs[1]


def test_a_pipe_in_prose_is_not_a_table_row():
    secs = sections("### A\n- Gate A | Gate B [t1]\n\n### B\nGate A | Gate B noted [t1]\n")
    sr._place_photos(secs, {"t1": ["p"]})
    assert secs[0]["photos_after"] == {0: ["p"]}


# ---- the recorded span -----------------------------------------------------------

def test_the_span_runs_from_the_first_topic_to_the_last():
    assert report_facts.recorded_span(["13:22 – 13:24", "11:41 – 11:41", "13:28 - 13:28", None]) \
        == ("11:41", "13:28")


def test_without_topic_times_the_speech_decides():
    turns = [dt.datetime(2026, 10, 1, 13, 22, 15), dt.datetime(2026, 10, 1, 9, 5, 0)]
    assert report_facts.recorded_span([None, ""], turns) == ("09:05", "13:22")
    assert report_facts.recorded_span([], []) is None


def test_report_details_says_what_was_recorded():
    sec = report_facts.header_section("Report details",
                                      {"date": "2026-10-01", "window": {"from": "00:00", "to": "23:59"}},
                                      {}, ("11:41", "13:28"))
    assert "Recording window | 11:41 - 13:28" in sec["paragraphs"]
