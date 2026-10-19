"""
Lambda: check-gaps -- "check done": which checklist items are still open
(spec docs/superpowers/specs/2026-10-20-check-done-gaps-design.md).

Invoked synchronously by the Ask voice worker; no API route. Outside the VPC
because it calls the STT vendor and the model; the database facts come from
CheckGapsDbFunction (in-VPC, no egress), the AskAgent -> RagSearch split.

Event:  {"caller_sub", "check_name", "spoken", "pressed_at"}
Result: check_done.run's response -- always a dict with `status` and `say`.

Environment:
    S3_BUCKET               - the lake (transcripts/, users/*/audio/)
    CHECK_GAPS_DB_FUNCTION  - the in-VPC hop
    ELEVENLABS_ASK_API_KEY  - the Ask STT key; absent = stored transcript only
    LLM_* / QWEN_*          - llm_utils, the Ask worker's fast model
"""
import json
import logging
import os

import boto3
import urllib3
from botocore.config import Config

import check_done
import elevenlabs_utils
import llm_utils

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
CHECK_GAPS_DB_FUNCTION = os.environ.get("CHECK_GAPS_DB_FUNCTION", "")
# The hop's Timeout is 10 s, but a cold VPC start runs its init (psycopg import,
# ENI) BEFORE that clock starts, so 11 could cut a cold hop that would have
# answered. 13 still leaves the gap pass its 12 s inside the 30 s run budget
# (check_done.RUN_BUDGET_S) when the hop is warm, and a cold slow hop is the
# case the budget then cuts, by name.
DB_HOP_READ_TIMEOUT_S = 13

_s3 = None
_lambda = None


def _get_s3():
    global _s3
    if _s3 is None:
        # Bounded: a 30 s chunk is ~1 MB; a hung GET must not eat the STT phase.
        # Legacy retry mode counts RETRIES, so 1 is two attempts at most: one
        # retry on a transient 5xx, never a third.
        _s3 = boto3.client("s3", config=Config(connect_timeout=2, read_timeout=5,
                                               retries={"max_attempts": 1}))
    return _s3


def _get_lambda():
    global _lambda
    if _lambda is None:
        # retries 0 (legacy mode counts retries: 1 would be two attempts), as
        # lambda_fieldsight_api's ask clients: a retried DB hop is a second cold
        # start the person waits for.
        _lambda = boto3.client("lambda", config=Config(connect_timeout=2,
                                                       read_timeout=DB_HOP_READ_TIMEOUT_S,
                                                       retries={"max_attempts": 0}))
    return _lambda


def db_hop(payload):
    if not CHECK_GAPS_DB_FUNCTION:
        raise RuntimeError("CHECK_GAPS_DB_FUNCTION not set")
    resp = _get_lambda().invoke(FunctionName=CHECK_GAPS_DB_FUNCTION,
                                InvocationType="RequestResponse",
                                Payload=json.dumps(payload))
    body = json.loads(resp["Payload"].read().decode("utf-8") or "{}")
    if resp.get("FunctionError"):
        raise RuntimeError("check-gaps-db FunctionError: %s" % resp.get("FunctionError"))
    return body


def stt_chunk(audio_bytes, filename):
    """One 30 s chunk -> text. Modelled on elevenlabs_utils.stt_short, with three
    differences on purpose:

    * ONE attempt, check_done.STT_TIMEOUT_S. A retry spends the seconds the
      answer needs; a chunk not heard makes the answer `partial`, not wrong.
    * No diarisation, as stt_short: speakers are not what is asked here.
    * The ASK key, never the recording pipeline's (ELEVENLABS_API_KEY): its
      credits running out would stop recordings, and up to eight parallel posts
      per ask is the kind of load that could."""
    fields = [("model_id", elevenlabs_utils.ELEVENLABS_STT_MODEL),
              ("file", (filename, audio_bytes, "application/octet-stream"))]
    if elevenlabs_utils.ELEVENLABS_LANGUAGE:
        fields.append(("language_code", elevenlabs_utils.ELEVENLABS_LANGUAGE))
    resp = urllib3.PoolManager().request(
        "POST", elevenlabs_utils.ELEVENLABS_STT_URL, fields=fields,
        headers={"xi-api-key": elevenlabs_utils.ELEVENLABS_ASK_API_KEY},
        timeout=check_done.STT_TIMEOUT_S)
    if resp.status != 200:
        raise RuntimeError("ElevenLabs STT: HTTP %d %s" % (resp.status, resp.data[:200]))
    return (json.loads(resp.data).get("text") or "").strip()


def _gather(bucket, folder, date, session_base):
    # Lazy, as lambda_rolling_summary does: the extraction module is heavy at
    # import and only this step needs it. Its gather/assemble are used, not
    # copied, so the stored transcript is the one extraction and step B read.
    import lambda_extract_session as ex
    return ex.gather_session_segments(bucket, folder, date, session_base)


def _assemble(bucket, keys):
    import lambda_extract_session as ex
    return ex.assemble_deduped_turns(bucket, keys)


def lambda_handler(event, context):
    stt = stt_chunk if elevenlabs_utils.ELEVENLABS_ASK_API_KEY else None
    return check_done.run(event or {}, db_hop=db_hop, s3=_get_s3(), bucket=S3_BUCKET,
                          gather=_gather, assemble=_assemble, stt=stt,
                          call_llm=llm_utils.call_llm)
