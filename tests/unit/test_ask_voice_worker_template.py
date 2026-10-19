"""Unit: the template wiring of the two-phase voice Ask worker (spec sections 4, 5, 7, 8, 10).

These pin the join between the template and the code: the worker is a copy of
AskAgent's environment with three deliberate budget overrides, its budgets fit
under its own Timeout, it never retries, and every S3 grant is scoped to the
`ask_turns/` prefix of the EXISTING data bucket.
"""
import io
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
PROD_WORKFLOW = os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")
TEST_WORKFLOW = os.path.join(REPO, ".github", "workflows", "deploy.yml")

_yaml = pytest.importorskip("yaml")

TTS_SECONDS = 10  # fixed in code; the worker must leave room for it
OVERRIDES = {"ASK_DEADLINE_SECONDS": "45", "WEB_ANSWER_HARD_STOP": "45", "LLM_HTTP_TIMEOUT": "40"}
# AskAgent-only: the worker cannot reference itself, and restatement runs in AskAgent.
AGENT_ONLY = {"ASK_VOICE_WORKER_FUNCTION", "ASK_VOICE_RESTATE", "VOICE_RESTATE_MODEL"}


class _Loader(_yaml.SafeLoader):
    pass


def _tagged(loader, suffix, node):
    if isinstance(node, _yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, _yaml.MappingNode):
        return {suffix: loader.construct_mapping(node, deep=True)}
    return {suffix: loader.construct_sequence(node, deep=True)}


_Loader.add_multi_constructor("!", _tagged)


@pytest.fixture(scope="module")
def resources():
    with io.open(TEMPLATE, encoding="utf-8") as f:
        return _yaml.load(f.read(), Loader=_Loader)["Resources"]


def _props(resources, name):
    return resources[name]["Properties"]


def _env(resources, name):
    return _props(resources, name)["Environment"]["Variables"]


def _statements(resources, name):
    out = []
    for pol in _props(resources, name)["Policies"]:
        if "Statement" in pol:
            out.extend(pol["Statement"])
    return out


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _s3_resources(resources, name):
    res = []
    for st in _statements(resources, name):
        res.extend((a, r, st) for a in _as_list(st["Action"]) for r in _as_list(st["Resource"]))
    return res


def _sub(r):
    return r.get("Sub") if isinstance(r, dict) else r


def test_worker_function_shape(resources):
    p = _props(resources, "AskVoiceWorkerFunction")
    assert p["Handler"] == "ask_voice_turn.worker_handler"
    assert p["Timeout"] == 60 and p["MemorySize"] == 256
    assert p["Layers"] == [{"Ref": "DashScopeLayer"}]
    assert "ask-voice-worker" in str(p["FunctionName"])


def test_worker_env_is_askagents_plus_overrides(resources):
    agent = _env(resources, "AskAgentFunction")
    worker = _env(resources, "AskVoiceWorkerFunction")
    missing = set(agent) - AGENT_ONLY - set(worker)
    assert not missing, "worker lacks AskAgent env keys: %s" % sorted(missing)
    for k, v in agent.items():
        if k in AGENT_ONLY or k in OVERRIDES:
            continue
        assert worker[k] == v, "%s differs from AskAgent's expression" % k
    for k, v in OVERRIDES.items():
        assert str(worker[k]) == v
    assert worker["ASK_TURN_BUCKET"] == {"Ref": "DataBucketName"}
    assert worker["VOICE_WEB_ENABLED"] == {"Ref": "VoiceWebEnabled"}


def test_worker_budgets_fit_under_its_timeout(resources):
    p = _props(resources, "AskVoiceWorkerFunction")
    env = _env(resources, "AskVoiceWorkerFunction")
    for k in ("ASK_DEADLINE_SECONDS", "WEB_ANSWER_HARD_STOP"):
        assert float(env[k]) + TTS_SECONDS < p["Timeout"], k
    assert float(env["LLM_HTTP_TIMEOUT"]) < float(env["ASK_DEADLINE_SECONDS"])


def test_worker_never_retries(resources):
    cfg = _props(resources, "AskVoiceWorkerFunction")["EventInvokeConfig"]
    assert cfg["MaximumRetryAttempts"] == 0
    assert cfg["MaximumEventAgeInSeconds"] == 120


def test_askagent_is_wired_to_the_worker(resources):
    env = _env(resources, "AskAgentFunction")
    assert env["ASK_VOICE_WORKER_FUNCTION"] == {"Ref": "AskVoiceWorkerFunction"}
    assert env["ASK_TURN_BUCKET"] == {"Ref": "DataBucketName"}
    assert env["ASK_VOICE_RESTATE"] == {"Ref": "AskVoiceRestate"}
    assert env["VOICE_RESTATE_MODEL"] == "meta-llama/llama-3.1-8b-instruct"
    invokes = [p["LambdaInvokePolicy"]["FunctionName"]
               for p in _props(resources, "AskAgentFunction")["Policies"]
               if "LambdaInvokePolicy" in p]
    assert {"Ref": "AskVoiceWorkerFunction"} in invokes


