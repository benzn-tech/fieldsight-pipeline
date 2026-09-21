"""The one `LLM_USAGE` CloudWatch line, shared by every LLM client in this repo.

Extracted out of `llm_utils.py` (2026-09-21) so `corroboration_client.py` --
a deliberately separate OpenRouter client, see its own module docstring for
the four reasons it exists -- can emit the SAME log line without importing
`llm_utils` itself.

That "without importing `llm_utils`" is load-bearing, not incidental.
`test_corroboration_client.py::test_the_client_is_one_vendor_on_every_stack`
asserts `corroboration_client.py` never reaches `llm_utils`, `LLM_PROVIDER`,
`QWEN_API_KEY`, `dashscope` or `anthropic`: TEST and prod point `llm_utils` at
different models via `LLM_PROVIDER`, and if `corroboration_client` could route
through that machinery, the environment where the corroboration feature gets
tested would be exercising a different vendor from the one that ships. A
one-function, no-dependency logging module carries none of that risk, so the
formatting logic lives here instead of being duplicated in each client.

`llm_utils.py` still owns the module-level `logger.setLevel(logging.INFO)`
side effect that makes its own usage line survive the Lambda runtime's
WARNING-by-default root logger (proven in
`test_llm_usage_survives_the_lambda_runtime_default.py`). This module does the
SAME thing for the same reason: `corroboration_client.py` must not be able to
assume it inherits that side effect just because it happens to run in a
process that also imports `llm_utils` somewhere -- an Ask invocation with
`ASK_CONVERSATION_MEMORY` off never imports `ask_rewrite`/`corroboration_client`
at all, and one that does must not depend on import ORDER to get a visible log
line.
"""
import logging

# The ROOT logger, not a named one -- the same object `llm_utils.py` raises to
# INFO, and the one the Lambda runtime actually gates at WARNING before any
# application code runs. A named logger here would propagate to a root still
# sitting at WARNING and the line would vanish exactly the way a decision log
# vanished on TEST on 2026-09-18 (see `llm_utils.py`'s own comment on this).
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def log_usage(provider, model, caller, elapsed, prompt_tokens=None,
              completion_tokens=None, reasoning_tokens=None,
              cache_read_tokens=None, cache_write_tokens=None):
    """One CloudWatch-queryable line per completed call, on every provider path.

    We have exactly one cost data point in this whole repo: a hand-run bench in
    a code comment (2026-09-09, two models, three runs each). Every production
    call before this line existed spent real money and left nothing behind --
    not the model that actually served it, not the tokens, not whether a cache
    fired. We are about to decide, among other things, whether the rewrite
    prompt (conversation history, up to several thousand tokens) is large and
    stable enough to be worth caching -- and the rewrite call is one this
    module exists to make visible for the first time.

    Fixed `LLM_USAGE ` prefix + key=value, not prose: the point is
    `filter @message like /^LLM_USAGE/` and `parse` in CloudWatch Logs
    Insights, so grep-shaped debugging text would defeat the purpose. Counts
    and identifiers only -- NEVER prompt or completion text, which is customer
    conversation data.

    Every caller wraps this in try/except (see `llm_utils._call_anthropic`/
    `_call_qwen` and `corroboration_client.call`): a telemetry bug must never
    turn into an outage, and a usage line that is never written is worse than
    no line, because it looks like instrumentation.
    """
    logger.info(
        "LLM_USAGE provider=%s model=%s caller=%s latency_ms=%d prompt_tokens=%s "
        "completion_tokens=%s reasoning_tokens=%s cache_read_tokens=%s "
        "cache_write_tokens=%s",
        provider, model, caller, int(elapsed * 1000),
        prompt_tokens if prompt_tokens is not None else "-",
        completion_tokens if completion_tokens is not None else "-",
        reasoning_tokens if reasoning_tokens is not None else "-",
        cache_read_tokens if cache_read_tokens is not None else "-",
        cache_write_tokens if cache_write_tokens is not None else "-",
    )
