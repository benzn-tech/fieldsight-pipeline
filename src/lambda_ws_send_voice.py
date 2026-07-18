"""
Non-VPC WS `sendVoice` orchestrator. It cannot touch Aurora (it is NON-VPC), so
it sync-invokes the IN-VPC voice-resolve leaf (ACL + insert voice_messages +
resolve recipients), then async-invokes the NON-VPC voice-fanout to POST the
payload over @connections. Non-VPC so BOTH lambda:Invoke calls have egress — an
in-VPC fn cannot reach the Lambda API (no NAT / no lambda VPC endpoint, BUG-36),
which is exactly why the DB work is delegated to the in-VPC leaf and the invokes
+ the @connections fanout run out here where egress exists.
"""
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RESOLVE_FUNCTION = os.environ.get("VOICE_RESOLVE_FUNCTION", "")
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
    if not RESOLVE_FUNCTION:
        logger.error("VOICE_RESOLVE_FUNCTION not set")
        return {"statusCode": 500}
    try:
        resp = _lambda().invoke(
            FunctionName=RESOLVE_FUNCTION, InvocationType="RequestResponse",
            Payload=json.dumps({"sub": sub, "siteId": site_id,
                                "s3Key": s3_key, "durationS": duration_s}))
        resolved = json.loads(resp["Payload"].read())
    except Exception:
        logger.exception("voice-resolve invoke failed")
        return {"statusCode": 500}
    status = resolved.get("statusCode", 500)
    if status != 200:
        return {"statusCode": status, "body": "not authorized for site"}
    recipients = resolved.get("connectionIds") or []
    payload = resolved.get("payload") or {}
    _dispatch_fanout(rc, recipients, payload)
    return {"statusCode": 200, "body": json.dumps(
        {"messageId": resolved.get("messageId"), "recipients": len(recipients)})}


def _dispatch_fanout(rc, recipients, payload):
    """Async-invoke the non-VPC fanout with the connection list + payload.
    Best-effort; skip when nobody else is online or the env is unset."""
    if not recipients or not FANOUT_FUNCTION:
        return
    endpoint = f"https://{rc.get('domainName')}/{rc.get('stage')}"
    try:
        _lambda().invoke(
            FunctionName=FANOUT_FUNCTION, InvocationType="Event",
            Payload=json.dumps({"endpoint": endpoint,
                                "connectionIds": recipients, "payload": payload}))
    except Exception:
        logger.exception("fanout dispatch failed")
