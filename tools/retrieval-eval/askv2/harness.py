"""Wiring for the Ask v2 mode (spec §2.1): source export, import-time env, seam
assertions. Production logic is never copied: the real orchestrator, ask_tools and
rag-search run in-process and only transport is substituted."""
import copy
import fnmatch
import inspect
import io
import json
import logging
import os
import re
import sys
import threading
import time

import boto3
import urllib3

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


# ============================================================== per-turn records

class TurnRecord:
    """Everything one turn produced, filled by the fake Lambda, the tools proxy, the
    chat wrapper and the adapters (spec §1: the funnel is scored from these)."""

    def __init__(self):
        from askv2.adapter import AdapterLog
        self.log = AdapterLog()
        self.invokes = []         # {"batch", "calls", "results", "ms"}
        self.batches = []         # {"calls", "envelopes", "ms", "judge_calls_before"}
        self.judge = []           # {"messages", "tool_choice", "turn", "usage", ...}
        self.states = []


class EvalTurns:
    """The current turn's record, shared by every seam object. `next()` starts a turn."""

    def __init__(self):
        self.rec = TurnRecord()
        self.local = threading.local()     # the batch an invoke belongs to, per thread

    def next(self):
        self.rec = TurnRecord()
        return self.rec


# ============================================================== fake Lambda

class FakeLambda:
    """`lambda_client.invoke` for rag-search, run in-process (spec §2.1.4). One invoke
    at a time (review S4): a late round-1 thread and a judge round never share an
    adapter or interleave their records."""

    def __init__(self, ev):
        self.ev = ev
        self.lock = threading.Lock()

    def invoke(self, FunctionName, InvocationType, Payload, **_):
        from askv2.render import EvalAdapterError
        import lambda_rag_search
        if FunctionName != FAKE_FUNCTION or InvocationType != "RequestResponse":
            raise EvalAdapterError("unexpected invoke %s/%s" % (FunctionName, InvocationType))
        event = json.loads(Payload)
        if event.get("mode") not in ("tools", "metric"):
            raise EvalAdapterError("unexpected rag-search mode %r" % event.get("mode"))
        with self.lock:
            t0 = time.perf_counter()
            result = lambda_rag_search.lambda_handler(event, None)
            ms = (time.perf_counter() - t0) * 1000
        # The JSON round trip Lambda marshalling applies.
        body = json.dumps(result, default=str)
        plain = json.loads(body)
        self.ev.rec.invokes.append({
            "batch": getattr(self.ev.local, "batch", None), "ms": round(ms, 1),
            "calls": [{k: c.get(k) for k in ("id", "name", "args")}
                      for c in event.get("calls") or []],
            "results": plain.get("results") if event.get("mode") == "tools" else [plain]})
        return {"StatusCode": 200, "Payload": io.BytesIO(body.encode("utf-8"))}


# ============================================================== patches

