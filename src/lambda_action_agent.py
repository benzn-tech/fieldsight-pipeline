"""ActionAgent worker -- the background action agent's run (Task 10 skeleton).

Spec: docs/superpowers/specs/2026-11-01-background-action-agent-design.md §4.1, §4.2, §4.5,
§14 D3, S1, S8, S9, S16.

    S3 ObjectCreated action_pack_requests/<pack_id>.json   (data bucket; wire-s3-events.sh)
      -> ACTION_PACKS internal|on? else return (no read, no invoke)
      -> read the request object (its pack_id must match the key)
      -> org-api "action_pack_claim" (RequestResponse, compare-and-swap queued -> running).
         Lost claim (a second S3 delivery, cancelled, company inactive) -> exit, nothing spent.
      -> run_stages(ctx) -> payload       (action_pack_stages; verify: Task 12)
         ctx.checkpoint() between rounds: cancel marker + heartbeat; raises Cancelled
      -> final checkpoint, then org-api "action_pack_store"
         ok -> done; refused (cancelled/failed meanwhile) -> stop quietly;
         payload_too_large / invalid / anything else -> "action_pack_fail" reason internal
      -> any exception -> "action_pack_fail" with the exception's reason code
         (StageError subclasses below; anything untyped is `internal`); Cancelled -> nothing.

No automatic retries (EventInvokeConfig MaximumRetryAttempts 0): a retry would re-run a paid
run, and the claim makes a duplicate S3 delivery a no-op anyway.

Logs carry ids, counts and codes only -- never the instruction, a reading, a title or an
excerpt, and never an exception's message (it may quote any of those).
"""
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import unquote_plus

import action_pack_view

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REQUEST_PREFIX = "action_pack_requests/"
_REQUEST_KEY_RE = re.compile(
    r"^action_pack_requests/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{12})\.json$")
_MISSING_CODES = ("NoSuchKey", "404", "NotFound")

# Budgets (§4.2 as amended by §14 S8). The run aims to finish inside TARGET_S of its claim;
# the write and verify stages each keep a fixed reserve at the end of that window, and the
# tool rounds get what is left. STORE_RESERVE_S is never handed to the stages: it is the
# Lambda time kept for the final checkpoint, the store invoke and, on failure, the fail
# invoke.
#
# What actually keeps a model call inside this window is the DEADLINE each call is given:
# every stage passes `ctx.left(stage)` (llm_utils.call_llm, seconds from now) or
# `ctx.deadline_for(stage)` (llm_tools_client.chat, absolute monotonic). The function's
# LLM_CHAIN_BUDGET_SECONDS (780) is only a backstop: llm_utils measures it from the start of
# EACH call's fallback chain, not from the Lambda's start, so it bounds one call that was
# given no deadline -- it guarantees nothing about a call started late in the run.
TARGET_S = 600.0
WRITE_RESERVE_S = 90.0
VERIFY_RESERVE_S = 90.0
STORE_RESERVE_S = 60.0

# Per-run token cap (§4.2 cost guard), from ACTION_TOKEN_CAP. Enforced by the stages.
DEFAULT_TOKEN_CAP = 400_000


# ---- errors the stages raise (Tasks 11 and 12) ------------------------------------------

class Cancelled(Exception):
    """The pack was cancelled (marker present) or is no longer running (heartbeat). The
    worker stops and does nothing further: the pack already has its final state."""


class StageError(Exception):
    """A stage failed. `reason` is the pack's failure code (repositories.action_packs
    FAILURE_REASONS). Raise a subclass; anything untyped maps to `internal`."""
    reason = "internal"


class BudgetExceeded(StageError):
    """The time or token budget ran out with nothing storable."""
    reason = "budget"


class ModelFailure(StageError):
    """The model chain failed (every model in the chain, or an unusable answer)."""
    reason = "model"


class ToolsFailure(StageError):
    """A tool hop (rag-search, items, web lane) failed in a way the run cannot work around."""
    reason = "tools"


