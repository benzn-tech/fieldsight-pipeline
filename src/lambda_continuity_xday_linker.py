"""
Lambda: continuity-xday-linker -- hop 2 of cross-day item continuity.

Spec: docs/superpowers/specs/2026-11-09-cross-day-continuity-design.md (X1, X3, X6, X7, X9, X10).

Non-VPC (it calls the model over public HTTPS; BUG-36 -- an in-VPC lambda has no egress).
S3-triggered on `continuity_xday_requests/{site_id}/{date}/{run_hash}.json`, the request hop 1
(the in-VPC item writer) puts after its commit (wired by scripts/wire-s3-events.sh, BUG-33).

Per record:
  1. Gate (X1): idle -- no S3 read, no model call, no invoke -- unless BOTH this function's
     CROSS_DAY_CONTINUITY env is "on" AND the artifact's own `mode` is "on". A request written
     in shadow stays a measurement artifact even after the linker is switched on.
  2. One model call (X6): `continuity_xday.render_prompt`, thinking off, the stage's deployed
     extraction model (QWEN_MODEL / QWEN_MODEL_NONTHINKING env, as MatcherFunction), caller tag
     `continuity_xday`. Parsed with `llm_utils.extract_json(raw, quiet=True)`.
  3. Guards (X7): `continuity_xday.judge` -- only what it accepts becomes a link.
  4. One RequestResponse invoke of the in-VPC link writer (hop 3, lambda_continuity_link_writer)
     with {"request": <request, every item text removed>, "links", "claims", "model"}.

Failure (X10):
  * model error / unparseable answer -> nothing invoked, counts logged, NO raise: a retry would
    only repeat the paid call, and no links is the safe outcome.
  * an exception in judge / strip_texts / the payload build after a good model answer -> the
    same: nothing invoked, `type(e).__name__` logged, XdayLinks Outcome=judge_failed, NO raise.
  * malformed artifact -> logged and skipped (a retry reads the same bytes).
  * writer FunctionError or StatusCode != 200 -> RAISE, so S3's async retry is fail-closed
    (as lambda_programme_matcher does). EventInvokeConfig.MaximumRetryAttempts is pinned to 1
    in the template: each retry is a second paid model call.

Metrics (X9, Ruling R9): EMF `XdayLinks` by `Outcome` -- accepted, rejected_<guard family>
(echo_* collapse to `echo`), fuse_tripped, inserted, skipped -- one EMF line per value, the
CarriedByMethod shape (carry_forward_apply). Counts only.

Logs and metrics carry keys, ids and counts only -- never item text, the prompt, the model's
answer or an exception message (log `type(e).__name__`).

Environment Variables:
    S3_BUCKET            - lake bucket holding continuity_xday_requests/ (IngestBucketName)
    LINK_WRITER_FUNCTION - name of the in-VPC link writer to invoke
    CROSS_DAY_CONTINUITY - off (default) | shadow | on; only "on" does anything here
    STAGE                - EMF dimension
    LLM_* / QWEN_* / ANTHROPIC_API_KEY / CLAUDE_MODEL - read by llm_utils
"""
import json
import logging
import os
import time
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config

import continuity_xday
import llm_utils

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
LINK_WRITER_FUNCTION = os.environ.get("LINK_WRITER_FUNCTION", "")

CALLER = "continuity_xday"
MAX_TOKENS = 4096
# Seconds kept back from the model chain for the writer invoke (its Timeout is 120) and the
# return; the template's LLM_CHAIN_BUDGET_SECONDS caps the chain the same way.
WRITER_RESERVE_S = 130.0

_s3_client = None
_lambda_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def lambda_client():
    global _lambda_client
    if _lambda_client is None:
        # The writer's Timeout is 120 s: wait for it (botocore's default read timeout is 60 s),
        # and never let botocore retry the invoke -- the S3 retry is the only retry.
        _lambda_client = boto3.client("lambda", config=Config(
            read_timeout=130, connect_timeout=5, retries={"max_attempts": 0}))
    return _lambda_client


