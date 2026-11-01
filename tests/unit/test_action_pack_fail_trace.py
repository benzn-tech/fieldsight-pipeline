"""A failed pack keeps its trace counts (spec §4.5; Task 11 fix round 1): the stages attach
the counts gathered so far to the exception, the handler sends them with action_pack_fail,
org-api validates them (counts / bools / short codes only, size-capped) and the repository
stores them. A malformed trace is dropped -- the pack still fails."""
import json

import pytest

import action_pack_stages as st
import action_pack_view
import lambda_action_agent as agent
import lambda_org_api as org
from tests.unit.test_action_agent_handler import (FakeOrgApi, _key, _run,  # noqa: F401
                                                  _s3_with_request, env)
from tests.unit.test_action_pack_stages import Model, deps, make_ctx, plan_obj
from tests.unit.test_org_api_action_packs import PACK, task_env  # noqa: F401


# ---- the validator -----------------------------------------------------------------------

@pytest.mark.parametrize("trace", [
    {"rounds": 3, "tool_calls": 7, "truncated": True, "tokens": 1234, "stage": "gather",
     "truncated_reasons": ["time", "round_invalid"], "ms": {"gather": 1200, "total": 3},
     "web": {"web_searches": 1}, "ratio": 0.5, "none": None},
    {},
])
def test_counts_only_traces_pass(trace):
    assert action_pack_view.fail_trace_error(trace) is None


@pytest.mark.parametrize("trace,code", [
    ([], "not_object"),
    ({"reading": "The pour on 10-10 at grid C4"}, "value"),      # free text
    ({"x": "A"}, "value"),
    ({"bad key": 1}, "key"),
    ({"k" * 65: 1}, "key"),
    ({"a": {"b": {"c": 1}}}, "value"),                             # two levels deep
    ({"a": [1] * 17}, "value"),
    ({"a": float("nan")}, "value"),
    ({f"k{i}": i for i in range(2000)}, "size"),
])
def test_anything_else_is_refused(trace, code):
    assert action_pack_view.fail_trace_error(trace) == code


# ---- org-api ---------------------------------------------------------------------------

def test_the_fail_task_stores_a_valid_trace(task_env, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(org.action_packs, "fail",
                        lambda conn, pid, reason, run_trace=None: seen.append(run_trace)
                        or True)
    trace = {"rounds": 2, "truncated": False}
    assert org.lambda_handler({"task": "action_pack_fail", "pack_id": PACK,
                               "reason": "model", "run_trace": trace}, None) == {"ok": True}
    assert seen == [trace]


def test_a_malformed_trace_is_dropped_but_the_pack_still_fails(task_env,  # noqa: F811
                                                               monkeypatch):
    seen = []
    monkeypatch.setattr(org.action_packs, "fail",
                        lambda conn, pid, reason, run_trace=None: seen.append(run_trace)
                        or True)
    assert org.lambda_handler({"task": "action_pack_fail", "pack_id": PACK,
                               "reason": "model", "run_trace": {"text": "free text here"}},
                              None) == {"ok": True, "trace_dropped": "value"}
    assert seen == [None]


# ---- handler -----------------------------------------------------------------------------

def test_the_handler_sends_the_counts_gathered_so_far(env):  # noqa: F811
    lam = FakeOrgApi()

    def runner(ctx):
        exc = agent.ModelFailure("write")
        exc.run_trace = {"rounds": 3, "stage": "write"}
        raise exc
    assert _run(_key(), _s3_with_request(), lam, runner) == "failed:model"
    [fail] = lam.of("action_pack_fail")
    assert fail["run_trace"] == {"rounds": 3, "stage": "write"}


def test_no_trace_is_sent_when_there_is_none(env):  # noqa: F811
    lam = FakeOrgApi()

    def runner(ctx):
        raise agent.ModelFailure()
    _run(_key(), _s3_with_request(), lam, runner)
    assert "run_trace" not in lam.of("action_pack_fail")[0]


# ---- stages ------------------------------------------------------------------------------

def test_a_stage_failure_carries_valid_counts(monkeypatch):
    monkeypatch.setenv("ACTION_WRITER_MODEL", "w")
    model = Model(write=["nope", "nope"])
    with pytest.raises(agent.ModelFailure) as info:
        st.run(make_ctx(), deps(model=model))
    trace = info.value.run_trace
    assert trace["stage"] == "write" and trace["rounds"] >= 1 and trace["model_calls"] >= 3
    assert action_pack_view.fail_trace_error(trace) is None
    assert "nope" not in json.dumps(trace)


def test_a_plan_failure_carries_the_plan_stage(monkeypatch):
    monkeypatch.setenv("ACTION_WRITER_MODEL", "w")
    with pytest.raises(agent.ModelFailure) as info:
        st.run(make_ctx(), deps(model=Model(plan=[{"x": 1}, {"x": 1}])))
    assert info.value.run_trace["stage"] == "plan"
    assert plan_obj()                                      # fixture import kept honest