class VerifierFailure(StageError):
    """The verifier could not run (every model check failed, or a malformed answer). No
    verifier of a different family (§14 S6) is ModelFailure (reason `model`)."""
    reason = "verifier"


class OrgApiError(Exception):
    """An org-api invoke crashed (FunctionError), answered a non-200, or returned a body
    that is not a JSON object."""


def failure_reason(exc) -> str:
    reason = getattr(exc, "reason", None) if isinstance(exc, StageError) else None
    return reason if isinstance(reason, str) else "internal"


# ---- model settings (read per call; Tasks 11/12 consume them) ----------------------------

def writer_model() -> str:
    """ACTION_WRITER_MODEL, else the Ask judge (ASK_JUDGE_MODEL), else the tools client's own
    default -- the same resolution the Ask judge uses, so no model id is invented here."""
    import llm_tools_client
    return (os.environ.get("ACTION_WRITER_MODEL") or "").strip() or \
        llm_tools_client._primary_model()


def verifier_model() -> str:
    """ACTION_VERIFIER_MODEL, else the cheap model (CORROBORATION_CHEAP_MODEL / its in-code
    default in web_answer)."""
    import web_answer
    return (os.environ.get("ACTION_VERIFIER_MODEL") or "").strip() or \
        (os.environ.get("CORROBORATION_CHEAP_MODEL") or "").strip() or web_answer.CHEAP_MODEL


def verifier_fallbacks() -> list:
    """ACTION_VERIFIER_FALLBACKS, comma-separated, de-duplicated, order kept (§14 S6)."""
    out = []
    for m in (s.strip() for s in (os.environ.get("ACTION_VERIFIER_FALLBACKS") or "").split(",")):
        if m and m not in out:
            out.append(m)
    return out


def token_cap() -> int:
    try:
        value = int((os.environ.get("ACTION_TOKEN_CAP") or "").strip())
    except ValueError:
        return DEFAULT_TOKEN_CAP
    return value if value > 0 else DEFAULT_TOKEN_CAP


# ---- the run context ----------------------------------------------------------------------

@dataclass
class Budget:
    """Absolute deadlines on `clock` (time.monotonic by default)."""
    start: float
    end: float
    clock: Callable[[], float] = time.monotonic

    @property
    def verify_deadline(self) -> float:
        return self.end

    @property
    def write_deadline(self) -> float:
        return self.end - VERIFY_RESERVE_S

    @property
    def gather_deadline(self) -> float:
        return self.end - VERIFY_RESERVE_S - WRITE_RESERVE_S

    def left(self, deadline: float) -> float:
        return max(0.0, deadline - self.clock())


@dataclass
class RunContext:
    """What the stages get. `caller_sub` is the pack creator's Cognito sub as the claim read
    it from the database -- the identity every tool call is made as (ask_tools.ToolContext).
    `checkpoint(reading=None, title=None)` is called between rounds: it raises Cancelled when
    the run must stop, and otherwise records liveness (and the plan's reading/title)."""
    pack_id: str
    pack: dict
    caller_sub: str
    site_id: str
    company_id: str
    s3: object
    lambda_client: object
    data_bucket: str
    lake_bucket: str
    org_api_function: str
    rag_search_function: str
    budget: Budget
    checkpoint: Callable = field(repr=False, default=None)
    clock: Callable[[], float] = time.monotonic
    # timeout -> a Lambda client for one rag-search hop (rag_lambda_client); None = use
    # lambda_client (tests).
    rag_client_factory: Optional[Callable] = field(repr=False, default=None)

    def deadline_for(self, stage: str) -> float:
        """The absolute deadline (on `clock`) of a stage: 'gather' (tool rounds), 'write'
        or 'verify'. Pass it as llm_tools_client.chat(deadline=...)."""
        try:
            return {"gather": self.budget.gather_deadline, "write": self.budget.write_deadline,
                    "verify": self.budget.verify_deadline}[stage]
        except KeyError:
            raise ValueError(f"unknown stage {stage!r}") from None

    def left(self, stage: str = "verify") -> float:
        """Seconds left before the stage's deadline (>= 0). Pass it as
        llm_utils.call_llm(deadline=...) -- every model call must get one (see TARGET_S)."""
        return self.budget.left(self.deadline_for(stage))


