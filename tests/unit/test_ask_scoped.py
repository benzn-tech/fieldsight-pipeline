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


# --------------------------------------------------------------------------
# route harness
# --------------------------------------------------------------------------

CHUNK = {"id": "c-1", "chunk_text": "Scaffold tagged.", "chunk_type": "topic",
         "topic_id": None, "source_s3_key": "reports/2026-09-03/Ben/daily_report.json",
         "metadata": {}, "topic_title": "Scaffold", "topic_summary": "",
         "report_date": "2026-09-03", "site_id": SITE_ID, "site_name": "UC PK",
         "site_slug": "uc-pk", "distance": 0.1}

PINNED = {"id": TOPIC_ID, "title": "Scaffold handover", "summary": "Signed off.",
          "report_date": "2026-09-03", "site_id": SITE_ID, "site_name": "UC PK",
          "user_id": "u-7", "time_range": "09:10–09:40",
          "action_items": [{"text": "Send tag photos", "responsible": "Ben",
                            "deadline": None, "status": "open"}]}


class FakeLambdaClient:
    def __init__(self, responses, function_error=None):
        self.responses = list(responses)
        self.function_error = function_error
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append(json.loads(Payload))
        body = self.responses.pop(0) if self.responses else {"chunks": []}
        resp = {"Payload": io.BytesIO(json.dumps(body).encode("utf-8"))}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


def wire(mp, responses=({"chunks": [CHUNK]},), answer=("Grounded answer [1].", None),
         function_error=None, web=None):
    mp.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    client = FakeLambdaClient(responses, function_error=function_error)
    mp.setattr(laa, "_get_lambda_client", lambda: client)
    seen = {"llm_calls": 0}

    def fake_llm(prompt, max_tokens=4096, force_json=False):
        seen["prompt"] = prompt
        seen["llm_calls"] += 1
        return answer

    mp.setattr(llm_utils, "call_llm", fake_llm)
    mp.setattr(web_answer, "answer", lambda question, chunks, **k: web)
    return client, seen


def ask(**body):
    body.setdefault("caller_sub", "sub-1")
    body.setdefault("tz", "Pacific/Auckland")
    body.setdefault("now", NOW)
    return laa._rag_answer(body)


# --------------------------------------------------------------------------
# spec §5 test 7: every row of the precedence table, through the route
# --------------------------------------------------------------------------

def test_topic_with_question_range_and_other_day_sends_the_body_date_and_reports_both(monkeypatch):
    client, _ = wire(monkeypatch, responses=[{
        "chunks": [CHUNK], "pinned_topic": PINNED,
        "applied": {"topic_row_id": TOPIC_ID, "topic_title": "Scaffold handover",
                    "site_id": SITE_ID, "date": "2026-09-03", "dropped": []}}])

    out = ask(question="what did we say yesterday", topic_row_id=TOPIC_ID, date="2026-09-01")

    p = client.calls[0]
    assert (p["date_from"], p["date_to"]) == ("2026-09-01", "2026-09-01")
    assert p.get("widen_when_empty", False) is False
    assert p["topic_row_id"] == TOPIC_ID
    assert out["applied_scope"]["date"] == "2026-09-03"
    assert out["applied_scope"]["topic_title"] == "Scaffold handover"
    assert drops(out["applied_scope"]["dropped"]) == {("question_range", "overridden_by_topic"),
                                                      ("date", "overridden_by_topic")}


def test_topic_not_visible_with_body_date_still_narrows_to_that_day(monkeypatch):
    client, _ = wire(monkeypatch, responses=[{
        "chunks": [CHUNK],
        "applied": {"dropped": [{"field": "topic_row_id", "reason": "not_visible"}]}}])

    out = ask(question="concrete issues", topic_row_id=TOPIC_ID, date="2026-09-01")

    assert (client.calls[0]["date_from"], client.calls[0]["date_to"]) == ("2026-09-01", "2026-09-01")
    assert len(client.calls) == 1                                # no retry
    assert out["applied_scope"]["date"] == "2026-09-01"
    assert drops(out["applied_scope"]["dropped"]) == {("topic_row_id", "not_visible")}