def _flag():
    """Read per call so a test (or a hot config change) is seen."""
    return (os.environ.get("CROSS_DAY_CONTINUITY", "off") or "off").strip().lower()


# --- pure helpers -----------------------------------------------------------------------------

def strip_texts(request):
    """The request minus every item text (the writer contract): `text` is dropped from each
    new_items[] and candidates[] entry; every other field is kept as given."""
    out = dict(request)
    for list_name in ("new_items", "candidates"):
        entries = request.get(list_name)
        if isinstance(entries, list):
            out[list_name] = [{k: v for k, v in e.items() if k != "text"}
                              if isinstance(e, dict) else e for e in entries]
    return out


def _guard_family(guard):
    return "echo" if isinstance(guard, str) and guard.startswith("echo_") else guard


def outcome_counts(counts, writer_result=None):
    """`{Outcome: n}` for the XdayLinks metric, from judge's counts and the writer's return.

    Every rejected guard family judge emitted gets a `rejected_<family>` entry; inserted and
    skipped are zero when the writer was not reached."""
    values = {"accepted": int(counts.get("accepted", 0)),
              "fuse_tripped": 1 if counts.get("fuse_tripped") else 0}
    for guard, n in (counts.get("by_guard") or {}).items():
        key = "rejected_" + str(_guard_family(guard))
        values[key] = values.get(key, 0) + int(n)
    wr = writer_result if isinstance(writer_result, dict) else {}
    values["inserted"] = int(wr.get("inserted") or 0)
    skipped = wr.get("skipped")
    if isinstance(skipped, dict):
        values["skipped"] = sum(int(n or 0) for n in skipped.values())
    else:
        values["skipped"] = int(skipped or 0)
    return values


def _emit_metric(key, values):
    """One XdayLinks EMF line per outcome (EMF wants one dimension set per metric name, and
    Outcome has one value per line). Never raises."""
    try:
        ts = int(time.time() * 1000)
        stage = os.environ.get("STAGE", "unknown")
        for outcome, n in values.items():
            print(json.dumps({
                "_aws": {
                    "Timestamp": ts,
                    "CloudWatchMetrics": [{
                        "Namespace": "FieldSight/Pipeline",
                        "Dimensions": [["Stage", "Outcome"]],
                        "Metrics": [{"Name": "XdayLinks", "Unit": "Count"}],
                    }],
                },
                "Stage": stage,
                "Outcome": outcome,
                "XdayLinks": n,
                "key": key,
            }))
    except Exception:  # noqa: BLE001 -- a metric must not fail the hop
        logger.warning("could not emit the XdayLinks metric for %s", key)


def _deadline(context):
    """Seconds the model chain may take: the invocation's remaining time minus the writer's
    window. None when no context is known (the template's chain budget still applies)."""
    try:
        left = float(context.get_remaining_time_in_millis()) / 1000.0
    except Exception:  # noqa: BLE001 -- no context: rely on LLM_CHAIN_BUDGET_SECONDS
        return None
    return max(left - WRITER_RESERVE_S, 1.0)


# --- one request ------------------------------------------------------------------------------

def _writer_payload(request, links, claims, model):
    return json.dumps({"request": strip_texts(request), "links": links, "claims": claims,
                       "model": model}, ensure_ascii=False)


