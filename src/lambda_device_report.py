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

import datetime as dt
import json
import logging
import os
from zoneinfo import ZoneInfo

import boto3

# Bare module names: the Lambda zip is flat, so `from src.x import y` works in
# tests and fails at runtime. Imported as modules, not functions, because the
# tests monkeypatch them as attributes of this module.
import device_alerts
import device_notify
import notion_client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
LEDGER_FUNCTION = os.environ.get("LEDGER_FUNCTION", "")
NOTION_DATA_SOURCE = os.environ.get("NOTION_DATA_SOURCE", "")
DATABASE_URL = os.environ.get("DEVICE_LEDGER_URL", "")
TEAMS_WEBHOOK = os.environ.get("TEAMS_WEBHOOK", "")
EMAIL_TO = [a.strip() for a in os.environ.get("DEVICE_ALERT_EMAILS", "").split(",") if a.strip()]
SES_SENDER = os.environ.get("EMAIL_SENDER", "")
QUIET_WORKING_DAYS = int(os.environ.get("QUIET_ALERT_WORKING_DAYS", "7"))
GRACE_DAYS = int(os.environ.get("DEVICE_GRACE_DAYS", "3"))

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

    rows = notion_client.list_rows(NOTION_TOKEN, NOTION_DATA_SOURCE)
    today = dt.datetime.now(ZoneInfo("Pacific/Auckland")).date()
    results = device_alerts.derive(devices, rows, today, QUIET_WORKING_DAYS, GRACE_DAYS)

    # Per-row, on purpose: one malformed row must not leave the other nineteen
    # stale. A stale table is indistinguishable from a correct one at a glance,
    # which is the failure this whole design exists to avoid.
    failed = 0
    for r in results:
        try:
            notion_client.update_row(NOTION_TOKEN, r["page_id"], r["updates"])
        except Exception:
            failed += 1
            logger.exception("notion update failed for %s", r["device"])

    device_notify.push(
        device_notify.format_message(results, DATABASE_URL),
        teams_webhook=TEAMS_WEBHOOK,
        email_to=EMAIL_TO,
        ses_sender=SES_SENDER,
    )
    return {"status": "ok", "devices": len(results), "failed": failed}