def test_topic_alone_sends_no_range(monkeypatch):
    client, _ = wire(monkeypatch)
    ask(question="concrete issues", topic_row_id=TOPIC_ID)
    assert "date_from" not in client.calls[0] and "widen_when_empty" not in client.calls[0]


def test_question_range_beats_body_date_without_a_topic(monkeypatch):
    client, _ = wire(monkeypatch)
    out = ask(question="what happened yesterday", date="2026-09-01")
    p = client.calls[0]
    assert (p["date_from"], p["date_to"]) == ("2026-08-29", "2026-08-29")
    assert p["widen_when_empty"] is True
    assert "date" not in out["applied_scope"]
    assert drops(out["applied_scope"]["dropped"]) == {("date", "overridden_by_question")}


def test_question_range_alone_widens_as_today(monkeypatch):
    client, _ = wire(monkeypatch)
    out = ask(question="what happened yesterday")
    assert client.calls[0]["widen_when_empty"] is True
    assert out["applied_scope"] == {"dropped": []}


def test_body_date_alone_narrows_and_never_widens(monkeypatch):
    client, _ = wire(monkeypatch)
    out = ask(question="concrete issues", date="2026-09-01")
    p = client.calls[0]
    assert (p["date_from"], p["date_to"]) == ("2026-09-01", "2026-09-01")
    assert "widen_when_empty" not in p
    assert out["applied_scope"] == {"date": "2026-09-01", "dropped": []}


def test_nothing_requested_keeps_the_payload_byte_identical(monkeypatch):
    client, _ = wire(monkeypatch)
    out = ask(question="concrete issues")
    assert set(client.calls[0]) == {"sub", "query_embedding", "k"}
    assert out["applied_scope"] == {"dropped": []}


def test_site_and_author_are_forwarded_under_rag_search_names(monkeypatch):
    client, _ = wire(monkeypatch)
    ask(question="concrete issues", site_id=SITE_ID, author_folder="Ben_UCPK2")
    assert client.calls[0]["site"] == SITE_ID
    assert client.calls[0]["author"] == "Ben_UCPK2"


# --------------------------------------------------------------------------
# spec §5 test 8: malformed values never reach rag-search
# --------------------------------------------------------------------------

def test_malformed_values_are_dropped_and_the_answer_still_comes_back(monkeypatch):
    client, _ = wire(monkeypatch)

    out = ask(question="concrete issues", topic_row_id="not-a-uuid", site_id="123",
              date="2026-02-30", author_folder="x" * 201)

    p = client.calls[0]
    for key in ("topic_row_id", "site", "author", "date_from", "date_to"):
        assert key not in p
    assert out["answer"] == "Grounded answer [1]."
    assert "error" not in out
    assert drops(out["applied_scope"]["dropped"]) == {
        ("topic_row_id", "invalid"), ("site_id", "invalid"),
        ("date", "invalid"), ("author_folder", "invalid")}


# --------------------------------------------------------------------------
# spec §5 test 9: a scoped count is never answered unscoped
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [("site_id", SITE_ID), ("author_folder", "Ben_UCPK2"),
                                         ("topic_row_id", TOPIC_ID)])
def test_scope_skips_the_metric_route(monkeypatch, field, value):
    client, _ = wire(monkeypatch)
    ask(question="how many photos did I take yesterday", **{field: value})
    assert client.calls[0].get("mode") != "metric"
    assert "query_embedding" in client.calls[0]


def test_an_invalid_scope_field_does_not_skip_the_metric_route(monkeypatch):
    client, _ = wire(monkeypatch, responses=[{"metric": "count_photos", "value": 3,
                                              "unit": "photos", "notes": {}}])
    ask(question="how many photos did I take yesterday", site_id="123")
    assert client.calls[0]["mode"] == "metric"


# --------------------------------------------------------------------------
# spec §5 test 11: applied_scope on every return, each driven
# --------------------------------------------------------------------------

def _assert_scoped(out):
    assert isinstance(out["applied_scope"], dict)
    assert isinstance(out["applied_scope"]["dropped"], list)


def test_rag_return_function_error(monkeypatch):
    wire(monkeypatch, function_error="Unhandled")
    out = ask(question="concrete issues", date="bad")
    assert out["error"] == "rag-search unavailable"
    _assert_scoped(out)
    assert drops(out["applied_scope"]["dropped"]) == {("date", "invalid")}


