"""In-VPC Lambda: WebSocket $disconnect for Site Voice. Removes the connection
row. Best-effort — a missing row (already reaped) is fine; never fail."""
import logging

from db.connection import get_connection
from repositories import ws_connections

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    connection_id = (event.get("requestContext", {}) or {}).get("connectionId")
    if not connection_id:
        return {"statusCode": 400}
    try:
        with get_connection() as conn:
            ws_connections.delete_connection(conn, connection_id)
    except Exception:
        logger.exception("ws disconnect failed")
    return {"statusCode": 200}
