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
import shutil
import subprocess

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


def test_the_chain_backstop_and_the_run_window_fit_the_timeout(fn):
    """What this pins, and what it does not: LLM_CHAIN_BUDGET_SECONDS is measured per call
    from that call's start (llm_utils), so `chain < Timeout - store reserve` only bounds ONE
    call made without a deadline. A call started late is kept inside the Lambda by the
    deadline every stage passes (ctx.left / ctx.deadline_for, pinned in
    test_action_agent_handler), and that window itself must leave the store reserve."""
    import lambda_action_agent as agent
    env = _env(fn)
    timeout = _props(fn)["Timeout"]
    chain = int(env["LLM_CHAIN_BUDGET_SECONDS"])
    assert chain == 780
    assert chain < timeout - agent.STORE_RESERVE_S          # a no-deadline call, alone
    assert int(env["LLM_HTTP_TIMEOUT"]) < chain              # one attempt < its chain
    # the stage deadlines all end by TARGET_S after the claim, before the store reserve
    assert agent.TARGET_S + agent.STORE_RESERVE_S < timeout


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
    # deploy-prod never opts in: prod wiring is the owner's step (spec D3)
    assert "WIRE_ACTION_AGENT" not in prod


# ---- prod wiring is an owner step (spec D3; fix round 1) --------------------------------

_BASH = shutil.which("bash")


def _bash_works():
    if not _BASH:
        return False
    try:
        return subprocess.run([_BASH, "-c", "echo ok"], capture_output=True, text=True,
                              timeout=20).stdout.strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return False


needs_bash = pytest.mark.skipif(not _bash_works(), reason="no working bash")
# The whole-script runs need jq (the script's JSON tool). CI's ubuntu runner has it; a
# Windows box without it skips them -- the mode decision above still runs there.
needs_jq = pytest.mark.skipif(not (_bash_works() and shutil.which("jq")), reason="no jq")


def _mode_function():
    text = open(WIRE, encoding="utf-8").read()
    m = re.search(r"^action_agent_mode\(\) \{.*?^\}\n", text, re.S | re.M)
    assert m, "the decision is a named function so it can be tested alone"
    return m.group(0)


@needs_bash
@pytest.mark.parametrize("stage, var, expected", [
    ("test", "", "add"), ("test", "false", "add"), ("test", "true", "add"),
    ("prod", "", "keep"), ("prod", "false", "keep"), ("prod", "TRUE", "keep"),
    ("prod", "true", "add"),
])
def test_only_test_or_an_explicit_opt_in_adds_the_rule(stage, var, expected):
    env = {k: v for k, v in os.environ.items() if k != "WIRE_ACTION_AGENT"}
    if var:
        env["WIRE_ACTION_AGENT"] = var
    out = subprocess.run([_BASH, "-c", _mode_function() + f'action_agent_mode "{stage}"'],
                         capture_output=True, text=True, env=env, timeout=20)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected


_FAKE_AWS = r"""#!/usr/bin/env bash
case "$1 $2" in
  "sts get-caller-identity") echo 123456789012 ;;
  "lambda get-function")
    for f in $FAKE_FNS; do [ "$4" = "$f" ] && exit 0; done; exit 254 ;;
  "s3api get-bucket-notification-configuration") cat "$FAKE_CURRENT" ;;
  "lambda add-permission") echo "$4" >> "$FAKE_PERMS" ;;
  "s3api put-bucket-notification-configuration")
    while [ $# -gt 0 ]; do
      if [ "$1" = "--notification-configuration" ]; then printf '%s' "$2" > "$FAKE_OUT"; fi
      shift
    done ;;
  *) echo "unexpected aws $*" >&2; exit 99 ;;
esac
"""

OWNER_RULE = {"Id": "fs-action-agent",
              "LambdaFunctionArn": "arn:aws:lambda:r:1:function:fieldsight-prod-action-agent",
              "Events": ["s3:ObjectCreated:*"],
              "Filter": {"Key": {"FilterRules": [
                  {"Name": "prefix", "Value": "action_pack_requests/"},
                  {"Name": "suffix", "Value": ".json"}]}}}


def _run_wire(tmp_path, stage, *, current=None, fns=("action-agent",), opt_in=None):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    aws = bindir / "aws"
    aws.write_text(_FAKE_AWS, encoding="utf-8", newline="\n")
    aws.chmod(0o755)
    cur = tmp_path / "current.json"
    cur.write_text(json.dumps(current or {}), encoding="utf-8")
    out, perms = tmp_path / "out.json", tmp_path / "perms.txt"
    perms.write_text("", encoding="utf-8")
    prefix = f"fieldsight-{stage}"
    env = {k: v for k, v in os.environ.items() if k not in ("WIRE_ACTION_AGENT", "RETIRE_IDS")}
    env.update(PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}", FAKE_CURRENT=str(cur),
               FAKE_OUT=str(out), FAKE_PERMS=str(perms),
               FAKE_FNS=" ".join(f"{prefix}-{f}" for f in fns))
    if opt_in is not None:
        env["WIRE_ACTION_AGENT"] = opt_in
    res = subprocess.run([_BASH, WIRE, "bkt", stage, "r", "--apply"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    cfg = json.loads(out.read_text(encoding="utf-8"))
    rules = {r["Id"]: r for r in cfg.get("LambdaFunctionConfigurations", [])}
    return rules, perms.read_text(encoding="utf-8").split()


@needs_jq
def test_test_stage_wires_the_agent(tmp_path):
    rules, perms = _run_wire(tmp_path, "test")
    assert rules["fs-action-agent"]["LambdaFunctionArn"].endswith(
        ":function:fieldsight-test-action-agent")
    assert "fieldsight-test-action-agent" in perms


@needs_jq
def test_prod_without_the_opt_in_does_not_add_it(tmp_path):
    rules, perms = _run_wire(tmp_path, "prod")
    assert "fs-action-agent" not in rules
    assert "fieldsight-prod-action-agent" not in perms


@needs_jq
def test_prod_without_the_opt_in_keeps_an_owner_wired_rule(tmp_path):
    current = {"LambdaFunctionConfigurations": [OWNER_RULE, {
        "Id": "fs-vad-wav", "LambdaFunctionArn": "x", "Events": ["s3:ObjectCreated:*"]}]}
    rules, _ = _run_wire(tmp_path, "prod", current=current)
    assert rules["fs-action-agent"] == OWNER_RULE
    assert "fs-vad-wav" not in rules          # the rest of fs-* is still replaced as before


@needs_jq
def test_prod_with_the_opt_in_adds_it(tmp_path):
    rules, perms = _run_wire(tmp_path, "prod", opt_in="true")
    assert rules["fs-action-agent"]["LambdaFunctionArn"].endswith(
        ":function:fieldsight-prod-action-agent")
    assert "fieldsight-prod-action-agent" in perms


@needs_jq
def test_a_kept_rule_is_dropped_once_the_function_is_gone(tmp_path):
    rules, _ = _run_wire(tmp_path, "prod", current={"LambdaFunctionConfigurations": [OWNER_RULE]},
                         fns=())
    assert "fs-action-agent" not in rules
