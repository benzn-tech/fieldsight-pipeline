"""In-VPC leaf: reads the device ledger and returns it as JSON.

This function makes NO outbound calls, and cannot: there is no NAT gateway, so
anything inside the VPC reaches Aurora and nothing else (BUG-36). The Notion
and Teams half lives in lambda_device_report, outside the VPC, which invokes
this one — the same split as AskAgentFunction -> RagSearchFunction.

Reads the whole table. Twenty rows; there is nothing to paginate.
"""

import logging
import os

import psycopg

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Ordering is part of the contract: never-seen devices are the alert that
# matters most, so nulls sort first and the report cannot bury them.
_QUERY = """
select d.asset_tag,
       d.device_uuid,
       d.uuid_trusted,
       d.app_version,
       d.last_seen_at,
       d.last_account_sub,
       s.name as actual_site,
       c.name as actual_company
from devices d
left join lateral (
    select r.site_id
    from recordings r
    where r.device_id = d.id and r.site_id is not null
    order by r.created_at desc
    limit 1
) last_rec on true
left join sites     s on s.id = last_rec.site_id
left join companies c on c.id = s.company_id
order by d.last_seen_at asc nulls first, d.asset_tag asc
"""


def _connect():
    return psycopg.connect(
        host=os.environ["PGHOST"],
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        connect_timeout=10,
    )


def lambda_handler(event, context):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_QUERY)
            rows = cur.fetchall()

    devices = [
        {
            "asset_tag": r[0],
            "device_uuid": r[1],
            "uuid_trusted": r[2],
            "app_version": r[3],
            "last_seen_at": r[4].isoformat() if r[4] else None,
            "last_account_sub": r[5],
            "actual_site": r[6],
            "actual_company": r[7],
        }
        for r in rows
    ]
    logger.info("device ledger returning %d rows", len(devices))
    return {"devices": devices}
