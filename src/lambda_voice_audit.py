"""
In-VPC Lambda: write one voice_ask_log audit row (SP-Ask), and publish what the agent said
where the extraction path can read it.

Async-invoked (InvocationType='Event') by the non-VPC AskAgentFunction after a
voice ask completes: AskAgent cannot reach Aurora (BUG-36), so the audit write
is split into this in-VPC hop. Best-effort: never raises out -- a failed audit
does not matter to the already-returned ask.

The sidecar exists because the device plays the answer aloud into a running recording, so the
agent's words come back as ordinary speaker turns and end up in reports and in the search index
-- which is what the agent reads the next answer out of. Cutting that loop needs the answer text
at extraction time, and ExtractSessionFunction is non-VPC with no database (it calls an LLM, so
it cannot be in the VPC). This function is already in-VPC, already receives the answer, and
already resolves the user row that gives the folder name, so it is the one place that can write
a key the extractor can find.

ONE OBJECT PER ASK. S3 has no append, so a shared per-day file would be a read-modify-write:
two asks eight seconds apart (measured) would lose one, and the at-least-once `Event` retry
would duplicate the other.

Event: {"caller_sub": "...", "transcript": "...", "answer": "..."}
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import boto3

from db.connection import get_connection
from repositories import users, voice_ask_log

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET")
VOICE_ASK_PREFIX = os.environ.get("VOICE_ASK_PREFIX", "voice_ask/")
# Device wall clocks are NZ-local and so are the transcript paths; the date in the key has to
# match the reader's, or a 09:00 ask files under yesterday in UTC and nobody ever looks there.
DEVICE_TZ = ZoneInfo(os.environ.get("DEVICE_TZ", "Pacific/Auckland"))


def _write_sidecar(folder_name, answer):
    """Publish one answer for the extraction path. Returns the key, or None if not written."""
    if not (S3_BUCKET and folder_name and answer):
        return None
    now_utc = datetime.now(timezone.utc)
    date_local = now_utc.astimezone(DEVICE_TZ).date().isoformat()
    key = (f"{VOICE_ASK_PREFIX}{folder_name}/{date_local}/"
           f"{now_utc.strftime('%H%M%S')}-{uuid.uuid4().hex[:8]}.json")
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET, Key=key, ContentType="application/json",
        Body=json.dumps({"at_utc": now_utc.isoformat(), "answer": answer}).encode("utf-8"),
    )
    return key


def lambda_handler(event, context):
    caller_sub = event.get("caller_sub")
    if not caller_sub:
        return {"written": False, "error": "missing caller_sub"}
    try:
        # `with get_connection() as conn:` commits on clean exit (db/connection.py).
        with get_connection() as conn:
            caller = users.get_user_by_sub(conn, caller_sub)
            company_id = caller["company_id"] if caller else None
            row_id = voice_ask_log.insert_voice_ask(
                conn, caller_sub, event.get("transcript"), event.get("answer"),
                company_id=company_id)
            folder_name = (caller or {}).get("folder_name")
        # Outside the DB block: a sidecar failure must not roll back the audit row, and the two
        # are independent records. Logged loudly rather than swallowed -- a missing sidecar is a
        # session whose agent turns silently stay in the report, and the only way anyone finds
        # out is this line plus the extractor's unmatched-answer counter.
        sidecar_key = None
        try:
            sidecar_key = _write_sidecar(folder_name, event.get("answer"))
        except Exception as e:
            logger.error("voice ask sidecar write failed (agent turns will NOT be filtered "
                         "for this ask): %s", e)
        return {"written": True, "id": row_id, "sidecar": sidecar_key}
    except Exception as e:
        logger.error("voice audit write failed: %s", e)
        return {"written": False, "error": str(e)}