def test_rag_return_empty_with_web_answer(monkeypatch):
    wire(monkeypatch, responses=[{"chunks": []}], web={"answer": "From the web."})
    out = ask(question="concrete issues")
    assert out.get("from_web") is True
    _assert_scoped(out)


def test_rag_return_empty_no_answer(monkeypatch):
    wire(monkeypatch, responses=[{"chunks": [], "applied": {"dropped": []}}])
    out = ask(question="concrete issues", date="2026-09-01")
    assert out["answer"] == "No relevant records found for this question."
    assert out["applied_scope"] == {"date": "2026-09-01", "dropped": []}


def test_rag_return_web_with_chunks(monkeypatch):
    wire(monkeypatch, web={"answer": "From the web."})
    out = ask(question="concrete issues")
    assert out.get("from_web") is True
    _assert_scoped(out)


def test_rag_return_llm_error(monkeypatch):
    wire(monkeypatch, answer=("", "model exploded"))
    out = ask(question="concrete issues")
    assert out["error"] == "model exploded"
    _assert_scoped(out)


def test_rag_return_success(monkeypatch):
    wire(monkeypatch)
    out = ask(question="concrete issues")
    assert out["grounded"] is True and out["answer"] == "Grounded answer [1]."
    _assert_scoped(out)


def test_rag_return_exception(monkeypatch):
    wire(monkeypatch)

    def explode(texts, dim=None):
        raise RuntimeError("embed down")

    monkeypatch.setattr(dashscope_utils, "embed", explode)
    out = ask(question="concrete issues")
    assert out["error"] == "embed down"
    _assert_scoped(out)


METRIC_Q = "how long did I record yesterday"


def test_metric_return_not_configured(monkeypatch):
    wire(monkeypatch)
    monkeypatch.setattr(laa, "RAG_SEARCH_FUNCTION", "")
    out = ask(question=METRIC_Q, date="2026-08-20")
    assert out["error"] == "rag-search not configured"
    assert out["applied_scope"] == {"dropped": [{"field": "date", "reason": "overridden_by_question"}]}


def test_metric_return_function_error(monkeypatch):
    wire(monkeypatch, function_error="Unhandled")
    out = ask(question=METRIC_Q)
    assert out["error"] == "rag-search unavailable"
    assert out["applied_scope"] == {"dropped": []}


def test_metric_return_success(monkeypatch):
    wire(monkeypatch, responses=[{"metric": "duration", "value": 600, "unit": "seconds",
                                  "notes": {}}])
    out = ask(question=METRIC_Q)
    assert out["computed"] is True
    assert out["applied_scope"] == {"dropped": []}


# --------------------------------------------------------------------------
# Task 5 review carry-overs
# --------------------------------------------------------------------------

def test_a_full_width_date_is_dropped_invalid():
    req, dropped = laa._validate_scope({"date": "\uff12\uff10\uff12\uff16-\uff10\uff19-\uff10\uff13"})
    assert "date" not in req
    assert dropped == [{"field": "date", "reason": "invalid"}]


def test_pinned_topic_on_the_body_date_reports_no_date_override(monkeypatch):
    wire(monkeypatch, responses=[{
        "chunks": [CHUNK], "pinned_topic": PINNED,
        "applied": {"topic_row_id": TOPIC_ID, "topic_title": "Scaffold handover",
                    "site_id": SITE_ID, "date": "2026-09-03", "dropped": []}}])

    out = ask(question="concrete issues", topic_row_id=TOPIC_ID, date="2026-09-03")

    assert out["applied_scope"]["date"] == "2026-09-03"
    assert ("date", "overridden_by_topic") not in drops(out["applied_scope"]["dropped"])


def test_applied_scope_deduplicates_a_drop_reported_by_both_hops():
    req = {"site_id": SITE_ID}
    plan = laa._scope_range(req, None, None)
    same = {"field": "site_id", "reason": "not_visible"}
    out = laa._applied_scope({"applied": {"dropped": [dict(same)]}}, req, plan, [dict(same)])
    assert out["dropped"] == [same]
