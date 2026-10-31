"""Unit: ActionAgentFunction is wired end to end, or not at all (spec 2026-11-01 §4.1, §4.2,
§14 D3, S1, S8, S16; Task 10).

A worker with no Events whose wire-s3-events.sh rule is missing deploys green and never
fires; a missing ListBucket turns "no cancel marker" into a 403 that fails every run; a retry
would re-run a paid run; and a flag that reaches org-api but not the worker makes the two
disagree about whether packs exist.
"""
import io
import json
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WIRE = os.path.join(REPO, "scripts", "wire-s3-events.sh")
WORKFLOWS = {"TEST": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
             "PROD": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")}

_yaml = pytest.importorskip("yaml")


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
def tpl():
    with io.open(TEMPLATE, encoding="utf-8") as f:
        return _yaml.load(f.read(), Loader=_Loader)


@pytest.fixture(scope="module")
def fn(tpl):
    return tpl["Resources"]["ActionAgentFunction"]


def _props(fn):
    return fn["Properties"]


def _env(fn):
    return _props(fn)["Environment"]["Variables"]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _statements(fn):
    for pol in _props(fn).get("Policies", []):
        if isinstance(pol, dict) and "Statement" in pol:
            for st in _as_list(pol["Statement"]):
                yield st


def _sub(r):
    return r.get("Sub") if isinstance(r, dict) else r


def test_shape(fn):
    p = _props(fn)
    assert fn["Condition"] == "HasOrgApi"
    assert p["Handler"] == "lambda_action_agent.lambda_handler"
    assert p["Timeout"] == 900
    assert p["MemorySize"] >= 1024
    assert p["ReservedConcurrentExecutions"] == 3
    assert "VpcConfig" not in p, "it calls models and the web; in the VPC it would hang"
    assert "Events" not in p, "S3 events on the data bucket are wired by script (BUG-33)"
    assert p["EventInvokeConfig"]["MaximumRetryAttempts"] == 0
    assert 0 < p["EventInvokeConfig"]["MaximumEventAgeInSeconds"] <= 3600
    assert "action-agent" in json.dumps(p["FunctionName"])


def test_the_chain_budget_leaves_the_store_reserve(fn):
    import lambda_action_agent as agent
    env = _env(fn)
    chain = int(env["LLM_CHAIN_BUDGET_SECONDS"])
    assert chain == 780
    assert chain < _props(fn)["Timeout"] - agent.STORE_RESERVE_S
    # one model attempt never outlives the chain, nor the run's target window
    assert int(env["LLM_HTTP_TIMEOUT"]) < chain
    assert agent.TARGET_S + agent.STORE_RESERVE_S < _props(fn)["Timeout"]


def test_env_names_the_switch_the_models_the_hops_and_the_buckets(fn):
    env = _env(fn)
    assert env["ACTION_PACKS"] == {"Ref": "ActionPacks"}
    assert env["ACTION_WRITER_MODEL"] == {"Ref": "AskJudgeModel"}
    # empty -> the cheap model in code (web_answer.CHEAP_MODEL): no model id invented here
    assert env["ACTION_VERIFIER_MODEL"] == ""
    assert env["ACTION_VERIFIER_FALLBACKS"] == ""
    assert int(env["ACTION_TOKEN_CAP"]) > 0
    assert env["RAG_SEARCH_FUNCTION"] == {"Ref": "RagSearchFunction"}
    assert env["ORG_API_FUNCTION"] == {"Ref": "OrgApiFunction"}
    assert env["S3_BUCKET"] == {"Ref": "DataBucketName"}
    assert env["LAKE_BUCKET"] == {"Ref": "IngestBucketName"}


# The model/tool plumbing the worker runs (llm_utils, llm_tools_client, web_answer, ask_tools
# embeds and rerank) must be configured exactly as on the Ask worker it borrows from.
MIRRORED = ("ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL",
            "DASHSCOPE_EMBED_MODEL", "DASHSCOPE_EMBED_DIM", "ENABLE_RERANK",
            "DASHSCOPE_RERANK_TIMEOUT_SECONDS", "RERANK_CANDIDATE_K", "CLAUDE_MODEL",
            "LLM_PROVIDER", "LLM_TEMPERATURE", "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL",
            "LLM_REASONING_EFFORT", "CORROBORATION_API_KEY", "CORROBORATION_PROVIDER_ORDER",
            "ENABLE_WEB_ANSWER", "PROJECT_DOCUMENTS", "ASK_JUDGE_MODEL",
            "ASK_JUDGE_FALLBACKS", "ASK_JUDGE_PROVIDER", "ASK_JUDGE_REASONING")


@pytest.mark.parametrize("name", MIRRORED)
def test_model_and_tool_env_matches_the_ask_worker(tpl, fn, name):
    worker = _env(tpl["Resources"]["AskVoiceWorkerFunction"])
    assert _env(fn)[name] == worker[name], name


def test_no_voice_only_settings_leak_in(fn):
    env = _env(fn)
    for name in env:
        assert not name.startswith(("ELEVENLABS_", "ASK_STT", "ASK_TTS", "VOICE_",
                                    "ASK_VOICE")), name


def test_the_switch_reaches_exactly_org_api_and_the_worker(tpl):
    """Env parity for ACTION_PACKS: the routes, the internal tasks and the worker read one
    parameter, and nothing else carries it."""
    carriers = {name for name, res in tpl["Resources"].items()
                if res.get("Type") == "AWS::Serverless::Function"
                and "ACTION_PACKS" in (((res.get("Properties") or {}).get("Environment")
                                        or {}).get("Variables") or {})}
    assert carriers == {"OrgApiFunction", "ActionAgentFunction"}
    for name in carriers:
        assert _env(tpl["Resources"][name])["ACTION_PACKS"] == {"Ref": "ActionPacks"}


def test_the_parameter_defaults_off(tpl):
    p = tpl["Parameters"]["ActionPacks"]
    assert p["Default"] == "off" and p["AllowedValues"] == ["off", "internal", "on"]


def _grants(fn, bucket):
    out = set()
    marker = "${%s}/" % bucket
    for st in _statements(fn):
        if st.get("Effect") != "Allow":
            continue
        for action in _as_list(st.get("Action")):
            for res in _as_list(st.get("Resource")):
                r = _sub(res)
                if isinstance(r, str) and marker in r:
                    out.add((action, r.split(marker, 1)[1]))
    return out


def test_s3_grants_are_scoped_to_the_two_prefixes(fn):
    data = _grants(fn, "DataBucketName")
    lake = _grants(fn, "IngestBucketName")
    assert data == {("s3:GetObject", "action_pack_requests/*")}
    assert lake == {("s3:PutObject", "action_packs/*")}


def test_a_missing_cancel_marker_reads_404_not_403(fn):
    lists = []
    for st in _statements(fn):
        if "s3:ListBucket" in _as_list(st.get("Action")):
            res = [_sub(r) for r in _as_list(st["Resource"])]
            assert res == ["arn:aws:s3:::${DataBucketName}"]
            lists += _as_list(st["Condition"]["StringLike"]["s3:prefix"])
    assert lists == ["action_pack_requests/*"]


def test_no_wildcard_s3_or_extra_actions(fn):
    for st in _statements(fn):
        for action in _as_list(st.get("Action")):
            assert action in ("s3:GetObject", "s3:PutObject", "s3:ListBucket",
                              "lambda:InvokeFunction"), action
        for res in _as_list(st.get("Resource")):
            assert "*" != _sub(res)


def test_invoke_is_exactly_org_api_and_rag_search(fn):
    """Exact ARNs, not LambdaInvokePolicy: that grants `<name>*`."""
    targets = []
    for st in _statements(fn):
        if "lambda:InvokeFunction" in _as_list(st.get("Action")):
            targets += [r.get("GetAtt") for r in _as_list(st["Resource"])]
    assert sorted(targets) == ["OrgApiFunction.Arn", "RagSearchFunction.Arn"]
    assert not any("LambdaInvokePolicy" in p for p in _props(fn)["Policies"]
                   if isinstance(p, dict))


def test_the_dashscope_layer_is_attached_like_the_ask_worker(tpl, fn):
    assert _props(fn)["Layers"] == _props(tpl["Resources"]["AskVoiceWorkerFunction"])["Layers"]


# ---- wire-s3-events.sh --------------------------------------------------------------

def test_the_wire_script_routes_requests_to_the_agent_only_when_it_exists():
    text = open(WIRE, encoding="utf-8").read()
    assert 'ACTION_AGENT_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-action-agent"' in text
    block = re.search(r'if fn_exists "\$\{PREFIX\}-action-agent"; then\n(.*?)\nfi\n', text, re.S)
    assert block, "the action-agent rule must be inside its own fn_exists guard"
    body = block.group(1)
    assert 'WIRE_FNS+=("${PREFIX}-action-agent")' in body
    assert '"Id":"fs-action-agent"' in body
    assert '"Name":"prefix","Value":"action_pack_requests/"' in body
    assert '"Name":"suffix","Value":".json"' in body
    assert '--arg arn "$ACTION_AGENT_ARN"' in body


def test_test_deploy_applies_the_wiring_and_prod_keeps_its_gate():
    test = open(WORKFLOWS["TEST"], encoding="utf-8").read()
    assert re.search(r"bash scripts/wire-s3-events.sh fieldsight-data-test-\S+ test .+ --apply$",
                     test, re.M)
    prod = open(WORKFLOWS["PROD"], encoding="utf-8").read()
    assert 'if [ "${{ vars.PROD_WIRE_LAKE }}" = "true" ]; then' in prod
