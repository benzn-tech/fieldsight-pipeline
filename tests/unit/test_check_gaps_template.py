"""Check done (step A) is wired in the template, or it is not wired.

Three ways this deploys green and does nothing, or fails the stack:
- an unconditional `!Ref CheckGapsFunction` from the Ask worker while the flag is
  off (the default): CloudFormation refuses a Ref to a resource it did not
  create. RagSearch's W1001 shape survives only because HasDb is always true;
  this flag is not.
- a missing ListBucket on a prefix this function reads: a missing key answers
  403, and `agent_turn_filter` raises on that by design.
- the DB hop outside the VPC, or the STT/LLM function inside it (no egress).

Spec: docs/superpowers/specs/2026-10-20-check-done-gaps-design.md
"""
import io
import os
import re

import pytest

_yaml = pytest.importorskip("yaml")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WORKFLOWS = {"TEST": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
             "PROD": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")}
COND = "ShouldCheckDoneGaps"
NEW = ("CheckGapsFunction", "CheckGapsDbFunction")


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
def doc():
    with io.open(TEMPLATE, encoding="utf-8") as f:
        return _yaml.load(f.read(), Loader=_Loader)


def _props(doc, name):
    return doc["Resources"][name]["Properties"]


def _env(doc, name):
    return _props(doc, name)["Environment"]["Variables"]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _statements(doc, name):
    for pol in _props(doc, name)["Policies"]:
        if isinstance(pol, dict) and "Statement" in pol:
            yield from pol["Statement"]


def _sub(r):
    return r.get("Sub") if isinstance(r, dict) else r


def test_the_flag_defaults_off_and_needs_the_database(doc):
    p = doc["Parameters"]["CheckDoneGaps"]
    assert p["Default"] == "false" and p["AllowedValues"] == ["true", "false"]
    cond = doc["Conditions"][COND]["And"]
    assert {"Condition": "HasDb"} in cond
    assert {"Equals": [{"Ref": "CheckDoneGaps"}, "true"]} in cond


@pytest.mark.parametrize("name", NEW)
def test_both_functions_exist_only_under_the_flag(doc, name):
    assert doc["Resources"][name]["Condition"] == COND


def test_the_stt_and_llm_function_is_outside_the_vpc(doc):
    p = _props(doc, "CheckGapsFunction")
    assert "VpcConfig" not in p
    assert p["Handler"] == "lambda_check_gaps.lambda_handler"
    assert p["Timeout"] == 35


def test_the_db_hop_is_inside_the_vpc_like_rag_search(doc):
    p, rag = _props(doc, "CheckGapsDbFunction"), _props(doc, "RagSearchFunction")
    assert p["Handler"] == "lambda_check_gaps_db.lambda_handler"
    assert p["VpcConfig"] == rag["VpcConfig"]
    assert p["Layers"] == [{"Ref": "PsycopgLayer"}]
    assert p["Timeout"] == 10
    for k in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        assert _env(doc, "CheckGapsDbFunction")[k] == _env(doc, "RagSearchFunction")[k]


def test_the_ask_key_and_the_workers_model_are_carried(doc):
    env, worker = _env(doc, "CheckGapsFunction"), _env(doc, "AskVoiceWorkerFunction")
    assert env["ELEVENLABS_ASK_API_KEY"] == {"Ref": "ElevenLabsAskApiKey"}
    # Never the recording pipeline's key: its credits running out stops recordings.
    assert "ELEVENLABS_API_KEY" not in env
    for k in ("ELEVENLABS_STT_MODEL", "ELEVENLABS_LANGUAGE", "LLM_PROVIDER", "LLM_TEMPERATURE",
              "QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL", "LLM_REASONING_EFFORT",
              "ANTHROPIC_API_KEY", "CLAUDE_MODEL"):
        assert env[k] == worker[k], k
    assert env["S3_BUCKET"] == {"Ref": "IngestBucketName"}
    assert env["CHECK_GAPS_DB_FUNCTION"] == {"Ref": "CheckGapsDbFunction"}
    assert int(env["LLM_HTTP_TIMEOUT"]) < 35 and int(env["LLM_CHAIN_BUDGET_SECONDS"]) < 35


READS = {"transcripts/*", "voice_ask/*", "audio_segments/*", "users/*/audio/*"}
LISTS = {"transcripts/*", "voice_ask/*", "users/*/audio/*"}


def test_it_reads_exactly_what_the_transcript_step_reads_and_writes_nothing(doc):
    gets, lists = set(), set()
    for st in _statements(doc, "CheckGapsFunction"):
        actions = _as_list(st["Action"])
        for a in actions:
            if a == "s3:ListBucket":
                assert _sub(st["Resource"]) == "arn:aws:s3:::${IngestBucketName}"
                lists |= set(_as_list(st["Condition"]["StringLike"]["s3:prefix"]))
            elif a.startswith("s3:"):
                assert a == "s3:GetObject", "read-only: %s" % a
                for r in _as_list(st["Resource"]):
                    s = _sub(r)
                    assert s.startswith("arn:aws:s3:::${IngestBucketName}/"), s
                    gets.add(s.split("}/", 1)[1])
    assert gets == READS
    assert lists == LISTS


def test_it_invokes_only_its_db_hop(doc):
    invokes = [p["LambdaInvokePolicy"]["FunctionName"] for p in _props(doc, "CheckGapsFunction")["Policies"]
               if isinstance(p, dict) and "LambdaInvokePolicy" in p]
    assert invokes == [{"Ref": "CheckGapsDbFunction"}]


def test_the_worker_reaches_it_only_when_it_exists(doc):
    env = _env(doc, "AskVoiceWorkerFunction")
    assert env["CHECK_GAPS_FUNCTION"] == {"If": [COND, {"Ref": "CheckGapsFunction"}, ""]}
    guarded = [p["If"] for p in _props(doc, "AskVoiceWorkerFunction")["Policies"]
               if isinstance(p, dict) and "If" in p]
    assert [COND, {"LambdaInvokePolicy": {"FunctionName": {"Ref": "CheckGapsFunction"}}},
            {"Ref": "AWS::NoValue"}] in guarded


def _unguarded_refs(node, names):
    """Refs to `names` not under an `!If [ShouldCheckDoneGaps, ...]`."""
    if isinstance(node, dict):
        if "If" in node and isinstance(node["If"], list) and node["If"] and node["If"][0] == COND:
            return []
        out = []
        for k, v in node.items():
            if k == "Ref" and v in names:
                out.append(v)
            elif k == "GetAtt" and str(v).split(".")[0] in names:
                out.append(v)
            elif k == "Sub" and any(re.search(r"\$\{%s[.}]" % n, str(v)) for n in names):
                out.append(v)
            else:
                out.extend(_unguarded_refs(v, names))
        return out
    if isinstance(node, list):
        return [r for v in node for r in _unguarded_refs(v, names)]
    return []


def test_no_resource_outside_the_flag_refers_to_the_new_functions(doc):
    bad = {}
    for name, res in doc["Resources"].items():
        if res.get("Condition") == COND:
            continue
        found = _unguarded_refs(res, set(NEW))
        if found:
            bad[name] = found
    assert bad == {}
    outputs = doc.get("Outputs") or {}
    assert not any(_unguarded_refs(o, set(NEW)) for o in outputs.values() if o.get("Condition") != COND)


@pytest.mark.parametrize("env", sorted(WORKFLOWS))
def test_both_workflows_pass_the_flag_defaulting_off(env):
    text = io.open(WORKFLOWS[env], encoding="utf-8").read()
    assert "\"CheckDoneGaps=${{ vars.%s_CHECK_DONE_GAPS || 'false' }}\"" % env in text
