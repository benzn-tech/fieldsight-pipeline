"""
Lambda: nightly-turn-pick -- hop 2 of the nightly J2 turn pick (non-VPC worker).

Spec: docs/superpowers/specs/2026-11-10-nightly-rematch-agent-design.md §3 J2 (worker,
guards), §4.2 (three-hop pattern, model-call layer), §6 (one model call per session).

S3-triggered on `nightly_requests/{site_id}/{night}/{hash}.json` (written in-VPC by the hub's
nightly_rematch handler). For one request:
  1. Read the artifact. Idle (no transcript read, no model call, no invoke) unless its
     `flag` is "on" -- in shadow the worker is not wired, and this is the second lock.
  2. Find THIS session's transcript keys with the extractor's own key discovery and rebuild
     its turns with the extractor's own chain (session_turns), so chunk `sid…` sessions
     resolve exactly as they do in extraction. A group (`grp…`) session has no single-session
     keys and is not handled in v1: no call; its items are answered abstained (below).
  3. nightly_turn_pick.build_request -> render_prompt -> ONE model call (registry entry
     `nightly_turn_pick`, Claude API with ANTHROPIC_API_KEY, SDK retries off) ->
     parse_answer -> apply_guards.
  4. Invoke the in-VPC writer (lambda_nightly_support_writer) RequestResponse with ids,
     enums, locators and usage -- never text.

Every `on` request with a session is answered (the writer is invoked), so the hub's circuit
breaker sees an answer and J2's no-re-ask rule stops a nightly re-send. Items the model is
never asked about go to the writer as rows with outcome `abstained`, turn None, no locator
and a guard naming the reason: `no_transcript` (no segments left), `unsupported_session`
(a group session), or build_request's own `no_text` / `no_window`. When no model call was
made at all, the usage is zero tokens, cost None and `model_call: false` (the run's
model_calls is not incremented). build_request's `over_cap` items get no row: they stay
askable on a later night.

Fail closed (raise, so the S3 event's single retry runs; the template sets
MaximumRetryAttempts 1): segments listed but no readable turns, a model API error, a
truncated (`max_tokens`) or refused answer, an unparseable answer, a writer FunctionError or a
non-200 invoke. Nothing is retried inside the worker. After a paid call whose answer is
unusable (truncated, refused, unparseable) the worker first invokes the writer with
`rows: []` so the call's tokens land on the run, then raises the original error.

What leaves for the model: the request's item text (Track C §5.2 whitelist) and this
session's own turns inside each item's topic window, fenced as quoted data. Never `origin`,
never another session's text (build_request copies only the item fields it needs; key
discovery returns only this session's segments).

Logs carry ids, counts and enums only -- never item, turn or answer text.

Environment Variables:
    S3_BUCKET               - the lake bucket (artifact and transcripts)
    SUPPORT_WRITER_FUNCTION - name of the in-VPC writer to invoke
    ANTHROPIC_API_KEY       - the Claude API key
"""
import json
import logging
import os
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config

import model_registry
import nightly_turn_pick as ntp
import session_turns

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
SUPPORT_WRITER_FUNCTION = os.environ.get("SUPPORT_WRITER_FUNCTION", "")
PREFIX = "nightly_requests/"
REGISTRY_TASK = "nightly_turn_pick"
# The writer's Timeout is 120 s: wait for it, and never let botocore retry the invoke.
WRITER_READ_TIMEOUT_S = 130
# Upper bound on the model call (seconds). The function Timeout (template: 300) must cover
# this + the writer wait (WRITER_READ_TIMEOUT_S) + the turn rebuild; a call cut off by the
# Lambda Timeout would be paid twice (the S3 retry). Pinned by tests/unit/test_nightly_rematch.py.
MODEL_TIMEOUT_S = 150.0

_s3_client = None
_lambda_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def writer_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", config=Config(
            read_timeout=WRITER_READ_TIMEOUT_S, connect_timeout=5,
            retries={"max_attempts": 0}))
    return _lambda_client


