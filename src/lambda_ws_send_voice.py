"""
In-VPC Lambda: WebSocket `sendVoice` route for Site Voice.

Body {siteId, s3Key, durationS}. Verifies the sender is a member of siteId
(same ACL floor as org-api: memberships.accessible_site_ids), records the
delivery pointer (voice_messages), resolves online recipients (connected
members of the site minus the sender) and async-invokes the NON-VPC
voice-fanout Lambda to POST over @connections. BUG-36: an in-VPC fn cannot
reach the execute-api endpoint, so the broadcast is split into that hop.
"""
import json
import logging
import os

import boto3

from db.connection import get_connection
from repositories import memberships, users, voice_messages, ws_connections

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FANOUT_FUNCTION = os.environ.get("VOICE_FANOUT_FUNCTION", "")

_lambda_client = None


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def lambda_handler(event, context):
    rc = event.get("requestContext", {}) or {}
    sub = (rc.get("authorizer") or {}).get("sub")
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"statusCode": 400, "body": "malformed body"}
    site_id = body.get("siteId")
    s3_key = body.get("s3Key")
    duration_s = body.get("durationS")
    if not (sub and site_id and s3_key):
        return {"statusCode": 400, "body": "siteId and s3Key required"}
    try:
        with get_connection() as conn:
            caller = users.get_user_by_sub(conn, sub)
            if caller is None or not caller["company_id"]:
                return {"statusCode": 403, "body": "not provisioned"}
            allowed = {str(x) for x in memberships.accessible_site_ids(
                conn, caller["id"], caller["global_role"])}
            if str(site_id) not in allowed:
                return {"statusCode": 403, "body": "not a member of site"}
            msg = voice_messages.insert_message(
                conn, caller["company_id"], site_id, caller["id"], s3_key,
                duration_s=duration_s)
            recipients = ws_connections.recipients_for_site(
                conn, caller["company_id"], site_id, caller["id"])
        _dispatch_fanout(rc, recipients, msg, caller)
        return {"statusCode": 200, "body": json.dumps(
            {"messageId": str(msg["id"]), "recipients": len(recipients)})}
    except Exception:
        logger.exception("sendVoice failed")
        return {"statusCode": 500, "body": "internal error"}


def _dispatch_fanout(rc, recipients, msg, caller):
    """Async-invoke the non-VPC fanout with the connection list + payload.
    Best-effort: never fail the send if the async hop can't be queued (and
    skip entirely when nobody else is online)."""
    if not recipients or not FANOUT_FUNCTION:
        return
    endpoint = f"https://{rc.get('domainName')}/{rc.get('stage')}"
    payload = {
        "type": "voice",
        "messageId": str(msg["id"]),
        "siteId": str(msg["site_id"]),
        "s3Key": msg["s3_key"],
        "durationS": float(msg["duration_s"]) if msg.get("duration_s") is not None else None,
        "senderUserId": str(caller["id"]),
        "createdAt": msg["created_at"].isoformat() if hasattr(msg["created_at"], "isoformat") else str(msg["created_at"]),
    }
    try:
        _lambda().invoke(
            FunctionName=FANOUT_FUNCTION, InvocationType="Event",
            Payload=json.dumps({"endpoint": endpoint,
                                "connectionIds": recipients, "payload": payload}))
    except Exception:
        logger.exception("fanout dispatch failed")
