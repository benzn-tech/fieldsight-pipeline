"""ask_items: the action-pack agent's items tool -- request validation, the client wrapper
(identity from the context, never the args), the `{tool, id, items, meta}` envelope, and
that Ask never sees it (action packs spec §13, §14 S4; Task 8)."""
import io
import json
import uuid

import pytest

import ask_items
import ask_tools

SID = str(uuid.uuid4())


class FakeLambda:
    def __init__(self, body=None, function_error=None, exc=None):
        self.body, self.events = body or {"rows": [], "meta": {"empty_reason": "no_match"}}, []
        self.function_error, self.exc = function_error, exc

    def invoke(self, FunctionName, InvocationType, Payload):
        if self.exc:
            raise self.exc
        self.events.append(json.loads(Payload))
        out = {"Payload": io.BytesIO(json.dumps(self.body).encode())}
        if self.function_error:
            out["FunctionError"] = self.function_error
        return out


def _ctx(fake, deadline=1030.0, refs=None):
    kw = {} if refs is None else {"refs": refs}
    return ask_tools.ToolContext(caller_sub="sub-creator", today=None, deadline=deadline,
                                 lambda_client=fake, function_name="rag",
                                 clock=lambda: 1000.0, **kw)


def _row(**kw):
    base = {"stable_id": SID, "task_no": 7, "text": "Fix the fire door", "status": "open",
            "deadline": None, "responsible": None, "priority": None,
            "updated_at": None, "created_at": "2026-10-30T00:00:00+00:00",
            "site_id": str(uuid.uuid4()), "site": "Alpha", "topic_id": str(uuid.uuid4()),
            "report_date": "2026-10-30", "source": "ai"}
    return dict(base, **kw)


# --- validation --------------------------------------------------------------

def test_defaults():
    norm, err = ask_items.validate_request({})
    assert err is None
    assert norm == {"site_id": None, "statuses": ["open", "in_progress", "blocked"],
                    "query": None, "task_no": None, "limit": 50}
    assert ask_items.validate_request(None)[0] == norm


@pytest.mark.parametrize("args, field", [
    ([], "args"),
    ({"company_id": "x"}, "company_id"),          # identity is never an argument
    ({"sub": "someone"}, "sub"),
    ({"site_id": "Alpha"}, "site_id"),
    ({"site_id": 5}, "site_id"),
    ({"status": []}, "status"),
    ({"status": [""]}, "status"),
    ({"status": ["x" * 33]}, "status"),
    ({"status": ["open"] * 9}, "status"),
    ({"status": [1]}, "status"),
    ({"query": 5}, "query"),
    ({"query": "x" * 201}, "query"),
    ({"task_no": 0}, "task_no"),
    ({"task_no": True}, "task_no"),
    ({"task_no": "6.9"}, "task_no"),
    ({"task_no": 2147483648}, "task_no"),
    ({"limit": 0}, "limit"),
    ({"limit": False}, "limit"),
])
def test_bad_args(args, field):
    assert ask_items.validate_request(args) == (None, {"error": "bad_args", "field": field})


def test_normalizes_and_clamps():
    norm, err = ask_items.validate_request({
        "site_id": SID.upper(), "status": ["Done", "done", " closed "],
        "query": "  fire door  ", "task_no": "69", "limit": 500})
    assert err is None
    assert norm == {"site_id": SID, "statuses": ["done", "closed"], "query": "fire door",
                    "task_no": 69, "limit": 200, "clamped": {"limit": 200}}
    # A single status string is a one-element set; a blank query is no query.
    norm, _ = ask_items.validate_request({"status": "blocked", "query": "   "})
    assert norm["statuses"] == ["blocked"] and norm["query"] is None


def test_wire_args_round_trip():
    norm, _ = ask_items.validate_request({"site_id": SID, "query": "door", "task_no": 3,
                                          "limit": 10})
    wire = ask_items.wire_args(norm)
    assert wire == {"status": ["open", "in_progress", "blocked"], "limit": 10,
                    "site_id": SID, "query": "door", "task_no": 3}
    assert ask_items.validate_request(wire) == (dict(norm), None)


# --- envelope ----------------------------------------------------------------

def test_envelope_items_keep_every_snapshot_field_even_none():
    env = ask_items.envelope("g1", [_row()], {"truncated": False}, ask_tools.RefAssigner())
    assert env["tool"] == "list_items" and env["id"] == "g1"
    [it] = env["items"]
    assert it["ref"] == "R1" and it["kind"] == "item" and it["provenance"] == "records"
    assert set(ask_items.ITEM_FIELDS) <= set(it)
    # A None deadline that vanished would make every update_item stale at apply.
    for f in ("status", "deadline", "responsible", "priority", "text", "updated_at"):
        assert f in it
    assert it["deadline"] is None and it["stable_id"] == SID and it["task_no"] == 7


