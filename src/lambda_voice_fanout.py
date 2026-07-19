"""
Non-VPC Lambda: broadcast a Site Voice payload to WebSocket connections.

Async-invoked (Event) by the in-VPC sendVoice Lambda with
{endpoint, connectionIds, payload}. POSTs the payload to each connection via
the API Gateway Management API (execute-api:ManageConnections). A
GoneException means the connection is dead — collect those ids and async-invoke
the in-VPC voice-reaper to delete their ws_connections rows (replaces
DynamoDB TTL). This split exists because an in-VPC fn cannot reach the
execute-api endpoint (no NAT / no VPC endpoint — BUG-36).
"""
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REAPER_FUNCTION = os.environ.get("VOICE_REAPER_FUNCTION", "")

_lambda_client = None


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def lambda_handler(event, context):
    endpoint = event.get("endpoint")
    connection_ids = event.get("connectionIds") or []
    payload = event.get("payload") or {}
    if not endpoint or not connection_ids:
        return {"sent": 0, "gone": 0}
    api = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
    data = json.dumps(payload).encode("utf-8")
    sent, gone = 0, []
    for cid in connection_ids:
        try:
            api.post_to_connection(ConnectionId=cid, Data=data)
            sent += 1
        except api.exceptions.GoneException:
            gone.append(cid)
        except ClientError:
            logger.exception("post_to_connection failed for %s", cid)
    if gone:
        _reap(gone)
    return {"sent": sent, "gone": len(gone)}


def _reap(gone):
    if not REAPER_FUNCTION:
        return
    try:
        _lambda().invoke(
            FunctionName=REAPER_FUNCTION, InvocationType="Event",
            Payload=json.dumps({"connectionIds": gone}))
    except Exception:
        logger.exception("reaper dispatch failed")
