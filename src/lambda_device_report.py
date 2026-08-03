"""Non-VPC scheduler for the device ledger.

Lives outside the VPC because it must reach Notion and Teams; it therefore
cannot reach Aurora, and invokes the in-VPC lambda_device_ledger for data.
Same split as AskAgentFunction -> RagSearchFunction (BUG-36).

Phase 1 ships this INERT: with NOTION_TOKEN unset it returns immediately and
never even calls the ledger. Alert derivation and the Notion write arrive in
Phase 3, once a Notion database and an integration token exist and there is a
month of real last_seen_at data to set the quiet threshold from.

A ledger failure is deliberately allowed to raise. Swallowing it would leave a
Notion table that merely looks unchanged — the silent-staleness failure this
whole design is built to avoid.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
LEDGER_FUNCTION = os.environ.get("LEDGER_FUNCTION", "")

_client = None


def _lambda():
    global _client
    if _client is None:
        _client = boto3.client("lambda")
    return _client


def lambda_handler(event, context):
    if not NOTION_TOKEN:
        logger.info("device report disabled: NOTION_TOKEN unset")
        return {"status": "disabled", "devices": 0}

    resp = _lambda().invoke(
        FunctionName=LEDGER_FUNCTION,
        InvocationType="RequestResponse",
        Payload=b"{}",
    )
    payload = json.loads(resp["Payload"].read() or b"{}")
    devices = payload.get("devices") or []
    logger.info("device report received %d devices", len(devices))
    return {"status": "ok", "devices": len(devices)}