class _Patches:
    def __init__(self):
        self._undo = []

    def set(self, obj, attr, value):
        self._undo.append(("attr", obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def setitem(self, d, key, value):
        self._undo.append(("item", d, key, d[key]))
        d[key] = value

    def restore(self):
        while self._undo:
            kind, obj, key, old = self._undo.pop()
            if kind == "item":
                obj[key] = old
            else:
                setattr(obj, key, old)


def install_patches(fake, adapter_factory, screen_deadline_s=120.0):
    """Route rag-search's connection to a thread-local adapter (review S4), forbid a
    real psycopg connection, hand the orchestrator the fake Lambda client, raise the
    screen deadline (spec §3 Timing). Every name was checked by assert_seams."""
    from askv2.render import EvalAdapterError
    import ask_orchestrator
    import lambda_ask_agent
    import lambda_rag_search
    from db import connection

    local = threading.local()

    def get_cached_connection():
        conn = getattr(local, "conn", None)
        if conn is None or conn.closed:
            conn = local.conn = adapter_factory()
        return conn

    def close_cached_connection():
        conn, local.conn = getattr(local, "conn", None), None
        if conn is not None:
            conn.close()

    def no_connection(*a, **k):
        raise EvalAdapterError("a real database connection was requested")

    p = _Patches()
    p.set(lambda_rag_search, "get_cached_connection", get_cached_connection)
    p.set(lambda_rag_search, "close_cached_connection", close_cached_connection)
    p.set(connection, "get_connection", no_connection)
    p.set(lambda_ask_agent, "_get_lambda_client", lambda: fake)
    p.setitem(ask_orchestrator.DEADLINE_S, "screen", float(screen_deadline_s))
    return p


# ============================================================== guards

class GuardError(Exception):
    """A network or AWS call the eval does not allow."""


class _Guard:
    def __init__(self, restore):
        self.counts = {}
        self._restore = restore

    def restore(self):
        self._restore()


def arm_url_guard(allowed_hosts):
    """Every urllib3 request (botocore's included: its pools subclass this one) must go
    to an allowed host; counted per host. fnmatch patterns allowed (`sts.*.amazonaws.com`)."""
    pool = urllib3.connectionpool.HTTPConnectionPool
    orig = pool.urlopen
    allowed = set(allowed_hosts)

    def urlopen(self, *a, **k):
        host = self.host or ""
        if not any(fnmatch.fnmatchcase(host, pat) for pat in allowed):
            key = host + " (refused)"
            g.counts[key] = g.counts.get(key, 0) + 1
            raise GuardError("HTTP to %s is not allowed in the eval" % host)
        g.counts[host] = g.counts.get(host, 0) + 1
        return orig(self, *a, **k)

    def restore():
        pool.urlopen = orig
    g = _Guard(restore)
    pool.urlopen = urlopen
    return g


def arm_boto_guard(allowed_services):
    """boto3 clients only for `allowed_services` (spec §2.1.7: rds-data once armed)."""
    cls = boto3.session.Session
    orig = cls.client

    def client(self, service_name, *a, **k):
        if service_name not in allowed_services:
            raise GuardError("boto3 client %r is not allowed in the eval" % service_name)
        return orig(self, service_name, *a, **k)

    def restore():
        cls.client = orig
    g = _Guard(restore)
    cls.client = client
    return g


def freeze_session(session):
    """A boto3 Session on static credentials resolved now (review S5), so no SSO or
    assume-role refresh needs the network after the guards are armed."""
    fc = session.get_credentials().get_frozen_credentials()
    return boto3.session.Session(aws_access_key_id=fc.access_key,
                                 aws_secret_access_key=fc.secret_key,
                                 aws_session_token=fc.token, region_name=session.region_name)


# ============================================================== log capture

_KV = re.compile(r"(\w+)=(\S+)")
_USAGE_KEYS = ("provider", "model", "caller", "prompt_tokens", "completion_tokens",
               "reasoning_tokens")


class _Capture(logging.Handler):
    """The root logger's only handler (spec §7): keeps ASK_ROUTE (by turn id) and the
    LLM_USAGE counts, drops everything else -- nothing reaches the console, so no vendor
    response body or record text is echoed."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.routes, self.usage, self.dropped = {}, [], 0
        self._saved = None

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:                                   # noqa: BLE001
            self.dropped += 1
            return
        if msg.startswith("ASK_ROUTE "):
            route = json.loads(msg[len("ASK_ROUTE "):])
            self.routes[route.get("turn")] = route
        elif msg.startswith("LLM_USAGE "):
            kv = dict(_KV.findall(msg))
            self.usage.append({k: kv[k] for k in _USAGE_KEYS if k in kv and kv[k] != "-"})
        else:
            self.dropped += 1

    def restore(self):
        root = logging.getLogger()
        root.handlers, level = self._saved
        root.setLevel(level)


def install_log_capture():
    root = logging.getLogger()
    cap = _Capture()
    cap._saved = (list(root.handlers), root.level)
    root.handlers = [cap]
    root.setLevel(logging.INFO)
    return cap


# ============================================================== proxies

class ToolsProxy:
    """The `tools` argument of `ask_orchestrator.answer`: the real ask_tools, with the
    fake Lambda client in every ToolContext and each batch recorded (spec §2.1.3)."""

    def __init__(self, ev, fake):
        import ask_tools
        self._t, self._ev, self._fake = ask_tools, ev, fake
        self.TOOLS = ask_tools.TOOLS
        self.RefAssigner = ask_tools.RefAssigner

    def offered_tools(self):
        return self._t.offered_tools()

    def ToolContext(self, **kw):
        return self._t.ToolContext(**{**kw, "lambda_client": self._fake})

    def execute(self, calls, ctx):
        rec = self._ev.rec
        entry = {"calls": copy.deepcopy(calls), "envelopes": None, "ms": None,
                 "judge_calls_before": len(rec.judge)}
        rec.batches.append(entry)
        self._ev.local.batch = len(rec.batches) - 1
        t0 = time.perf_counter()
        try:
            envs = self._t.execute(calls, ctx)
        finally:
            self._ev.local.batch = None
            entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        entry["envelopes"] = copy.deepcopy(envs)
        return envs


def _usage(u):
    u = u or {}
    return {"prompt": u.get("prompt_tokens"), "completion": u.get("completion_tokens"),
            "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens")}


def chat_wrapper(ev, real_chat):
    """The `chat` argument: records every judge request (messages as sent) and the
    ChatTurn it got back, then returns that turn unchanged."""
    def chat(messages, tools, **kw):
        entry = {"messages": copy.deepcopy(messages), "tool_choice": kw.get("tool_choice"),
                 "turn": None, "usage": _usage(None), "model": None, "error": None,
                 "ms": None}
        ev.rec.judge.append(entry)
        t0 = time.perf_counter()
        try:
            turn = real_chat(messages, tools, **kw)
        except BaseException as exc:
            entry["error"] = "raised:%s" % type(exc).__name__
            raise
        finally:
            entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        entry.update(turn=turn, usage=_usage(turn.usage), model=turn.model, error=turn.error)
        return turn
    return chat
