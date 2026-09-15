"""Ask Agent: scoped Ask (spec 2026-09-15 §4.2, §5 tests 7-11).

Helpers first (validation, precedence table), then the route, driven through
_rag_answer with a fake rag-search -- each return reached for real, never a
source scan.
"""
import io
import json
import os

import pytest

os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

import lambda_ask_agent as laa   # noqa: E402
import llm_utils                 # noqa: E402
import dashscope_utils           # noqa: E402
import web_answer                # noqa: E402

TOPIC_ID = "df023596-1111-4222-8333-444455556666"
SITE_ID = "5c0e8d7a-1111-4222-8333-444455556666"
NOW = "2026-08-30T09:00:00+00:00"     # NZ 2026-08-30; "yesterday" = 2026-08-29


def drops(items):
    return {(d["field"], d["reason"]) for d in items}


# --------------------------------------------------------------------------
# _validate_scope  (spec §5 test 8, helper half)
# --------------------------------------------------------------------------

def test_well_formed_fields_survive_and_are_canonical():
    req, dropped = laa._validate_scope({"topic_row_id": TOPIC_ID.upper(), "site_id": SITE_ID,
                                        "date": "2026-09-03", "author_folder": " Ben_UCPK2 "})
    assert req == {"topic_row_id": TOPIC_ID, "site_id": SITE_ID,
                   "date": "2026-09-03", "author_folder": "Ben_UCPK2"}
    assert dropped == []


@pytest.mark.parametrize("field,value", [
    ("topic_row_id", "not-a-uuid"), ("topic_row_id", 7),
    ("site_id", "123"), ("site_id", ["x"]),
    ("date", "2026-02-30"), ("date", "2026-9-3"), ("date", 20260903), ("date", "03/09/2026"),
    ("author_folder", "x" * 201), ("author_folder", 42), ("author_folder", "   "),
])
def test_malformed_fields_are_dropped_invalid_and_never_raise(field, value):
    req, dropped = laa._validate_scope({field: value})
    assert field not in req
    assert dropped == [{"field": field, "reason": "invalid"}]


@pytest.mark.parametrize("value", [None, ""])
def test_absent_fields_are_neither_kept_nor_dropped(value):
    body = {f: value for f in ("topic_row_id", "site_id", "date", "author_folder")}
    assert laa._validate_scope(body) == ({}, [])


def test_an_author_folder_of_exactly_200_chars_is_kept():
    req, _ = laa._validate_scope({"author_folder": "a" * 200})
    assert req["author_folder"] == "a" * 200


# --------------------------------------------------------------------------
# _scope_range  (spec §4.2 table, helper half)
# --------------------------------------------------------------------------

Q = ("2026-08-29", "2026-08-29")
D = "2026-09-01"


@pytest.mark.parametrize("req,q,want", [
    # topic pinned, question range, body date -> body date..date, no widen
    ({"topic_row_id": TOPIC_ID, "date": D}, Q,
     {"from": D, "to": D, "widen": False, "body_date_sent": True,
      "dropped": [{"field": "question_range", "reason": "overridden_by_topic"}]}),
    # topic pinned, question range, no body date -> no range
    ({"topic_row_id": TOPIC_ID}, Q,
     {"from": None, "to": None, "widen": False, "body_date_sent": False,
      "dropped": [{"field": "question_range", "reason": "overridden_by_topic"}]}),
    # topic pinned, no question range, body date -> body date..date
    ({"topic_row_id": TOPIC_ID, "date": D}, (None, None),
     {"from": D, "to": D, "widen": False, "body_date_sent": True, "dropped": []}),
    # topic pinned, nothing else -> no range
    ({"topic_row_id": TOPIC_ID}, (None, None),
     {"from": None, "to": None, "widen": False, "body_date_sent": False, "dropped": []}),
    # no topic, question range, body date -> question wins
    ({"date": D}, Q,
     {"from": Q[0], "to": Q[1], "widen": True, "body_date_sent": False,
      "dropped": [{"field": "date", "reason": "overridden_by_question"}]}),
    # no topic, question range, no body date
    ({}, Q, {"from": Q[0], "to": Q[1], "widen": True, "body_date_sent": False, "dropped": []}),
    # no topic, no question range, body date -> body date, no widen
    ({"date": D}, (None, None),
     {"from": D, "to": D, "widen": False, "body_date_sent": True, "dropped": []}),
    # nothing
    ({}, (None, None), {"from": None, "to": None, "widen": False, "body_date_sent": False, "dropped": []}),
])
def test_the_precedence_table(req, q, want):
    assert laa._scope_range(req, *q) == want


# --------------------------------------------------------------------------
# _applied_scope
# --------------------------------------------------------------------------

def test_applied_scope_copies_only_enforced_fields_and_merges_drops():
    req = {"topic_row_id": TOPIC_ID, "date": D}
    plan = laa._scope_range(req, *Q)
    result = {"pinned_topic": {"report_date": "2026-09-03"},
              "applied": {"topic_row_id": TOPIC_ID, "topic_title": "Scaffold handover",
                          "site_id": SITE_ID, "date": "2026-09-03", "dropped": []}}

    out = laa._applied_scope(result, req, plan, plan["dropped"])

    assert out == {"topic_row_id": TOPIC_ID, "topic_title": "Scaffold handover",
                   "site_id": SITE_ID, "date": "2026-09-03",
                   "dropped": [{"field": "question_range", "reason": "overridden_by_topic"},
                               {"field": "date", "reason": "overridden_by_topic"}]}


def test_applied_scope_without_a_pinned_topic_claims_the_body_date_it_sent():
    req = {"topic_row_id": TOPIC_ID, "date": D}
    plan = laa._scope_range(req, None, None)
    result = {"applied": {"dropped": [{"field": "topic_row_id", "reason": "not_visible"}]}}

    out = laa._applied_scope(result, req, plan, plan["dropped"])

    assert out == {"date": D, "dropped": [{"field": "topic_row_id", "reason": "not_visible"}]}


def test_a_rag_search_that_predates_applied_claims_nothing_but_the_body_date():
    req = {"site_id": SITE_ID}
    plan = laa._scope_range(req, None, None)
    assert laa._applied_scope({"chunks": []}, req, plan, []) == {"dropped": []}
