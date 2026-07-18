"""
In-VPC Lambda: WebSocket $connect for Site Voice. Registers the live
connection in ws_connections, keyed by the authorizer-verified Cognito sub.
Non-provisioned callers are refused (403 → API Gateway rejects the connection).
"""
import logging

from db.connection import get_connection
from repositories import users, ws_connections

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    rc = event.get("requestContext", {}) or {}
    connection_id = rc.get("connectionId")
    sub = (rc.get("authorizer") or {}).get("sub")
    if not connection_id or not sub:
        return {"statusCode": 401}
    try:
        with get_connection() as conn:
            caller = users.get_user_by_sub(conn, sub)
            if caller is None or not caller["company_id"]:
                logger.warning("ws connect refused: sub %s not provisioned", sub)
                return {"statusCode": 403}
            ws_connections.upsert_connection(
                conn, connection_id, caller["id"], caller["company_id"])
        return {"statusCode": 200}
    except Exception:
        logger.exception("ws connect failed")
        return {"statusCode": 500}
