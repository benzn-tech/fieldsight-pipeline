"""
In-VPC Lambda: write one voice_ask_log audit row (SP-Ask).

Async-invoked (InvocationType='Event') by the non-VPC AskAgentFunction after a
voice ask completes: AskAgent cannot reach Aurora (BUG-36), so the audit write
is split into this in-VPC hop. Best-effort: never raises out -- a failed audit
does not matter to the already-returned ask.

Event: {"caller_sub": "...", "transcript": "...", "answer": "..."}
"""
import logging

from db.connection import get_connection
from repositories import users, voice_ask_log

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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
        return {"written": True, "id": row_id}
    except Exception as e:
        logger.error("voice audit write failed: %s", e)
        return {"written": False, "error": str(e)}
