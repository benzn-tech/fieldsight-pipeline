# tests/unit/test_ask_tools_args.py
from datetime import date
import pytest
import ask_tools
import query_slots

TODAY = date(2026, 10, 15)  # a Thursday


def test_span_is_public_and_matches_private():
    assert query_slots.span(TODAY, "yesterday") == (date(2026, 10, 14),) * 2
    assert query_slots._span is query_slots.span


def test_tools_have_exactly_the_five_v1_names_and_no_identity_args(monkeypatch):
    # The five v1 tools, plus the project-documents tool (spec 2026-10-16), which is
    # known to validation and dispatch but offered only while PROJECT_DOCUMENTS is on.
    v1 = ["day_overview", "search_records", "latest_activity", "find_people", "count_metric"]
    names = [t["name"] for t in ask_tools.TOOLS]
    assert names == v1 + [ask_tools.DOC_TOOL]
    monkeypatch.setenv("PROJECT_DOCUMENTS", "false")
    assert [t["name"] for t in ask_tools.offered_tools()] == v1
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
    assert err is None
    assert args == {"query": "pour", "date_from": "2026-10-01", "date_to": "2026-10-03"}


def test_day_overview_capped_to_today_then_clamped_to_latest_7_days():
    # Order is resolve -> cap at today -> clamp: 10-20 caps to 10-15, and the
    # 7-day clamp then keeps the most recent end, 10-09..10-15.
    args, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "2026-10-01", "date_to": "2026-10-20"}}, TODAY)
    assert err is None
    assert (args["date_from"], args["date_to"]) == ("2026-10-09", "2026-10-15")
    assert args["clamped"] == {"capped_to_today": True, "requested_from": "2026-10-01",
                               "max_days": 7}


def test_other_tools_clamped_to_latest_92_days():
    args, err = ask_tools.validate_call(
        {"name": "search_records", "args": {"query": "x", "date_from": "2026-01-01",
                                            "date_to": "2026-10-15"}}, TODAY)
    assert err is None
    assert (args["date_from"], args["date_to"]) == ("2026-07-16", "2026-10-15")
    assert args["clamped"] == {"requested_from": "2026-01-01", "max_days": 92}


def test_relative_span_start_for_from_end_for_to_and_on_or_before():
    a, err = ask_tools.validate_call({"name": "day_overview", "args": {"date_from": "last_week"}}, TODAY)
    assert err is None
    assert a == {"date_from": "2026-10-05", "date_to": "2026-10-11"}
    a, err = ask_tools.validate_call({"name": "day_overview",
        "args": {"date_from": "2026-10-12", "date_to": "this_week"}}, TODAY)
    assert err is None
    assert a == {"date_from": "2026-10-12", "date_to": "2026-10-15"}
    a, err = ask_tools.validate_call(
        {"name": "latest_activity", "args": {"on_or_before": "last_week"}}, TODAY)
    assert err is None
    assert a == {"on_or_before": "2026-10-11"}


def test_future_date_to_and_on_or_before_are_capped_to_today():
    a, err = ask_tools.validate_call(
        {"name": "search_records", "args": {"query": "x", "date_from": "2026-10-10",
                                            "date_to": "2026-12-31"}}, TODAY)
    assert err is None
    assert (a["date_from"], a["date_to"]) == ("2026-10-10", "2026-10-15")
    assert a["clamped"] == {"capped_to_today": True}
    a, err = ask_tools.validate_call(
        {"name": "latest_activity", "args": {"on_or_before": "2027-01-01"}}, TODAY)
    assert err is None
    assert a == {"on_or_before": "2026-10-15", "clamped": {"capped_to_today": True}}


def test_future_dates_not_capped_without_today():
    a, err = ask_tools.validate_call(
        {"name": "latest_activity", "args": {"on_or_before": "2027-01-01"}}, None)
    assert err is None and a == {"on_or_before": "2027-01-01"}


def test_range_wholly_in_the_future_is_bad_args_date_from():
    a, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "2026-10-20"}}, TODAY)
    assert a is None and err == {"error": "bad_args", "field": "date_from"}


def test_every_date_to_description_states_its_default():
    for t in ask_tools.TOOLS:
        prop = t["parameters"]["properties"].get("date_to")
        if prop is not None:
            assert "Defaults to date_from." in prop["description"], t["name"]


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
    ({"name": "search_records", "args": {"query": "x", "date_to": "2026-10-01"}}, "date_from"),
    ({"name": "search_records", "args": {"query": "x", "date_from": "2026-10-01",
                                         "date_to": ""}}, "date_to"),
])
def test_bad_args_name_the_field(call, field):
    args, err = ask_tools.validate_call(call, TODAY)
    assert args is None and err == {"error": "bad_args", "field": field}


def test_date_to_before_date_from_is_bad_args():
    args, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "2026-10-10", "date_to": "2026-10-09"}}, TODAY)
    assert args is None and err == {"error": "bad_args", "field": "date_to"}


SPOKEN_HINT = "Resolve spoken dates (e.g. '3 September', 'last Tuesday') to ISO before calling."


def test_the_model_is_told_to_resolve_spoken_dates_itself():
    """query_slots resolves only 'last <weekday>'; every other spoken date is
    the Ask judge's job, so the tool schema must say so."""
    by_name = {t["name"]: t for t in ask_tools.TOOLS}
    assert SPOKEN_HINT in by_name["day_overview"]["description"]
    props = by_name["search_records"]["parameters"]["properties"]
    assert SPOKEN_HINT in props["date_from"]["description"]
    latest = by_name["latest_activity"]["parameters"]["properties"]
    assert SPOKEN_HINT in latest["on_or_before"]["description"]


def test_latest_activity_description_asks_for_a_short_subject():
    """latest_activity(query) picks the day naming the MOST words of `query`
    (spec 2026-10-20 §8, coverage first): a short subject keeps generic words
    from deciding it, so the model is still asked for one to three words."""
    desc = {t["name"]: t for t in ask_tools.offered_tools()}["latest_activity"]["description"]
    assert "most words" in desc and "one to three words" in desc
    assert "every word" not in desc
