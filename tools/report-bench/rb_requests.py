"""Per-model request bodies, pinned to the prod body (spec §3).

OpenRouter models get develop's `llm_utils.build_qwen_payload(...)` body; the
module globals it reads are pinned before every build (`pin_globals`), because
llm_utils reads env at import and pytest shares one interpreter. Provider
routing is borrowed from the web bench (`openrouter_bench.COREWEAVE_PIN`) and
`corroboration_client.provider_preference`.

Claude (Sonnet 5.5 / Haiku 5.5): kwargs for `client.messages.stream(...)`,
shapes checked against the claude-api skill on 2026-10-28:
  thinking {type: "adaptive"} (never {type: "disabled"}: a 400 on Sonnet 5.5);
  output_config {effort, format {type: "json_schema", schema}} -- effort set
  explicitly (Haiku 5.5 defaults to medium); json_schema needs no beta header
  and works with thinking and streaming; no temperature (non-default is a 400);
  max_tokens = answer budget + 8 000 (<= 40 000, under the 128K output cap).
"""
from __future__ import annotations

import contextlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402
import rb_schemas  # noqa: E402

rb_common.ensure_src_on_path()

import corroboration_client as cc  # noqa: E402
import llm_utils  # noqa: E402

_ORB = rb_common.load_web("openrouter_bench")

HEADROOM = 8000
ANSWER_CEILING = 32000
T_BUDGET = 8000
T_RETRY_BUDGET = 12000
TIMEOUT_S = 540.0

MODELS = {
    "muse": {"vendor": "openrouter", "model": "meta/muse-spark-1.3-contributor"},
    "deepseek": {"vendor": "openrouter", "model": _ORB.DEEPSEEK},
    "gemini": {"vendor": "openrouter", "model": "google/gemini-3.8-flash"},
    "mimo": {"vendor": "openrouter", "model": "xiaomi/mimo-v2.6-pro"},
    "sonnet": {"vendor": "anthropic", "model": "claude-sonnet-5-5"},
    "haiku": {"vendor": "anthropic", "model": "claude-haiku-5-5"},
}
# The planned set (spec 2026-10-31 Part A): muse, DeepSeek, Haiku 5.5, Sonnet
# 5.5. gemini / mimo stay in MODELS (their bodies are still built and tested)
# but are planned in no phase.
MODEL_ORDER = ["muse", "deepseek", "haiku", "sonnet"]


_PINNED = {"QWEN_BASE_URL": rb_common.IMPORT_ENV["QWEN_BASE_URL"],
           "LLM_REASONING_EFFORT": "high",
           "REASONING_HEADROOM_TOKENS": HEADROOM,
           "LLM_TEMPERATURE": None,
           "ANSWER_TOKEN_CEILING": ANSWER_CEILING}


@contextlib.contextmanager
def pin_globals():
    """The llm_utils globals build_qwen_payload reads, set to the spec §2 values
    for the duration of one build and then restored, so no other test (or
    caller) in this interpreter sees them changed."""
    saved = {k: getattr(llm_utils, k) for k in _PINNED}
    try:
        for k, v in _PINNED.items():
            setattr(llm_utils, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(llm_utils, k, v)


def budget(task: str, n_segments: int, retry: bool = False) -> int:
    """Answer budget: X and R `min(8192 + 700 n, 32000)` (max_tokens_for, the
    nightly generator); T 8 000, its retry 12 000."""
    if task == "T":
        return T_RETRY_BUDGET if retry else T_BUDGET
    return min(8192 + 700 * int(n_segments), ANSWER_CEILING)


def openrouter_body(key, task, prompt, budget, retry=False):
    model = MODELS[key]["model"]
    with pin_globals():
        body = llm_utils.build_qwen_payload(prompt, budget, force_json=(task != "T"),
                                            enable_thinking=(False if retry else None),
                                            model=model)
    if key == "deepseek":
        body["provider"] = dict(_ORB.COREWEAVE_PIN, only=list(_ORB.COREWEAVE_PIN["only"]))
    elif key == "gemini":
        pref = cc.provider_preference(model)
        if pref:
            body["provider"] = pref
    return body


def claude_request(key, task, prompt, budget, retry=False, with_format=True):
    output_config = {"effort": "low" if retry else "high"}
    schema = rb_schemas.for_task(task, prompt) if with_format else None
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    return {"model": MODELS[key]["model"], "max_tokens": budget + HEADROOM,
            "thinking": {"type": "adaptive"}, "output_config": output_config,
            "messages": [{"role": "user", "content": prompt}]}


def request_for(key, task, prompt, budget, retry=False, with_format=True):
    if MODELS[key]["vendor"] == "anthropic":
        return claude_request(key, task, prompt, budget, retry, with_format)
    return openrouter_body(key, task, prompt, budget, retry)


def params_of(req: dict) -> dict:
    """The request minus the prompt (frozen.v1.json `params`)."""
    out = {k: v for k, v in req.items() if k != "messages"}
    return out
