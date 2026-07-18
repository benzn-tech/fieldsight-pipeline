"""
In-VPC Lambda: delete stale ws_connections rows (replaces DynamoDB TTL).

Two modes:
  * targeted — {"connectionIds": [...]}: rows for connections a fanout POST
    hit with GoneException.
  * sweep    — {"sweep": true}: scheduled belt-and-braces — drop connections
    older than WS_STALE_HOURS (a dead conn that never fired $disconnect) and
    prune voice_messages older than VOICE_RETENTION_DAYS (S3 lifecycle parity).
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from db.connection import get_connection
from repositories import voice_messages, ws_connections

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STALE_HOURS = int(os.environ.get("WS_STALE_HOURS", "24"))
RETENTION_DAYS = int(os.environ.get("VOICE_RETENTION_DAYS", "30"))


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        if event.get("sweep"):
            swept = ws_connections.delete_stale(
                conn, now - timedelta(hours=STALE_HOURS))
            pruned = voice_messages.prune_older_than(
                conn, now - timedelta(days=RETENTION_DAYS))
            return {"swept_connections": swept, "pruned_messages": pruned}
        ids = event.get("connectionIds") or []
        deleted = ws_connections.delete_connections(conn, ids)
        return {"deleted": deleted}
