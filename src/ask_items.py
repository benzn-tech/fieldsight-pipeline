"""The action-pack agent's items tool (action packs spec §4.3 Gather, §13 "items mode",
§14 S4; Task 8).

Two halves, one module, because both sides must agree on the request shape:

* `validate_request` -- pure. The ask side (the background agent, not in the VPC) runs it
  before invoking; rag-search's `mode: "items"` runs it AGAIN on what arrives, because a
  request is never trusted just because our own client built it.
* `run` -- the client wrapper the agent calls. It invokes rag-search with the caller's
  identity taken from the `ToolContext` (`ctx.caller_sub`), never from the model's
  arguments (the `ask_tools` invariant, spec §7 "Run identity"), and returns one
  `{tool, id, items, meta}` envelope whose items are kind `item`, refs from the turn's
  shared `RefAssigner` (dedupe key ("item", stable_id), prefix R), so the pack's sources
  table can record them (kind 'item', stable_id).

NOT an Ask tool: `ITEMS_TOOL` is never added to `ask_tools.TOOLS`, so Ask's orchestrator
never offers it and `ask_tools.validate_call` still refuses its name. Read-only.

Every item carries the fields apply's staleness check compares (status, deadline,
responsible, priority, text, updated_at -- Task 7: a target_snapshot missing a touched field
is stale), ALWAYS, even when None: the agent copies them verbatim into target_snapshot, so
a dropped None key would make every action on that item stale.

Logs: counts and ids only, never the query or item text.
"""
from __future__ import annotations

import json
import logging
import uuid

logger = logging.getLogger()

TOOL_NAME = "list_items"
# The open-ish set: what a pack can still close or update (S10's "from" rows minus the
# finished ones).
DEFAULT_STATUSES = ("open", "in_progress", "blocked")
MAX_STATUSES = 8
MAX_STATUS_CHARS = 32
MAX_QUERY_CHARS = 200
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_MAX_TASK_NO = 2147483647           # task_no is a Postgres integer
_ARGS = ("site_id", "status", "query", "task_no", "limit")
# Every item key, always present (None included). Order is the envelope's.
ITEM_FIELDS = ("stable_id", "task_no", "text", "status", "deadline", "responsible",
               "priority", "updated_at", "created_at", "site_id", "site", "topic_id",
               "report_date", "source")
# The items envelope's own budget: up to MAX_LIMIT short rows, which the Ask tools' 12k
# (about 40 items) would cut. The agent's render (Task 11) budgets what the model reads.
MAX_RESULT_CHARS = 80_000
# One rag-search invoke; RagSearchFunction's Timeout is 23 s.
MAX_DEADLINE_S = 20

ITEMS_TOOL = {
    "name": TOOL_NAME,
    "description": (
        "List the action items (todos) on the projects in scope, with their stable_id, "
        "task number (#n), text, status, deadline, responsible and priority. Defaults to "
        "open, in_progress and blocked items. A close or update action must target a "
        "stable_id copied from this tool's output."),
    "parameters": {
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "description": "Optional project (site) id."},
            "status": {"type": "array", "items": {"type": "string"},
                       "description": "Optional statuses; default open, in_progress, blocked."},
            "query": {"type": "string",
                      "description": f"Optional words the item text contains "
                                     f"(at most {MAX_QUERY_CHARS} characters)."},
            "task_no": {"type": "integer", "minimum": 1,
                        "description": "Optional task number (#n on the Tasks page)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        },
        "required": [],
        "additionalProperties": False,
    },
}


class _Bad(Exception):
    def __init__(self, field):
        super().__init__(field)
        self.field = field


