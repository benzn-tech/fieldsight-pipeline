"""Action packs, first live TEST runs (2026-11-01): run_trace.tool_errors was 4 of 20 and 1 of
22 with nothing in the log saying why. Every logged rag-search hop said err=None: the errors
were calls refused by argument validation before any hop (ask_tools.validate_call), which
logged nothing.

Now: the trace counts each error by tool, code and field (ids/codes only), ask_tools logs a
refused call's tool and field, the round model reads the field it got wrong, null-valued and
site arguments never reach validation, and the tool catalog tells the model each argument's
type and which are required. Fakes only."""
import json
import logging

import pytest

import action_pack_stages as st
import action_pack_view
import ask_tools
from tests.unit.test_action_pack_stages import (SITE, TODAY, WRITER, Model, Tools, deps,
                                                make_ctx)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACTION_WRITER_MODEL", WRITER)
    monkeypatch.delenv("ACTION_TOKEN_CAP", raising=False)


class ValidatingTools(Tools):
    """Tools whose execute runs the REAL argument validation first (what ask_tools does
    before any rag-search hop) and answers a refused call the way ask_tools does."""

    def execute(self, calls, tctx):
        out, ok = [None] * len(calls), []
        for i, c in enumerate(calls):
            _, err = ask_tools.validate_call(c, TODAY)
            if err:
                out[i] = ask_tools._bad(c, str(i), dict(err))
            else:
                ok.append(i)
        good = super().execute([calls[i] for i in ok], tctx) if ok else []
        for i, env in zip(ok, good):
            out[i] = env
        return out


def run_rounds(rounds, tools=None):
    tools = tools or ValidatingTools()
    payload = st.run(make_ctx(), deps(model=Model(rounds=rounds), tools=tools))
    return payload, tools


def test_tool_errors_are_counted_by_tool_code_and_field():
    rounds = [{"calls": [
        {"tool": "search_records", "args": {"query": "pour", "k": 50}},
        {"tool": "search_project_documents", "args": {"query": "curing",
                                                      "date_from": "2026-10-01"}},
        {"tool": "latest_activity", "args": {"query": "pour", "since": "2026-10-01"}},
        {"tool": "day_overview", "args": {"date_from": "2026-10-01"}}], "done": True}]
    payload, _ = run_rounds(rounds)
    trace = payload["run_trace"]
    assert trace["tool_errors"] == 3
    assert trace["tool_error_reasons"] == {
        "search_records__bad_args__k": 1,
        "search_project_documents__bad_args__date_from": 1,
        # a model-made argument name never reaches the trace
        "latest_activity__bad_args__other": 1}
    assert action_pack_view.fail_trace_error({"tool_error_reasons":
                                              trace["tool_error_reasons"]}) is None
    assert "tool_error_reasons" in action_pack_view.public_trace(trace)


def test_reason_keys_are_trace_safe_for_any_envelope():
    run = st.Run(ctx=make_ctx(), deps=deps())
    run.add(2, {"tool": "Weird Tool!", "meta": {"error": "Some Free Text!", "field": "x y"}})
    run.add(2, {"tool": "web_search", "meta": {"error": "already_searched"}})
    run.add(2, {"tool": "list_items", "meta": {"error": "upstream"}})
    assert run.trace["tool_error_reasons"] == {"other__other__other": 1,
                                               "web_search__already_searched": 1,
                                               "list_items__upstream": 1}
    assert action_pack_view.fail_trace_error(run.trace) is None


def test_null_arguments_are_dropped_before_validation():
    rounds = [{"calls": [{"tool": "search_records",
                          "args": {"query": "pour", "date_from": None, "date_to": None,
                                   "person": None, "k": None}},
                         {"tool": "list_items", "args": {"query": None, "task_no": None}}],
               "done": True}]
    payload, tools = run_rounds(rounds)
    assert payload["run_trace"].get("tool_errors", 0) == 0
    assert tools.calls[-1] == [{"name": "search_records", "args": {"query": "pour",
                                                                    "site": SITE}}]
    assert tools.items_calls[-1] == {"site_id": SITE}


def test_site_arguments_never_reach_validation():
    rounds = [{"calls": [{"tool": "search_records",
                          "args": {"query": "pour", "site_id": "elsewhere"}},
                         {"tool": "list_items", "args": {"site": "Harbour St"}}],
               "done": True}]
    payload, tools = run_rounds(rounds)
    assert payload["run_trace"].get("tool_errors", 0) == 0
    assert tools.calls[-1] == [{"name": "search_records", "args": {"query": "pour",
                                                                    "site": SITE}}]
    assert tools.items_calls[-1] == {"site_id": SITE}


def test_the_round_model_reads_which_argument_was_wrong():
    rounds = [{"calls": [{"tool": "search_records", "args": {"query": "pour", "k": 50}}],
               "done": False},
              {"calls": [], "done": True}]
    model = Model(rounds=rounds)
    st.run(make_ctx(), deps(model=model, tools=ValidatingTools()))
    second = [c["prompt"] for c in model.calls if c["caller"] == "action_round"][1]
    assert "error bad_args k" in second


def test_the_catalog_gives_types_and_required_arguments():
    run = st.Run(ctx=make_ctx(), deps=deps())
    run.plan = st.validate_plan(json.loads(json.dumps({
        "reading": "r", "title": "t", "language": "en", "site_matches": True,
        "date_from": "2026-10-10", "date_to": "2026-10-10", "record_queries": [],
        "document_queries": [], "web_queries": []})), TODAY)[0]
    cat = st._tool_catalog(run)
    sr = next(ln for ln in cat.splitlines() if ln.startswith("- search_records:"))
    assert '"query": "string, required"' in sr
    assert '"k": "integer 1-12"' in sr
    assert '"date_from": "YYYY-MM-DD"' in sr
    assert "site" not in json.loads(sr.split("args: ", 1)[1])
    items = next(ln for ln in cat.splitlines() if ln.startswith("- list_items:"))
    assert "site_id" not in items and '"limit": "integer 1-200"' in items


def test_ask_tools_logs_a_refused_call_by_name_and_field_only(caplog):
    class Ctx:
        today = TODAY
        deadline = 1e9
        refs = ask_tools.RefAssigner()

        @staticmethod
        def clock():
            return 0.0
    caplog.set_level(logging.INFO)
    envs = ask_tools.execute([{"name": "search_records",
                               "args": {"query": "SECRET words", "k": 99}},
                              {"name": "latest_activity",
                               "args": {"query": "x", "SECRET_KEY_NAME": 1}}], Ctx())
    assert [e["meta"] for e in envs] == [{"error": "bad_args", "field": "k"},
                                         {"error": "bad_args", "field": "SECRET_KEY_NAME"}]
    text = caplog.text
    assert "ask tool refused name=search_records field=k" in text
    assert "ask tool refused name=latest_activity field=other" in text
    assert "SECRET" not in text
