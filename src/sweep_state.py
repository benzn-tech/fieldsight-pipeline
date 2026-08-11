"""sweep_state.py — the "is there anything for the finalize sweep to do?" flag.

The finalize sweep runs every minute to hold the ≤2-minute confirmation-email
promise, but ~99% of its ticks find nothing and still open an Aurora connection.
Those connections are what stop Aurora Serverless v2 from ever auto-pausing (AWS
needs a continuous idle window of at least 300s; a connection every 60s never
lets the timer get there), so the cluster rents its 0.5-ACU floor 24/7.

This module holds one DynamoDB item answering "is any session in a state the
sweep could act on?" — `open`, `pending_close`, or `finalizing`. When the answer
is no, the sweep returns without touching Postgres and the cluster can sleep.

DynamoDB rather than anything else because the VPC already has a DynamoDB gateway
endpoint (vpce-01233d5b756ffefcb), so the in-VPC writers reach it with no NAT and
no new paid interface endpoint. It reuses the existing items table — the deploy
role has no `dynamodb:CreateTable` (verified with simulate-principal-policy), so
declaring a new table would CREATE_FAILED and roll the whole stack back.

FAIL-OPEN IS THE WHOLE CONTRACT. A flag that wrongly reads "nothing pending"
silently stops confirmation emails, which is the failure this system has been
bitten by repeatedly. So every error path here returns "pending" (do the work)
and every failure is logged, never swallowed. The cost of a false "pending" is
one wasted database connection; the cost of a false "idle" is a lost email.

`open` must set the flag too, not just `pending_close`: a device that dies
without sending /close is closed by the sweep's idle-close inference, and that
session would otherwise never be looked at again.
"""
import logging
import os

logger = logging.getLogger()

# One item, one row. The table is already per-stage (fieldsight-items vs
# fieldsight-test-items) but the stage stays in the key so a mis-pointed
# table is obvious when reading the row rather than silently shared.
_SK = "flag"


def _pk(stage: str) -> str:
    return f"SWEEP_STATE#{stage}"


def _table_name() -> str | None:
    return os.environ.get("SWEEP_STATE_TABLE") or None


def _client(client=None):
    if client is not None:
        return client
    import boto3  # local import: keep cold start off the module path when unused

    return boto3.client("dynamodb")


def mark_pending(stage: str, *, client=None, table: str | None = None) -> bool:
    """Record that there is now something for the sweep to do. Called wherever a
    session is opened (org-api `/open`, session-activity from the chunk stream).

    Best-effort by design: a failure here is recovered by the sweep's hourly
    unconditional pass, so it must never break the caller's real work (opening a
    session matters more than the flag). Returns True if the write landed.
    """
    name = table or _table_name()
    if not name:
        return False
    try:
        _client(client).put_item(
            TableName=name,
            Item={"PK": {"S": _pk(stage)}, "SK": {"S": _SK},
                  "pending": {"BOOL": True}},
        )
        return True
    except Exception:
        # Never silent (CLAUDE.md BUG-40): a swallowed except here is exactly how
        # the safety net would end up carrying traffic it was never meant to.
        logger.exception("sweep_state: could not set pending flag for stage %s", stage)
        return False


def clear_pending(stage: str, *, client=None, table: str | None = None) -> bool:
    """Record that the sweep has just looked and found no live sessions.

    Only ever called by the sweep, and only right after it has observed zero rows
    across all three live states — the component holding ground truth clears it,
    never an inference.
    """
    name = table or _table_name()
    if not name:
        return False
    try:
        _client(client).put_item(
            TableName=name,
            Item={"PK": {"S": _pk(stage)}, "SK": {"S": _SK},
                  "pending": {"BOOL": False}},
        )
        return True
    except Exception:
        logger.exception("sweep_state: could not clear pending flag for stage %s", stage)
        return False


def is_pending(stage: str, *, client=None, table: str | None = None) -> bool:
    """Is there work for the sweep? FAILS OPEN — see the module docstring.

    Returns True when the flag says pending, when no flag has ever been written,
    when the table is not configured, and on any error whatsoever. The only way
    to get False is an explicit, successfully-read `pending: false`.
    """
    name = table or _table_name()
    if not name:
        return True                      # not configured -> behave as before
    try:
        resp = _client(client).get_item(
            TableName=name,
            Key={"PK": {"S": _pk(stage)}, "SK": {"S": _SK}},
            ConsistentRead=True,         # a stale read here delays an email
        )
    except Exception:
        logger.exception("sweep_state: could not read pending flag for stage %s "
                         "— assuming pending", stage)
        return True
    item = resp.get("Item")
    if not item:
        return True                      # never written -> assume work exists
    return bool(item.get("pending", {}).get("BOOL", True))
