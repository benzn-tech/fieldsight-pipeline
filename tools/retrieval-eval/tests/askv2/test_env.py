import json
import os

import pytest

from askv2 import env, harness

WORKER = {
    "ASK_JUDGE_MODEL": "deepseek/deepseek-v4.1-flash", "ASK_JUDGE_FALLBACKS": "a,b",
    "ASK_JUDGE_PROVIDER": "coreweave/fp8", "ASK_JUDGE_REASONING": "off",
    "ENABLE_RERANK": "false", "RERANK_CANDIDATE_K": "32", "PROJECT_DOCUMENTS": "true",
    "LLM_PROVIDER": "anthropic", "ASK_ANSWER_TOKENS": "8000", "LLM_ANSWER_TOKEN_CEILING": "32000",
    "ASK_DEADLINE_SECONDS": "45", "ENABLE_WEB_ANSWER": "true", "ASK_CONVERSATION_MEMORY": "true",
    "VOICE_AUDIT_FUNCTION": "fieldsight-prod-voice-audit",
    "CORROBORATION_API_KEY": "fake-judge-key", "DASHSCOPE_API_KEY": "fake-ds-key",
    "ELEVENLABS_API_KEY": "fake-el", "ANTHROPIC_API_KEY": "fake-ant",
    "QWEN_API_KEY": "fake-qwen", "DB_PASSWORD": "pw", "SOME_SECRET": "s", "GH_TOKEN": "t",
    "SERVER_BUILD": "build-42",
}
RAG = {"SEARCH_INDEX_READ": "session", "PROJECT_DOCUMENTS": "true", "DB_SECRET_ARN": "arn:x"}


class FakeLambdaApi:
    def __init__(self, worker=WORKER, rag=RAG):
        self.by_name = {"fieldsight-prod-ask-voice-worker": worker,
                        "fieldsight-prod-rag-search": rag}
        self.asked = []

    def get_function_configuration(self, FunctionName):
        self.asked.append(FunctionName)
        return {"FunctionName": FunctionName, "CodeSha256": "sha-" + FunctionName[-6:],
                "Environment": {"Variables": dict(self.by_name[FunctionName])}}


def test_copy_keeps_flags_and_drops_every_secret():
    copied, secrets, fp = env.fetch_configs(FakeLambdaApi(), "fieldsight-prod")
    assert copied["ASK_ANSWER_TOKENS"] == "8000" and copied["LLM_ANSWER_TOKEN_CEILING"] == "32000"
    assert copied["ASK_JUDGE_MODEL"] == "deepseek/deepseek-v4.1-flash"
    assert copied["SEARCH_INDEX_READ"] == "session" and copied["DB_SECRET_ARN"] == "arn:x"
    for k in ("ELEVENLABS_API_KEY", "ANTHROPIC_API_KEY", "QWEN_API_KEY", "DB_PASSWORD",
              "SOME_SECRET", "GH_TOKEN", "CORROBORATION_API_KEY", "DASHSCOPE_API_KEY"):
        assert k not in copied
    assert secrets == {"CORROBORATION_API_KEY": "fake-judge-key", "DASHSCOPE_API_KEY": "fake-ds-key"}


def test_qwen_key_only_when_the_fallback_provider_is_qwen():
    _, secrets, fp = env.fetch_configs(FakeLambdaApi(dict(WORKER, LLM_PROVIDER="qwen")),
                                       "fieldsight-prod")
    assert secrets["QWEN_API_KEY"] == "fake-qwen"
    assert fp["secrets"] == {"CORROBORATION_API_KEY": True, "DASHSCOPE_API_KEY": True,
                             "QWEN_API_KEY": True}


def test_fingerprint_carries_no_secret_value():
    _, _, fp = env.fetch_configs(FakeLambdaApi(), "fieldsight-prod")
    text = json.dumps(fp)
    assert "fake-" not in text and "pw" not in fp.get("values", {}).values()
    assert fp["secrets"] == {"CORROBORATION_API_KEY": True, "DASHSCOPE_API_KEY": True,
                             "QWEN_API_KEY": False}
    v = fp["values"]
    assert v["ASK_JUDGE_MODEL"] == "deepseek/deepseek-v4.1-flash" and v["LLM_PROVIDER"] == "anthropic"
    assert v["SEARCH_INDEX_READ"] == "session" and v["SERVER_BUILD"] == "build-42"
    assert fp["deployed"]["fieldsight-prod-rag-search"]["CodeSha256"].startswith("sha-")
    assert fp["fallback_synthesis"] == "anthropic: no key injected -> expect +no_synthesis"


def test_missing_judge_key_stops_the_run():
    w = dict(WORKER)
    del w["CORROBORATION_API_KEY"]
    with pytest.raises(SystemExit, match="CORROBORATION_API_KEY"):
        env.fetch_configs(FakeLambdaApi(w), "fieldsight-prod")


def test_injected_env_is_the_eval_one(monkeypatch):
    for k in list(WORKER) + list(RAG):
        monkeypatch.delenv(k, raising=False)
    copied, secrets, _ = env.fetch_configs(FakeLambdaApi(), "fieldsight-prod")
    monkeypatch.setattr(os, "environ", dict(os.environ))
    harness.set_import_env("report", {**copied, **secrets, "DASHSCOPE_RERANK_TIMEOUT_SECONDS": "3"})
    assert os.environ["DASHSCOPE_RERANK_TIMEOUT_SECONDS"] == "20"      # spec §3, not prod 3 s
    assert os.environ["CORROBORATION_API_KEY"] == "fake-judge-key"
    assert "ASK_DEADLINE_SECONDS" not in os.environ and "VOICE_AUDIT_FUNCTION" not in os.environ
    assert os.environ["ENABLE_WEB_ANSWER"] == "false"
    assert os.environ["ASK_CONVERSATION_MEMORY"] == "false"
    assert os.environ["SEARCH_INDEX_READ"] == "report"      # --family wins
    assert "ELEVENLABS_API_KEY" not in os.environ


def test_host_allowlist_follows_the_provider():
    hosts = env.allowed_hosts(
        {"LLM_PROVIDER": "anthropic"}, judge_url="https://openrouter.ai/api/v1/chat/completions",
        dashscope_urls=["https://dashscope-intl.aliyuncs.com/compatible-mode/v1"])
    assert hosts == {"openrouter.ai", "dashscope-intl.aliyuncs.com",
                     "rds-data.ap-southeast-2.amazonaws.com", "sts.*.amazonaws.com"}
    hosts = env.allowed_hosts(
        {"LLM_PROVIDER": "qwen", "QWEN_BASE_URL": "https://qwen.example/v1"},
        judge_url="https://openrouter.ai/x", dashscope_urls=[])
    assert "qwen.example" in hosts