def test_apifunction_has_the_turn_bucket(resources):
    assert _env(resources, "ApiFunction")["ASK_TURN_BUCKET"] == {"Ref": "DataBucketName"}


def test_worker_invokes_rag_and_audit(resources):
    invokes = [p["LambdaInvokePolicy"]["FunctionName"]
               for p in _props(resources, "AskVoiceWorkerFunction")["Policies"]
               if "LambdaInvokePolicy" in p]
    assert {"Ref": "RagSearchFunction"} in invokes
    assert {"Ref": "VoiceAuditFunction"} in invokes


PREFIX_ACTIONS = {
    # GetObject: voice_start reads convo.json / offer.json (spec 2026-10-15
    # voice offer and memory, 4.5). Without it every read is a 403.
    "AskAgentFunction": {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"},
    "AskVoiceWorkerFunction": {"s3:GetObject", "s3:PutObject"},
    "ApiFunction": {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"},
}


@pytest.mark.parametrize("fn", sorted(PREFIX_ACTIONS))
def test_object_grants_are_prefix_scoped(resources, fn):
    object_actions = set()
    for action, res, st in _s3_resources(resources, fn):
        if not action.startswith("s3:") or action == "s3:ListBucket":
            continue
        s = _sub(res)
        assert s == "arn:aws:s3:::${DataBucketName}/ask_turns/*", \
            "%s: %s resource %r is not prefix-scoped" % (fn, action, s)
        object_actions.add(action)
    assert PREFIX_ACTIONS[fn] <= object_actions
    assert object_actions <= PREFIX_ACTIONS[fn], "%s has extra S3 actions" % fn


@pytest.mark.parametrize("fn", sorted(PREFIX_ACTIONS))
def test_list_bucket_is_prefix_conditioned(resources, fn):
    """Without ListBucket a missing key is a 403, which the turn store cannot tell
    apart from a real IAM error."""
    found = [st for st in _statements(resources, fn) if "s3:ListBucket" in _as_list(st["Action"])
             and "ask_turns" in str(st.get("Condition"))]
    assert len(found) == 1, fn
    st = found[0]
    assert _sub(st["Resource"]) == "arn:aws:s3:::${DataBucketName}"
    assert _as_list(st["Condition"]["StringLike"]["s3:prefix"]) == ["ask_turns/*"]


def test_no_bare_bucket_wildcard_and_no_new_bucket(resources):
    for fn in PREFIX_ACTIONS:
        for _, res, _st in _s3_resources(resources, fn):
            assert not re.search(r"\}/\*$", str(_sub(res))), fn
    assert "BucketEncryption" not in io.open(TEMPLATE, encoding="utf-8").read().split(
        "AskVoiceWorkerFunction:")[1].split("DashScopeLayer:")[0]


def test_flags_are_false_by_default_string_booleans():
    with io.open(TEMPLATE, encoding="utf-8") as f:
        params = _yaml.load(f.read(), Loader=_Loader)["Parameters"]
    for name in ("AskVoiceRestate", "VoiceWebEnabled", "AskVoiceConvo"):
        assert params[name]["Type"] == "String"
        assert params[name]["Default"] == "false"
        assert params[name]["AllowedValues"] == ["true", "false"]
        assert params[name]["Description"]


def test_both_workflows_pass_the_flags_defaulting_off():
    """Prod was wired on 2026-10-19 so the release is a repo-var flip, not a code
    change (the owner chose the tested TEST setup for prod). An unset PROD_ var
    still deploys the template default, false."""
    prod = io.open(PROD_WORKFLOW, encoding="utf-8").read()
    test = io.open(TEST_WORKFLOW, encoding="utf-8").read()
    for name in ("AskVoiceRestate", "VoiceWebEnabled", "AskVoiceConvo"):
        assert re.search(r'"%s=\$\{\{ vars\.TEST_\w+ \|\| \'false\' \}\}"' % name, test)
        assert re.search(r'"%s=\$\{\{ vars\.PROD_\w+ \|\| \'false\' \}\}"' % name, prod)


def test_the_convo_flag_reaches_both_lambdas_that_use_it(resources):
    # start runs in AskAgent, the memory writes in the worker: one flag, both env blocks.
    for fn in ("AskAgentFunction", "AskVoiceWorkerFunction"):
        assert _env(resources, fn)["ASK_VOICE_CONVO"] == {"Ref": "AskVoiceConvo"}, fn