def _uuid(value):
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def _int(value):
    """A plain int (never a bool) or a digit string; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value.strip())
    return None


def validate_request(args):
    """(normalized, None) or (None, {"error": "bad_args", "field": f}).

    normalized = {"site_id": str|None, "statuses": [str], "query": str|None,
                  "task_no": int|None, "limit": int, "clamped": {...} only when clamped}.
    A limit above MAX_LIMIT is clamped (meta.clamped), anything else malformed is
    bad_args. Statuses are checked for shape, not membership: the column is free text."""
    try:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise _Bad("args")
        for key in args:
            if key not in _ARGS:
                raise _Bad(key)
        out = {"site_id": None, "statuses": list(DEFAULT_STATUSES), "query": None,
               "task_no": None, "limit": DEFAULT_LIMIT}
        if args.get("site_id") is not None:
            out["site_id"] = _uuid(args["site_id"])
            if out["site_id"] is None:
                raise _Bad("site_id")
        if args.get("status") is not None:
            raw = args["status"]
            if isinstance(raw, str):
                raw = [raw]
            if (not isinstance(raw, list) or not raw or len(raw) > MAX_STATUSES
                    or not all(isinstance(s, str) and 0 < len(s.strip()) <= MAX_STATUS_CHARS
                               for s in raw)):
                raise _Bad("status")
            out["statuses"] = list(dict.fromkeys(s.strip().lower() for s in raw))
        if args.get("query") is not None:
            q = args["query"]
            if not isinstance(q, str) or len(q.strip()) > MAX_QUERY_CHARS:
                raise _Bad("query")
            out["query"] = q.strip() or None
        if args.get("task_no") is not None:
            no = _int(args["task_no"])
            if no is None or not 0 < no <= _MAX_TASK_NO:
                raise _Bad("task_no")
            out["task_no"] = no
        if args.get("limit") is not None:
            lim = _int(args["limit"])
            if lim is None or lim < 1:
                raise _Bad("limit")
            if lim > MAX_LIMIT:
                out["clamped"] = {"limit": MAX_LIMIT}
                lim = MAX_LIMIT
            out["limit"] = lim
        return out, None
    except _Bad as bad:
        return None, {"error": "bad_args", "field": bad.field}


def wire_args(normalized):
    """The `args` rag-search receives: the validated request in its own vocabulary
    (`validate_request` accepts it back unchanged)."""
    out = {"status": list(normalized["statuses"]), "limit": normalized["limit"]}
    for key in ("site_id", "query", "task_no"):
        if normalized.get(key) is not None:
            out[key] = normalized[key]
    return out


def _size(obj):
    return len(json.dumps(obj, default=str, ensure_ascii=False))


def envelope(call_id, rows, meta, refs):
    """One `{tool, id, items, meta}` from rag-search's rows. Items are kind `item`,
    provenance `records`, every ITEM_FIELDS key present; ref = refs.assign("item",
    stable_id). A row without a stable_id is dropped (it cannot be targeted or cited)."""
    items, used, seen = [], 2, set()
    meta = dict(meta)
    for row in rows:
        if not isinstance(row, dict) or not row.get("stable_id"):
            continue
        sid = str(row["stable_id"])
        if sid in seen:
            continue
        item = {"kind": "item", "provenance": "records",
                **{f: row.get(f) for f in ITEM_FIELDS}}
        item["stable_id"] = sid
        size = _size(dict(item, ref="R0000")) + (2 if items else 0)
        if used + size > MAX_RESULT_CHARS:
            meta["truncated"] = True
            if not items:
                # Not a failure: the first item alone exceeds the budget. Narrow and ask
                # again (site_id / status / query / task_no).
                meta.setdefault("empty_reason", "over_budget")
            break
        seen.add(sid)
        items.append({"ref": refs.assign("item", sid, "R"), **item})
        used += size
    meta.setdefault("truncated", False)
    if not items and not meta.get("empty_reason") and not meta.get("error"):
        meta["error"] = "upstream"          # items [] always comes with a reason
    return {"tool": TOOL_NAME, "id": call_id, "items": items, "meta": meta}


def run(args, ctx, call_id="items"):
    """The agent's items call. Never raises; a failure is meta.error ("bad_args",
    "timeout", "upstream", "not_provisioned", or the company gate's reason).

    `ctx` is an `ask_tools.ToolContext`: caller_sub, deadline, clock, lambda_client,
    function_name and the turn's shared refs. Retrying a throttled/5xx invoke is the
    caller's lambda_client's business (one wrapper for every rag-search hop)."""
    try:
        return _run(args, ctx, call_id)
    except Exception as exc:                            # noqa: BLE001 -- last resort
        logger.warning("items tool failed: %s", type(exc).__name__)
        return {"tool": TOOL_NAME, "id": call_id, "items": [], "meta": {"error": "upstream"}}


def _run(args, ctx, call_id):
    norm, err = validate_request(args)
    if err:
        return {"tool": TOOL_NAME, "id": call_id, "items": [], "meta": dict(err)}
    left = ctx.deadline - ctx.clock()
    if left <= 0:
        return {"tool": TOOL_NAME, "id": call_id, "items": [], "meta": {"error": "timeout"}}
    resp = ctx.lambda_client.invoke(
        FunctionName=ctx.function_name, InvocationType="RequestResponse",
        Payload=json.dumps({"mode": "items", "sub": ctx.caller_sub,
                            "deadline_s": min(left, MAX_DEADLINE_S),
                            "args": wire_args(norm)}))
    if resp.get("FunctionError"):
        raise RuntimeError("rag-search FunctionError")
    body = json.loads(resp["Payload"].read())
    meta = dict(body["meta"]) if isinstance(body.get("meta"), dict) else {}
    rows = body["rows"] if isinstance(body.get("rows"), list) else []
    if norm.get("clamped"):
        meta["clamped"] = norm["clamped"]
    env = envelope(call_id, rows, meta, ctx.refs)
    logger.info("items tool items=%d rows=%d site=%s statuses=%d query=%s task_no=%s "
                "empty=%s err=%s truncated=%s", len(env["items"]), len(rows),
                norm["site_id"], len(norm["statuses"]), norm["query"] is not None,
                norm["task_no"] is not None, meta.get("empty_reason"), meta.get("error"),
                env["meta"].get("truncated"))
    return env
