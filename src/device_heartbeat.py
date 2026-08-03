"""Device ledger heartbeat.

Rides the org-api request path: any request carrying device headers refreshes
that device's row. The upsert is conditional, so an ordinary repeat heartbeat
modifies no row at all — no WAL, no bloat, just one statement on a connection
that is already open. An account switch or a version change always writes
through the throttle, because an account switch is the central event of a
hand-over and must be recorded the moment it happens.

Nothing here may raise. Failing to record telemetry must never fail a user's
API request.
"""

import logging
import uuid as _uuid

logger = logging.getLogger()

_UPSERT = """
insert into devices (id, asset_tag, device_uuid, app_version, last_account_sub, last_seen_at)
values (%s, %s, %s, %s, %s, now())
on conflict (asset_tag) do update set
  last_seen_at     = now(),
  device_uuid      = coalesce(excluded.device_uuid, devices.device_uuid),
  app_version      = coalesce(excluded.app_version, devices.app_version),
  last_account_sub = coalesce(excluded.last_account_sub, devices.last_account_sub)
where devices.last_seen_at is null
   or devices.last_seen_at < now() - interval '1 hour'
   or devices.last_account_sub is distinct from excluded.last_account_sub
   or devices.app_version is distinct from excluded.app_version
"""

_COLLIDING_UUIDS = """
select device_uuid
from devices
where device_uuid = %s
group by device_uuid
having count(distinct asset_tag) > 1
"""

_DISTRUST = "update devices set uuid_trusted = false where device_uuid = %s"


def _get(headers, name):
    if not headers:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    value = lowered.get(name.lower())
    if value is None:
        return None
    return str(value).strip() or None


def parse_headers(headers):
    """Extract device identity from request headers, or None if absent.

    A device that reports a uuid but no tag becomes an `unclaimed:` row, which
    is how it surfaces for someone to claim rather than vanishing.
    """
    tag = _get(headers, "X-Device-Tag")
    device_uuid = _get(headers, "X-Device-Id")
    version = _get(headers, "X-App-Version")

    if not tag and not device_uuid:
        return None

    asset_tag = tag.upper() if tag else "unclaimed:" + device_uuid[:8]
    return {"asset_tag": asset_tag, "device_uuid": device_uuid, "app_version": version}


def record(conn, ident, account_sub):
    """Refresh a device's row. Never raises."""
    if not ident:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                _UPSERT,
                (
                    str(_uuid.uuid4()),
                    ident["asset_tag"],
                    ident.get("device_uuid"),
                    ident.get("app_version"),
                    account_sub,
                ),
            )
            if ident.get("device_uuid"):
                cur.execute(_COLLIDING_UUIDS, (ident["device_uuid"],))
                if cur.fetchall():
                    # One uuid under two tags: the ROM is handing out a shared
                    # identifier. The tag stays authoritative and the uuid is
                    # marked untrustworthy — trust never auto-recovers.
                    cur.execute(_DISTRUST, (ident["device_uuid"],))
        conn.commit()
    except Exception:
        logger.exception("device heartbeat failed for %s", ident.get("asset_tag"))
