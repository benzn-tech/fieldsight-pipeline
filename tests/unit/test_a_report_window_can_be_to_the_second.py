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
