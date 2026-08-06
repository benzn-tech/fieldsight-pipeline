"""Device backlog vitals, and the channel that will one day carry a thaw.

Rides the same discipline as `device_heartbeat`: nothing here may raise. Failing to record
how far behind a device is must never fail the request that carried the report.

Phase 1 answers with an empty `thaw` list unconditionally. The register exists, the endpoint
records vitals, and no device is ever told to try again — so this ships without changing a
single device's behaviour, while the ledger starts carrying real backlog numbers.

The number that matters is `backlog_oldest_age_s`, not a count. A count is diluted by every
new recording and jumps around; an age rises monotonically, is comparable across devices,
and answers "is this device healthy" on its own.
"""

import logging
import os

logger = logging.getLogger()

SERVER_BUILD = os.environ.get("SERVER_BUILD", "")

_VITALS = """
update devices set
  backlog_oldest_age_s = %s,
  backlog_pending      = %s,
  backlog_reported_at  = now()
where id = %s
"""


def _int_or_none(value):
    """A value we cannot read is absent, not zero. Zero is a claim; absence is honest."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record(conn, device_id, vitals):
    """Record what the device said. Returns the response body. Never raises."""
    body = {"serverBuild": SERVER_BUILD or None, "thaw": []}

    if not device_id:
        # Headers did not resolve to a known device. It still gets a valid answer: a
        # device whose account or tag is wrong is the one most worth hearing from, and
        # refusing it would discard exactly the report that matters.
        return body

    try:
        vitals = vitals or {}
        with conn.cursor() as cur:
            cur.execute(_VITALS, (
                _int_or_none(vitals.get("oldestPendingAgeS")),
                _int_or_none(vitals.get("pending")),
                device_id,
            ))
        conn.commit()
    except Exception:
        logger.exception("device status vitals not recorded for %s", device_id)

    return body