def test_envelope_refs_are_shared_with_the_turn_and_deduped():
    refs = ask_tools.RefAssigner()
    assert refs.assign("topic", "t1") == "R1"
    other = str(uuid.uuid4())
    env = ask_items.envelope("g", [_row(), _row(), _row(stable_id=other), {"text": "x"}],
                             {}, refs)
    assert [(i["ref"], i["stable_id"]) for i in env["items"]] == [("R2", SID), ("R3", other)]
    again = ask_items.envelope("g2", [_row(stable_id=other)], {}, refs)
    assert again["items"][0]["ref"] == "R3"


def test_envelope_budget_truncates(monkeypatch):
    monkeypatch.setattr(ask_items, "MAX_RESULT_CHARS", 900)
    rows = [_row(stable_id=str(uuid.uuid4())) for _ in range(5)]
    env = ask_items.envelope("g", rows, {}, ask_tools.RefAssigner())
    assert 0 < len(env["items"]) < 5 and env["meta"]["truncated"] is True
    assert len(json.dumps(env["items"])) <= 900


def test_envelope_empty_needs_a_reason():
    env = ask_items.envelope("g", [], {}, ask_tools.RefAssigner())
    assert env["items"] == [] and env["meta"]["error"] == "upstream"
    env = ask_items.envelope("g", [], {"empty_reason": "no_match"}, ask_tools.RefAssigner())
    assert "error" not in env["meta"]


# --- run (the client wrapper) -------------------------------------------------

def test_run_injects_identity_from_the_context_only():
    fake = FakeLambda({"rows": [_row()], "meta": {"truncated": False, "site_count": 1}})
    env = ask_items.run({"site_id": SID, "limit": 999}, _ctx(fake), call_id="c1")
    [ev] = fake.events
    assert ev["mode"] == "items" and ev["sub"] == "sub-creator"
    assert ev["args"] == {"status": ["open", "in_progress", "blocked"], "limit": 200,
                          "site_id": SID}
    assert 0 < ev["deadline_s"] <= ask_items.MAX_DEADLINE_S
    assert env["id"] == "c1" and len(env["items"]) == 1
    assert env["meta"]["clamped"] == {"limit": 200} and env["meta"]["site_count"] == 1


def test_run_bad_args_never_invokes():
    fake = FakeLambda()
    env = ask_items.run({"sub": "someone-else"}, _ctx(fake))
    assert fake.events == [] and env["meta"] == {"error": "bad_args", "field": "sub"}


def test_run_out_of_time_never_invokes():
    fake = FakeLambda()
    env = ask_items.run({}, _ctx(fake, deadline=999.0))
    assert fake.events == [] and env["meta"] == {"error": "timeout"}


@pytest.mark.parametrize("fake", [FakeLambda(function_error="Unhandled"),
                                  FakeLambda(exc=RuntimeError("throttled"))])
def test_run_failures_are_upstream_never_raise(fake):
    env = ask_items.run({}, _ctx(fake))
    assert env == {"tool": "list_items", "id": "items", "items": [],
                   "meta": {"error": "upstream"}}


def test_run_passes_rag_search_errors_through():
    fake = FakeLambda({"rows": [], "error": "company_inactive",
                       "meta": {"error": "company_inactive"}})
    assert ask_items.run({}, _ctx(fake))["meta"]["error"] == "company_inactive"


def test_run_logs_no_query_text(caplog):
    caplog.set_level("INFO")
    fake = FakeLambda({"rows": [_row()], "meta": {}})
    ask_items.run({"query": "secret words"}, _ctx(fake))
    assert "secret words" not in caplog.text and "Fix the fire door" not in caplog.text


# --- Ask is unchanged ----------------------------------------------------------

def test_items_tool_is_not_an_ask_tool(monkeypatch):
    import project_doc_flag
    monkeypatch.setattr(project_doc_flag, "on", lambda: True)
    assert ask_items.TOOL_NAME not in {t["name"] for t in ask_tools.TOOLS}
    assert ask_items.TOOL_NAME not in {t["name"] for t in ask_tools.offered_tools()}
    _, err = ask_tools.validate_call({"name": ask_items.TOOL_NAME, "args": {}}, None)
    assert err == {"error": "bad_args", "field": "name"}
