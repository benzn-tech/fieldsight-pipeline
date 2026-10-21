"""Wiring for the Ask v2 mode (spec §2.1): source export, import-time env, seam
assertions. Production logic is never copied: the real orchestrator, ask_tools and
rag-search run in-process and only transport is substituted."""
import inspect
import os
import sys

from askv2.srcref import export_src

FAKE_FUNCTION = "eval-in-process"

# lambda_ask_agent and friends read these at import. Placeholders only: S3 and the
# Anthropic key are never reached on this path (the worker is not used; the boto3
# guard refuses s3).
PLACEHOLDERS = {
    "S3_BUCKET": "retrieval-eval-unused",
    "ANTHROPIC_API_KEY": "retrieval-eval-unused",
    "AWS_DEFAULT_REGION": "ap-southeast-2",
    "DASHSCOPE_EMBED_MODEL": "text-embedding-v4",
    "DASHSCOPE_EMBED_DIM": "1024",
    # A laptop timeout would fall back to arrival order and change the items (spec §3).
    "DASHSCOPE_RERANK_TIMEOUT_SECONDS": "20",
}
# Forced whatever the copied worker env says (spec §3).
FORCED = {
    "RAG_SEARCH_FUNCTION": FAKE_FUNCTION,
    "ENABLE_WEB_ANSWER": "false",
    "ASK_CONVERSATION_MEMORY": "false",
}
# Unset: the worker's '45' would cap the raised DEADLINE_S patch (spec §3 Timing),
# and voice audit is a voice-only path.
UNSET = ("ASK_DEADLINE_SECONDS", "VOICE_AUDIT_FUNCTION")


def set_import_env(family, copied_env=None):
    """os.environ as the eval process needs it BEFORE any production import."""
    for k, v in PLACEHOLDERS.items():
        os.environ.setdefault(k, v)
    os.environ.update(copied_env or {})
    os.environ.update(FORCED)
    os.environ["SEARCH_INDEX_READ"] = family
    os.environ.setdefault("ENABLE_RERANK", "false")
    for k in UNSET:
        os.environ.pop(k, None)


def prepare_imports(ref, family, copied_env=None):
    """Export `src` at `ref`, set the env, put the export first on sys.path."""
    src, sha = export_src(ref)
    set_import_env(family, copied_env)
    sys.path.insert(0, str(src))
    return src, sha


def _seam(ok, name):
    if not ok:
        raise SystemExit("seam moved: %s" % name)


def assert_seams(family, rerank):
    """Every patch point exists with the expected kind and the import-time env took
    effect (the run.py REWRITES rule: fail loudly, never measure something else)."""
    import ask_orchestrator
    import ask_tools
    import lambda_ask_agent
    import lambda_rag_search
    from db import connection

    def has(mod, attr, kind):
        name = "%s.%s" % (mod.__name__, attr)
        _seam(hasattr(mod, attr), name)
        val = getattr(mod, attr)
        _seam({"function": inspect.isfunction(val) or inspect.isbuiltin(val),
               "class": inspect.isclass(val), "dict": isinstance(val, dict),
               "list": isinstance(val, list), "callable": callable(val)}[kind], name)
        return val

    answer = has(ask_orchestrator, "answer", "function")
    params = inspect.signature(answer).parameters
    _seam(all(p in params for p in ("tools", "chat", "turn_id", "on_state", "channel")),
          "ask_orchestrator.answer(tools, chat, turn_id)")
    deadline = has(ask_orchestrator, "DEADLINE_S", "dict")
    _seam("screen" in deadline, "ask_orchestrator.DEADLINE_S['screen']")
    has(ask_orchestrator, "_cited_card_refs", "function")
    has(ask_orchestrator, "_final_of", "function")
    has(ask_orchestrator, "_item_line", "function")
    has(lambda_ask_agent, "_get_lambda_client", "function")
    _seam(lambda_ask_agent.RAG_SEARCH_FUNCTION == FAKE_FUNCTION,
          "lambda_ask_agent.RAG_SEARCH_FUNCTION == %r (env set after import?)" % FAKE_FUNCTION)
    _seam(lambda_ask_agent.RERANK_ENABLED is bool(rerank),
          "lambda_ask_agent.RERANK_ENABLED == %s" % bool(rerank))
    has(lambda_rag_search, "get_cached_connection", "function")
    has(lambda_rag_search, "close_cached_connection", "function")
    has(lambda_rag_search, "lambda_handler", "function")
    _seam(lambda_rag_search.INDEX_FAMILY == family,
          "lambda_rag_search.INDEX_FAMILY == %r" % family)
    has(ask_tools, "TOOLS", "list")
    has(ask_tools, "offered_tools", "function")
    has(ask_tools, "RefAssigner", "class")
    has(ask_tools, "ToolContext", "class")
    has(ask_tools, "execute", "function")
    has(connection, "get_connection", "function")