def run_stages(ctx: RunContext) -> dict:
    """The stage runner: plan, gather, write, verify -> the store payload (the shape in
    repositories/action_packs.py). The stages live in action_pack_stages (Task 11); the
    verifier seam (action_pack_verify, Task 12) raises NotImplementedError until it lands,
    so a claimed pack fails `internal` -- reachable only with ACTION_PACKS internal/on."""
    import action_pack_stages
    return action_pack_stages.run(ctx)


# ---- clients ------------------------------------------------------------------------------

_s3_client = None
_lambda_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client("s3")
    return _s3_client


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        import boto3
        from botocore.config import Config
        # The store answer can be large-ish and the claim runs a transaction: wait for it.
        _lambda_client = boto3.client("lambda", config=Config(read_timeout=120,
                                                              retries={"max_attempts": 0}))
    return _lambda_client


_rag_clients = {}


def rag_lambda_client(timeout):
    """A Lambda client for one rag-search hop whose read timeout is `timeout` rounded up to
    a whole second (cached per value): the hop is bounded by the gather time left, not by
    the org-api client's 120 s. No botocore retries -- S8's single retry is the stages'."""
    secs = max(1, int(-(-float(timeout) // 1)))
    if secs not in _rag_clients:
        import boto3
        from botocore.config import Config
        _rag_clients[secs] = boto3.client("lambda", config=Config(
            read_timeout=secs, connect_timeout=min(5, secs), retries={"max_attempts": 0}))
    return _rag_clients[secs]


# ---- helpers ------------------------------------------------------------------------------

def parse_request_key(key) -> Optional[str]:
    """The canonical pack id of action_pack_requests/<uuid>.json, else None."""
    m = _REQUEST_KEY_RE.match(key or "")
    return str(uuid.UUID(m.group(1))) if m else None


def _error_code(exc) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _invoke_org(lambda_client, function_name, payload) -> dict:
    """RequestResponse invoke of one org-api internal task -> its JSON object answer.
    Raises OrgApiError on a crash (FunctionError), a non-200 or a non-object body. An API
    Gateway envelope ({statusCode: ...}) means the task never reached its handler."""
    resp = lambda_client.invoke(FunctionName=function_name, InvocationType="RequestResponse",
                                Payload=json.dumps(payload))
    if resp.get("FunctionError"):
        raise OrgApiError("FunctionError")
    if resp.get("StatusCode", 200) != 200:
        raise OrgApiError(f"status {resp.get('StatusCode')}")
    raw = resp["Payload"].read()
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        raise OrgApiError("unparseable") from None
    if not isinstance(body, dict) or "statusCode" in body:
        raise OrgApiError("not a task answer")
    return body


def _read_request(s3, bucket, key):
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) in _MISSING_CODES:
            return None
        raise
    try:
        body = json.loads(obj["Body"].read())
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _cancel_marker_present(s3, bucket, pack_id) -> bool:
    """ask_voice_turn.is_cancelled's pattern: the role holds s3:ListBucket on
    action_pack_requests/*, so a missing marker is 404; a 403 is an IAM fault and raises."""
    try:
        s3.head_object(Bucket=bucket, Key=f"{REQUEST_PREFIX}{pack_id}.cancel")
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) in _MISSING_CODES:
            return False
        raise
    return True


def _make_checkpoint(*, s3, lambda_client, bucket, org_fn, pack_id):
    def checkpoint(reading=None, title=None):
        if _cancel_marker_present(s3, bucket, pack_id):
            logger.info("action agent cancel marker pack=%s", pack_id)
            raise Cancelled()
        event = {"task": "action_pack_heartbeat", "pack_id": pack_id}
        if reading is not None:
            event["reading"] = str(reading)[:action_pack_view.READING_MAX_CHARS]
        if title is not None:
            event["title"] = str(title)[:action_pack_view.TITLE_MAX_CHARS]
        try:
            answer = _invoke_org(lambda_client, org_fn, event)
        except Exception as exc:  # noqa: BLE001 -- liveness is best effort, see docstring
            logger.warning("action agent heartbeat failed pack=%s (%s)", pack_id,
                           type(exc).__name__)
            return
        if answer.get("error") == "invalid":
            logger.warning("action agent heartbeat invalid pack=%s detail=%s", pack_id,
                           answer.get("detail"))
            return
        state = answer.get("state")
        if state != "running":
            logger.info("action agent stopping pack=%s state=%s error=%s", pack_id, state,
                        answer.get("error"))
            raise Cancelled()
    checkpoint.__doc__ = (
        "Raise Cancelled if the cancel marker exists or the pack is no longer running; "
        "otherwise refresh heartbeat_at (and write reading/title when given). A heartbeat "
        "that cannot be delivered is logged and the run goes on: the marker was checked, "
        "and org-api refuses a store into a pack that is not running.")
    return checkpoint


def _fail(lambda_client, org_fn, pack_id, reason, run_trace=None) -> str:
    """`run_trace`: the counts the stages gathered before failing (§4.5), when the
    exception carried them; org-api keeps it only when it is counts-shaped."""
    event = {"task": "action_pack_fail", "pack_id": pack_id, "reason": reason}
    if isinstance(run_trace, dict):
        event["run_trace"] = run_trace
    try:
        _invoke_org(lambda_client, org_fn, event)
    except Exception as exc:  # noqa: BLE001 -- the stale-run sweep is the backstop (S9)
        logger.error("action agent fail invoke failed pack=%s reason=%s (%s)", pack_id,
                     reason, type(exc).__name__)
    logger.info("action agent failed pack=%s reason=%s", pack_id, reason)
    return f"failed:{reason}"


# ---- one record ---------------------------------------------------------------------------

def process_key(key, *, s3, lambda_client, clock=time.monotonic, remaining_s=900.0,
                stage_runner=None) -> str:
    """Run one request object. Returns an outcome code (for the handler's answer and the
    tests): disabled | ignored | no_request | request_error | bad_request | claim_error |
    lost | stored |
    refused | cancelled | failed:<reason>. `remaining_s` is the Lambda time left, measured
    at `clock()` now."""
    if not action_pack_view.enabled():
        return "disabled"
    pack_id = parse_request_key(key)
    if pack_id is None:
        return "ignored"
    hard_end = clock() + float(remaining_s)
    bucket = os.environ.get("S3_BUCKET", "")
    org_fn = os.environ.get("ORG_API_FUNCTION", "")

    try:
        request = _read_request(s3, bucket, key)
    except Exception as exc:  # noqa: BLE001 -- 403/5xx: an IAM or S3 fault, never a crash
        logger.error("action agent request read failed pack=%s code=%s (%s)", pack_id,
                     _error_code(exc) or "-", type(exc).__name__)
        return "request_error"
    if request is None:
        logger.warning("action agent request missing pack=%s", pack_id)
        return "no_request"
    if str(request.get("pack_id") or "").lower() != pack_id:
        logger.warning("action agent request pack id mismatch pack=%s", pack_id)
        return "bad_request"

    try:
        claim = _invoke_org(lambda_client, org_fn,
                            {"task": "action_pack_claim", "pack_id": pack_id})
    except Exception as exc:  # noqa: BLE001 -- the claim rolled back; the pack is queued
        logger.error("action agent claim failed pack=%s (%s)", pack_id, type(exc).__name__)
        return "claim_error"
    claimed_pack = claim.get("pack") if claim.get("claimed") is True else None
    if not isinstance(claimed_pack, dict):
        logger.info("action agent claim lost pack=%s reason=%s error=%s", pack_id,
                    claim.get("reason"), claim.get("error"))
        return "lost"
    pack = claimed_pack
    if str(pack.get("id") or "").lower() != pack_id or not pack.get("created_by_sub"):
        logger.error("action agent claim answer mismatch pack=%s", pack_id)
        return _fail(lambda_client, org_fn, pack_id, "internal")
    logger.info("action agent claimed pack=%s site=%s", pack_id, pack.get("site_id"))

    start = clock()
    budget = Budget(start=start, end=min(start + TARGET_S, hard_end - STORE_RESERVE_S),
                    clock=clock)
    checkpoint = _make_checkpoint(s3=s3, lambda_client=lambda_client, bucket=bucket,
                                  org_fn=org_fn, pack_id=pack_id)
    ctx = RunContext(
        pack_id=pack_id, pack=pack, caller_sub=pack["created_by_sub"],
        site_id=pack.get("site_id"), company_id=pack.get("company_id"),
        s3=s3, lambda_client=lambda_client, data_bucket=bucket,
        lake_bucket=os.environ.get("LAKE_BUCKET", ""), org_api_function=org_fn,
        rag_search_function=os.environ.get("RAG_SEARCH_FUNCTION", ""),
        budget=budget, checkpoint=checkpoint, clock=clock,
        rag_client_factory=rag_lambda_client)

    runner = stage_runner or run_stages
    try:
        payload = runner(ctx)
        if not isinstance(payload, dict):
            raise StageError("stage runner returned no payload")
        checkpoint()                    # a cancel during the last round never gets stored
    except Cancelled:
        logger.info("action agent cancelled pack=%s", pack_id)
        return "cancelled"
    except Exception as exc:  # noqa: BLE001 -- every failure is a reason code, never a crash
        reason = failure_reason(exc)
        logger.warning("action agent stage error pack=%s reason=%s (%s)", pack_id, reason,
                       type(exc).__name__)
        return _fail(lambda_client, org_fn, pack_id, reason,
                     run_trace=getattr(exc, "run_trace", None))

    try:
        answer = _invoke_org(lambda_client, org_fn,
                             {"task": "action_pack_store", "pack_id": pack_id,
                              "payload": payload})
    except Exception as exc:  # noqa: BLE001
        logger.error("action agent store failed pack=%s (%s)", pack_id, type(exc).__name__)
        return _fail(lambda_client, org_fn, pack_id, "internal")
    if answer.get("ok") is True:
        logger.info("action agent stored pack=%s sources=%d actions=%d elapsed_s=%d",
                    pack_id, len(payload.get("sources") or []),
                    len(payload.get("actions") or []), int(clock() - start))
        return "stored"
    if answer.get("error") == "refused":
        logger.info("action agent store refused pack=%s (no longer running)", pack_id)
        return "refused"
    logger.error("action agent store rejected pack=%s error=%s detail=%s", pack_id,
                 answer.get("error"), answer.get("detail"))
    return _fail(lambda_client, org_fn, pack_id, "internal")


def lambda_handler(event, context):
    outcomes = []
    records = (event or {}).get("Records") or []
    if not action_pack_view.enabled():
        return {"ok": True, "outcomes": ["disabled"] * max(1, len(records))}
    remaining_ms = (context.get_remaining_time_in_millis()
                    if context is not None and hasattr(context, "get_remaining_time_in_millis")
                    else 900_000)
    started = time.monotonic()
    s3, lam = _s3(), _lambda()
    for rec in records:
        key = unquote_plus(((rec.get("s3") or {}).get("object") or {}).get("key") or "")
        left = remaining_ms / 1000.0 - (time.monotonic() - started)
        outcomes.append(process_key(key, s3=s3, lambda_client=lam, remaining_s=left))
    return {"ok": True, "outcomes": outcomes}
