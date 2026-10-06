"""A report can cover a spoken check's window to the second.

The Level 1 pre-pour check on prod 10-05 ran 10:59:09-11:02:14, and the Level
2 steel check began at 11:02:14: to the minute, the two windows share 11:02.
"""
import datetime

import pytest

sr = pytest.importorskip("lambda_session_report")
org = pytest.importorskip("lambda_org_api")


def test_THE_the_worker_reads_a_window_to_the_second():
    assert sr._clock("2026-10-05", "11:02:14") == datetime.datetime(2026, 10, 5, 11, 2, 14)
    assert sr._clock("2026-10-05", "11:02") == datetime.datetime(2026, 10, 5, 11, 2)


def test_the_request_keeps_a_good_window_and_drops_a_bad_end():
    assert org._report_window({"from": "10:59:09", "to": "11:02:14"}) == {"from": "10:59:09", "to": "11:02:14"}
    assert org._report_window({"from": "09:00", "to": "11:30"}) == {"from": "09:00", "to": "11:30"}
    assert org._report_window({}) == {"from": "00:00", "to": "23:59"}
    assert org._report_window({"from": "25:00", "to": "11:02:60"}) == {"from": "00:00", "to": "23:59"}
    assert org._report_window({"from": "x; drop", "to": None}) == {"from": "00:00", "to": "23:59"}


def test_both_report_routes_use_it():
    src = open(org.__file__, encoding="utf-8").read()
    assert src.count('"window": _report_window(body),') == 2


SEGS = [{"from": "11:02:03", "to": "11:02:21"}, {"from": "11:02:41", "to": "11:03:21"}]


def test_THE_an_interrupted_checks_stretches_travel_and_the_gap_is_left_out():
    w = org._report_window({"from": "11:02:03", "to": "11:03:21", "segments": SEGS})
    assert w == {"from": "11:02:03", "to": "11:03:21", "segments": SEGS}
    gaps = sr._segment_gaps("2026-10-05", w["segments"])
    assert gaps == [(datetime.datetime(2026, 10, 5, 11, 2, 21), datetime.datetime(2026, 10, 5, 11, 2, 41))]


def test_one_bad_stretch_and_the_window_is_plain():
    for bad in ([SEGS[1], SEGS[0]],                                  # out of order
                [SEGS[0], {"from": "11:03:00", "to": "11:02:50"}],    # backwards
                [SEGS[0], {"from": "nope", "to": "11:04"}],
                [SEGS[0]]):                                          # one is not "stretches"
        w = org._report_window({"from": "11:02:03", "to": "11:03:21", "segments": bad})
        assert w == {"from": "11:02:03", "to": "11:03:21"}, bad


def test_the_steel_check_between_the_stretches_is_not_offered():
    topics = [{"time_range": "11:01 – 11:02", "topic_title": "pre-pour"},
              {"time_range": "11:05 – 11:06", "topic_title": "steel"},     # in the gap
              {"time_range": "11:08 – 11:09", "topic_title": "pre-pour again"}]
    day = "2026-10-05"
    stretches = [(sr._clock(day, "11:01:00"), sr._clock(day, "11:03:00")),
                 (sr._clock(day, "11:08:00"), sr._clock(day, "11:10:00"))]
    kept = [t["topic_title"] for t in topics
            if any(sr._in_window(t, day, a, b) for a, b in stretches)]
    assert kept == ["pre-pour", "pre-pour again"]
    src = open(sr.__file__, encoding="utf-8").read()
    assert "spans = spans + gaps" in src
    assert "if (any(test(t, date, a, b) for a, b in stretches) if stretches" in src



def test_THE_a_checks_report_reads_only_the_speech_inside_its_window():
    """TEST, 2026-10-06: the pre-pour check ended 11:02:14, and the Level 2
    PPE talk later in the same audio file answered its PPE item."""
    day = "2026-10-05"
    w = {"from": "10:59:09", "to": "11:02:14"}
    assert sr._precise(w) and not sr._precise({"from": "09:00", "to": "11:30"})
    steel = {"time_range": "11:02 – 11:03"}
    hvac = {"time_range": "10:59 – 11:02"}
    a, b = sr._clock(day, w["from"]), sr._clock(day, w["to"])
    assert not sr._mostly_in(steel, day, a, b), "14 of its 120 seconds"
    assert sr._mostly_in(hvac, day, a, b)
    src = open(sr.__file__, encoding="utf-8").read()
    assert 'turns = [t for t in turns if (t.get("until") or t["at"]) > win_from and t["at"] < win_to' in src
    assert "precise=_precise(window)" in src