def make_model_client():
    """An Anthropic SDK client on ANTHROPIC_API_KEY with SDK retries off (one call per
    session; the S3 retry is the only retry). The SDK is imported here, so nothing else in
    this module needs it installed."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    import anthropic
    return anthropic.Anthropic(api_key=key, max_retries=0, timeout=MODEL_TIMEOUT_S)


def call_model(client, entry, prompt):
    """(text, tokens_in, tokens_out, stop_reason) of one call. An API error propagates (no
    response, so no usage to record).

    The request is built from the registry entry: model, max_tokens, output_config.effort;
    `temperature` only when the entry sets one (None = not sent)."""
    kwargs = {"model": entry["model"], "max_tokens": entry["max_tokens"],
              "messages": [{"role": "user", "content": prompt}]}
    if entry.get("effort"):
        kwargs["output_config"] = {"effort": entry["effort"]}
    if entry.get("temperature") is not None:
        kwargs["temperature"] = entry["temperature"]
    resp = client.messages.create(**kwargs)
    usage = getattr(resp, "usage", None)
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    stop = getattr(resp, "stop_reason", None)
    text = "".join(getattr(b, "text", "") or "" for b in (resp.content or [])
                   if getattr(b, "type", None) == "text")
    return text, tokens_in, tokens_out, stop


def _invoke_writer(client, payload):
    resp = client.invoke(FunctionName=SUPPORT_WRITER_FUNCTION,
                         InvocationType="RequestResponse", Payload=payload)
    # A crashed writer comes back as a 200 with FunctionError set -- never "written".
    if resp.get("FunctionError"):
        raise RuntimeError(f"nightly-support-writer invoke failed: FunctionError "
                           f"{resp.get('FunctionError')}")
    if resp.get("StatusCode") != 200:
        raise RuntimeError(f"nightly-support-writer invoke status {resp.get('StatusCode')}")
    body = resp.get("Payload")
    raw = body.read() if hasattr(body, "read") else body
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _writer_rows(rows, turns):
    """The guarded rows minus anything the writer must not see: no text, no session-wide
    turn index; the turn's locator instead."""
    out = []
    for r in rows:
        idx = r.get("turn_index")
        loc = ntp.turn_locator(turns[idx]) if isinstance(idx, int) else None
        out.append({"stable_id": r.get("stable_id"), "row_id": r.get("row_id"),
                    "kind": r.get("kind"), "turn": r.get("turn") if loc else None,
                    "outcome": r.get("outcome"), "guard": r.get("guard"),
                    "cross_language": bool(r.get("cross_language")), "locator": loc})
    return out


def _abstained_rows(items, guard=None):
    """Writer rows (outcome abstained, no turn, no locator) for items the model is never
    asked about. `guard` names the reason; None takes each item's own `reason`
    (build_request's `skipped_items`)."""
    return [{"stable_id": it.get("stable_id"), "row_id": it.get("row_id"),
             "kind": it.get("kind"), "turn": None, "outcome": "abstained",
             "guard": guard or it.get("reason"), "cross_language": False, "locator": None}
            for it in items]


def _payload(request, key, rows, usage):
    return {"site_id": request.get("site_id"), "run_id": request.get("run_id"),
            "extraction_key": request.get("extraction_key"),
            "session_base": request.get("session_base") or "",
            "question_set": ntp.PROMPT_HASH, "input_key": key, "rows": rows, "usage": usage}


# Usage for an answer given without a model call: zero tokens, cost NULL (not 0), and
# model_call False so the run's model_calls / models stay honest.
NO_CALL_USAGE = {"tokens_in": 0, "tokens_out": 0, "model": None, "cost_usd": None,
                 "model_call": False}


def _answer_without_call(lambda_client, request, key, rows, counts, reason):
    """Every `on` request is answered: items the model can never be asked about go to the
    writer as abstained rows, so the hub's breaker sees an answer and J2 does not re-send
    the session every night."""
    result = _invoke_writer(lambda_client, json.dumps(
        _payload(request, key, rows, dict(NO_CALL_USAGE))))
    logger.info("nightly turn pick: answered without a call key=%s site=%s reason=%s rows=%d "
                "writer=%s", key, request.get("site_id"), reason, len(rows),
                json.dumps(result, sort_keys=True))
    return dict(counts, abstained_without_call=len(rows), writer=result)


