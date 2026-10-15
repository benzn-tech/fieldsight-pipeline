# tests/unit/test_ask_tools_args.py
from datetime import date
import pytest
import ask_tools
import query_slots

TODAY = date(2026, 10, 15)  # a Thursday


def test_span_is_public_and_matches_private():
    assert query_slots.span(TODAY, "yesterday") == (date(2026, 10, 14),) * 2
    assert query_slots._span is query_slots.span


def test_tools_have_exactly_the_five_v1_names_and_no_identity_args():
    names = [t["name"] for t in ask_tools.TOOLS]
    assert names == ["day_overview", "search_records", "latest_activity",
                     "find_people", "count_metric"]
    for t in ask_tools.TOOLS:
        props = t["parameters"]["properties"]
        for forbidden in ("sub", "caller_sub", "company", "company_id", "tz", "today"):
            assert forbidden not in props, (t["name"], forbidden)


def test_relative_date_resolves_against_caller_today():
    args, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "yesterday"}}, TODAY)
    assert err is None
    assert (args["date_from"], args["date_to"]) == ("2026-10-14", "2026-10-14")


def test_relative_date_without_today_is_bad_args_tz_never_unfiltered():
    args, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "yesterday"}}, None)
    assert args is None
    assert err == {"error": "bad_args", "field": "tz"}


def test_iso_dates_need_no_today():
    args, err = ask_tools.validate_call(
        {"name": "search_records", "args": {"query": "pour", "date_from": "2026-10-01",
                                            "date_to": "2026-10-03"}}, None)
    assert err is None and args["date_from"] == "2026-10-01"


def test_day_overview_clamped_to_7_days():
    args, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "2026-10-01", "date_to": "2026-10-20"}}, TODAY)
    assert err is None
    assert args["date_to"] == "2026-10-07"
    assert args["clamped"] == {"requested_to": "2026-10-20", "max_days": 7}


def test_other_tools_clamped_to_92_days():
    args, _ = ask_tools.validate_call(
        {"name": "search_records", "args": {"query": "x", "date_from": "2026-01-01",
                                            "date_to": "2026-10-15"}}, TODAY)
    assert args["date_to"] == "2026-04-02"


@pytest.mark.parametrize("call,field", [
    ({"name": "nope", "args": {}}, "name"),
    ({"name": "search_records", "args": {}}, "query"),
    ({"name": "search_records", "args": {"query": "x", "k": 13}}, "k"),
    ({"name": "search_records", "args": {"query": "x", "k": True}}, "k"),
    ({"name": "day_overview", "args": {"date_from": "15/10/2026"}}, "date_from"),
    ({"name": "count_metric", "args": {"metric": "duration", "date_from": "2026-10-01",
                                       "site": "S1"}}, "site"),
    ({"name": "count_metric", "args": {"metric": "bogus", "date_from": "2026-10-01"}}, "metric"),
    ({"name": "day_overview", "args": {"date_from": "2026-10-01", "sub": "x"}}, "sub"),
])
def test_bad_args_name_the_field(call, field):
    args, err = ask_tools.validate_call(call, TODAY)
    assert args is None and err == {"error": "bad_args", "field": field}


def test_date_to_before_date_from_is_bad_args():
    _, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "2026-10-10", "date_to": "2026-10-09"}}, TODAY)
    assert err == {"error": "bad_args", "field": "date_to"}