def _invoke_writer(payload):
    resp = lambda_client().invoke(
        FunctionName=LINK_WRITER_FUNCTION,
        InvocationType="RequestResponse",
        Payload=payload,
    )
    # A crashed writer comes back as a 200 with FunctionError set -- never "written".
    if resp.get("FunctionError"):
        raise RuntimeError(f"continuity-link-writer invoke failed: {resp.get('FunctionError')}")
    if resp.get("StatusCode") != 200:
        raise RuntimeError(f"continuity-link-writer invoke status {resp.get('StatusCode')}")
    body = resp.get("Payload")
    raw = body.read() if hasattr(body, "read") else body
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def process_request(key, request, context=None):
    """Model -> judge -> writer for one request. Returns a counts dict; raises only on a
    failed writer invoke (everything between the model call and the invoke is caught: an S3
    retry would repeat the paid call)."""
    if request.get("mode") != "on":
        logger.info("xday linker: idle (artifact mode=%s) key=%s",
                    request.get("mode"), key)
        return {"idle": 1}

    info = {}
    raw, err = llm_utils.call_llm(
        continuity_xday.render_prompt(request), max_tokens=MAX_TOKENS, force_json=True,
        enable_thinking=False, deadline=_deadline(context), caller=CALLER, info=info)
    parsed = None
    if err is None and raw:
        try:
            parsed = llm_utils.extract_json(raw, quiet=True)
        except Exception:  # noqa: BLE001 -- unparseable is the same outcome as no answer
            parsed = None
    n_new = len(request.get("new_items") or [])
    n_cand = len(request.get("candidates") or [])
    if not isinstance(parsed, dict):
        logger.warning("xday linker: model %s, nothing invoked: key=%s run_hash=%s new=%d "
                       "candidates=%d", "error" if err is not None else "answer unparseable",
                       key, request.get("run_hash"), n_new, n_cand)
        _emit_metric(key, {"model_failed": 1})
        return {"model_failed": 1}

    try:
        links, out = continuity_xday.judge(request, parsed)
        counts = out["counts"]
        model = {"provider": llm_utils.LLM_PROVIDER, "model": info.get("model"),
                 "model_version": info.get("served_model")}
        payload = _writer_payload(request, links, out["claims"], model)
    except Exception as e:  # noqa: BLE001 -- a retry would only repeat the paid model call
        logger.warning("xday linker: judge failed, nothing invoked: key=%s run_hash=%s "
                       "new=%d candidates=%d error=%s", key, request.get("run_hash"), n_new,
                       n_cand, type(e).__name__)
        _emit_metric(key, {"judge_failed": 1})
        return {"judge_failed": 1}
    result = _invoke_writer(payload)
    try:
        values = outcome_counts(counts, result)
    except Exception as e:  # noqa: BLE001 -- the writer already committed: never retry for this
        logger.warning("xday linker: writer result unreadable: key=%s error=%s", key,
                       type(e).__name__)
        result, values = None, outcome_counts(counts)
    logger.info("xday linker: key=%s run_hash=%s new=%d candidates=%d entries=%d dropped=%d "
                "claims=%d accepted=%d rejected=%d by_guard=%s fuse=%s inserted=%s "
                "skipped=%s records=%s", key, request.get("run_hash"), counts["new_items"],
                counts["candidates"], counts["entries"], counts["dropped_entries"],
                counts["claims"], counts["accepted"], counts["rejected"],
                json.dumps(counts["by_guard"], sort_keys=True), counts["fuse_tripped"],
                values["inserted"], values["skipped"],
                result.get("records") if isinstance(result, dict) else None)
    _emit_metric(key, values)
    return {"model_failed": 0, **values}


def lambda_handler(event, context):
    flag = _flag()
    if flag != "on":
        logger.info("xday linker: idle (CROSS_DAY_CONTINUITY=%s), %d record(s)",
                    flag, len(event.get("Records") or []))
        return {"idle": len(event.get("Records") or []), "model_failed": 0}

    totals = {"idle": 0, "model_failed": 0, "judge_failed": 0, "processed": 0,
              "malformed": 0}
    for record in event.get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])
        obj = s3().get_object(Bucket=S3_BUCKET, Key=key)
        try:
            request = json.loads(obj["Body"].read())
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
        except Exception as e:  # noqa: BLE001 -- a retry reads the same bytes
            logger.warning("xday linker: malformed request skipped: key=%s error=%s",
                           key, type(e).__name__)
            totals["malformed"] += 1
            continue
        res = process_request(key, request, context)
        if res.get("idle"):
            totals["idle"] += 1
        elif res.get("model_failed"):
            totals["model_failed"] += 1
        elif res.get("judge_failed"):
            totals["judge_failed"] += 1
        else:
            totals["processed"] += 1
    return totals
