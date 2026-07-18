"""
In-VPC leaf for Site Voice sendVoice. Does the Aurora work — authorize the
sender is a member of siteId (same ACL floor as org-api), insert the
voice_messages pointer, and resolve the online recipients — then RETURNS the
fanout payload + recipient connection ids. A LEAF: makes NO outbound AWS API
call (BUG-36 safe), so it is safe in-VPC (no NAT/no egress). The non-VPC
sendVoice orchestrator sync-invokes this, then does the @connections fanout.
"""
import logging

from db.connection import get_connection
from repositories import memberships, users, voice_messages, ws_connections

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    # event = {"sub", "siteId", "s3Key", "durationS"} from the non-VPC sendVoice
    sub = event.get("sub")
    site_id = event.get("siteId")
    s3_key = event.get("s3Key")
    duration_s = event.get("durationS")
    if not (sub and site_id and s3_key):
        return {"statusCode": 400}
    try:
        with get_connection() as conn:
            caller = users.get_user_by_sub(conn, sub)
            if caller is None or not caller["company_id"]:
                return {"statusCode": 403}
            allowed = {str(x) for x in memberships.accessible_site_ids(
                conn, caller["id"], caller["global_role"])}
            if str(site_id) not in allowed:
                return {"statusCode": 403}
            msg = voice_messages.insert_message(
                conn, caller["company_id"], site_id, caller["id"], s3_key,
                duration_s=duration_s)
            recipients = ws_connections.recipients_for_site(
                conn, caller["company_id"], site_id, caller["id"])
        payload = {
            "type": "voice",
            "messageId": str(msg["id"]),
            "siteId": str(msg["site_id"]),
            "s3Key": msg["s3_key"],
            "durationS": float(msg["duration_s"]) if msg.get("duration_s") is not None else None,
            "senderUserId": str(caller["id"]),
            "createdAt": msg["created_at"].isoformat() if hasattr(msg["created_at"], "isoformat") else str(msg["created_at"]),
        }
        return {"statusCode": 200, "messageId": str(msg["id"]),
                "connectionIds": recipients, "payload": payload}
    except Exception:
        logger.exception("voice-resolve failed")
        return {"statusCode": 500}