def process_request(bucket, key, request, *, s3_client, lambda_client, model_client=None):
    """One request body -> a counts dict. Raises to fail closed (module docstring)."""
    site_id = request.get("site_id")
    base = request.get("session_base") or ""
    counts = {"key": key, "site_id": site_id}
    req_items = list(request.get("items") or [])

    if (request.get("flag") or "off") != "on":
        logger.info("nightly turn pick: idle (flag=%s) key=%s", request.get("flag"), key)
        return dict(counts, idle=1)
    if not base:
        # Malformed: the writer needs a session to write against. The hub never writes one.
        logger.warning("nightly turn pick: request has no session_base key=%s", key)
        return dict(counts, unsupported_session=1)
    if base.startswith("grp"):
        logger.info("nightly turn pick: session kind not handled key=%s", key)
        return _answer_without_call(lambda_client, request, key,
                                    _abstained_rows(req_items, "unsupported_session"),
                                    dict(counts, unsupported_session=1), "unsupported_session")

    seg_keys = session_turns.gather_session_segments(
        bucket, request["user_folder"], request["date"], base, s3_client=s3_client)
    if not seg_keys:
        # The transcripts were removed since the extraction read them: nothing to match.
        logger.warning("nightly turn pick: no transcript segments key=%s", key)
        return _answer_without_call(lambda_client, request, key,
                                    _abstained_rows(req_items, "no_transcript"),
                                    dict(counts, no_transcript=1), "no_transcript")
    turns, _sources, _stats = session_turns.assemble_session_turns(
        bucket, seg_keys, s3_client=s3_client)
    if not turns:
        # assemble swallows a failed GetObject per segment: listed keys with no turns is more
        # likely a denied read than silence.
        raise RuntimeError(f"nightly turn pick {key}: {len(seg_keys)} segment(s) listed but "
                           f"no readable turns")

    req = ntp.build_request(req_items, turns, request["date"])
    counts.update(items=len(req["items"]), skipped=req["skipped"], turns=len(turns))
    skipped_rows = _abstained_rows(req["skipped_items"])
    if not req["items"]:
        logger.info("nightly turn pick: no item to ask key=%s skipped=%s", key, req["skipped"])
        return _answer_without_call(lambda_client, request, key, skipped_rows, counts,
                                    "no_item_to_ask")

    entry = model_registry.get(REGISTRY_TASK)
    client = model_client or make_model_client()
    text, tokens_in, tokens_out, stop = call_model(client, entry, ntp.render_prompt(req, turns))
    payload = _payload(request, key, [], {
        "tokens_in": tokens_in, "tokens_out": tokens_out, "model": entry["model"],
        "cost_usd": model_registry.cost_usd(entry, tokens_in, tokens_out)})

    failure, picks = None, None
    if stop in ("max_tokens", "refusal"):
        failure = RuntimeError(f"nightly turn pick {key}: model answer unusable: "
                               f"stop_reason={stop} (tokens_out={tokens_out})")
    else:
        try:
            picks = ntp.parse_answer(text)
        except ValueError:
            # The answer is never put in the message: it may quote the transcript.
            failure = RuntimeError(f"nightly turn pick {key}: parse error ({len(text)} chars)")
    if failure is not None:
        # The call was paid: record its tokens (rows [] -> the writer only adds usage), then
        # fail closed with the original error whatever that invoke did.
        try:
            _invoke_writer(lambda_client, json.dumps(payload))
        except Exception as e:  # noqa: BLE001 -- the original error is the one raised
            logger.warning("nightly turn pick: usage-only invoke failed key=%s site=%s: %s",
                           key, site_id, type(e).__name__)
        raise failure

    rows = ntp.apply_guards(req, picks, turns)
    # The items build_request skipped are answered too (abstained), in the same invoke.
    payload["rows"] = _writer_rows(rows, turns) + skipped_rows
    result = _invoke_writer(lambda_client, json.dumps(payload))

    outcomes = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    guards = {}
    for r in rows:
        if r.get("guard"):
            guards[r["guard"]] = guards.get(r["guard"], 0) + 1
    counts.update(picks=len(picks), outcomes=outcomes, guards=guards,
                  tokens_in=tokens_in, tokens_out=tokens_out, writer=result)
    logger.info("nightly turn pick: key=%s site=%s items=%d turns=%d outcomes=%s guards=%s "
                "tokens_in=%d tokens_out=%d writer=%s", key, site_id, len(req["items"]),
                len(turns), outcomes, guards, tokens_in, tokens_out,
                json.dumps(result, sort_keys=True))
    return counts


def process_artifact(bucket, key, *, s3_client=None, lambda_client=None, model_client=None):
    """Read one request artifact and process it."""
    s3c = s3_client or s3()
    body = s3c.get_object(Bucket=bucket, Key=key)["Body"].read()
    request = json.loads(body)
    return process_request(bucket, key, request, s3_client=s3c,
                           lambda_client=lambda_client or writer_client(),
                           model_client=model_client)


def lambda_handler(event, _context):
    out = []
    for rec in (event or {}).get("Records") or []:
        s3info = rec.get("s3") or {}
        bucket = (s3info.get("bucket") or {}).get("name") or S3_BUCKET
        key = unquote_plus((s3info.get("object") or {}).get("key") or "")
        if not key.startswith(PREFIX):
            logger.info("nightly turn pick: ignoring key outside %s", PREFIX)
            continue
        out.append(process_artifact(bucket, key))
    return {"processed": len(out)}
