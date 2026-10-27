"""Report bench, Task 5: request builders pinned to the prod body."""
import importlib.util
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "report-bench")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rq = _load("rb_requests")
schemas = _load("rb_schemas")
import corroboration_client as cc  # noqa: E402
import llm_utils  # noqa: E402

P = "PROMPT"


def test_muse_x_body():
    b = rq.openrouter_body("muse", "X", P, rq.budget("X", 10))
    assert b["model"] == "meta/muse-spark-1.3-contributor"
    assert b["reasoning"] == {"effort": "high"}
    assert b["max_tokens"] == min(8192 + 7000, 32000) + 8000
    assert b["response_format"] == {"type": "json_object"}
    assert "temperature" not in b and "provider" not in b


def test_deepseek_pinned_to_coreweave():
    b = rq.openrouter_body("deepseek", "X", P, 9000)
    assert b["provider"] == {"only": ["coreweave/fp8"], "allow_fallbacks": False}
    assert b["reasoning"] == {"effort": "high"}


def test_gemini_provider_order():
    b = rq.openrouter_body("gemini", "R", P, 9000)
    assert b["provider"] == cc.provider_preference("google/gemini-3.8-flash")


def test_mimo_reasoning_off_no_headroom():
    b = rq.openrouter_body("mimo", "X", P, 9000)
    assert b["reasoning"] == {"enabled": False}
    assert b["max_tokens"] == 9000
    assert b["response_format"] == {"type": "json_object"}


def test_t_body_and_retry():
    b = rq.openrouter_body("muse", "T", P, rq.budget("T", 3))
    assert "response_format" not in b
    assert b["max_tokens"] == 8000 + 8000
    r = rq.openrouter_body("muse", "T", P, rq.budget("T", 3, retry=True), retry=True)
    assert r["reasoning"] == {"effort": "low"}
    assert r["max_tokens"] == 12000 + 8000


def test_sonnet_x_request():
    prompt = 'x "location_markers" "inspections" "topics" "declared_site"'
    r = rq.claude_request("sonnet", "X", prompt, rq.budget("X", 10))
    assert r["model"] == "claude-sonnet-5-5"
    assert r["thinking"] == {"type": "adaptive"}
    assert r["output_config"]["effort"] == "high"
    assert r["output_config"]["format"]["type"] == "json_schema"
    assert r["max_tokens"] == rq.budget("X", 10) + 8000
    assert "temperature" not in r


def test_haiku_effort_explicit_and_t_has_no_format():
    r = rq.claude_request("haiku", "T", P, 8000)
    assert r["output_config"] == {"effort": "high"}
    r = rq.claude_request("haiku", "T", P, 12000, retry=True)
    assert r["output_config"] == {"effort": "low"} and r["max_tokens"] == 20000


def test_budget_matches_prod():
    import lambda_extract_session as les
    for n in (0, 10, 33, 50, 200):
        assert rq.budget("X", n) == les.max_tokens_for(n)
    assert rq.budget("X", 50) == 32000
    assert rq.budget("R", 3) == min(8192 + 2100, 32000)


def test_pinned_against_import_order(monkeypatch):
    monkeypatch.setattr(llm_utils, "LLM_TEMPERATURE", 0.0)
    monkeypatch.setattr(llm_utils, "LLM_REASONING_EFFORT", "")
    monkeypatch.setattr(llm_utils, "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/x")
    b = rq.openrouter_body("muse", "X", P, 9000)
    assert "temperature" not in b and b["reasoning"] == {"effort": "high"}
    # restored afterwards: nothing else in this interpreter sees the pin
    assert llm_utils.LLM_TEMPERATURE == 0.0 and llm_utils.LLM_REASONING_EFFORT == ""


def _top_keys(example):
    return re.findall(r'^  "(\w+)":', example, re.MULTILINE)


def _topic_keys(example):
    block = example[example.index('"topics": ['):]
    return re.findall(r'^      "(\w+)":', block, re.MULTILINE)


def test_schemas_follow_prod_examples():
    import lambda_extract_session as les
    import lambda_report_generator as rg
    x, r = schemas.for_task("X"), schemas.for_task("R")
    assert x["required"] == _top_keys(les.EXTRACTION_SCHEMA)
    assert r["required"] == _top_keys(rg.DAILY_REPORT_SCHEMA)
    assert x["properties"]["topics"]["items"]["required"] == _topic_keys(les.EXTRACTION_SCHEMA)
    assert r["properties"]["topics"]["items"]["required"] == _topic_keys(rg.DAILY_REPORT_SCHEMA)
    assert schemas.for_task("T") is None


def test_schema_requires_only_what_the_prompt_asks():
    s = schemas.for_task("X", 'old prompt {"topics": [], "declared_site": null}')
    assert s["required"] == ["topics", "declared_site"]
    assert "location_markers" in s["properties"]


def test_every_object_closed():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) <= set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schemas.for_task("X"))
    walk(schemas.for_task("R"))
