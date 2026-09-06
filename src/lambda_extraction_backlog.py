"""Words that were transcribed and never summarised.

WHY THIS EXISTS, and why it is not another error alarm. On 2026-09-02 the LLM
provider's account went into arrears and every extraction failed. The
`extract-session` error alarm added the same week cannot see the tail of that:
once the retries stopped there were no invocations at all, so the alarm read
`OK` while extraction was completely broken. An alarm on failures is blind to
absence.

Measured over the whole prod bucket while writing this, which is also what
fixed the rule:

    122 extraction requests
     69 fulfilled
     42 with no transcripts at all   <- VAD found no speech. NOT a backlog.
     11 with transcripts and no extraction   <- the words exist, the summary
                                                never happened

Only two of those eleven were the outage. The other nine go back to 2026-08-06
and nobody knew. A metric that counted all 53 unfulfilled requests would sit at
42 forever and be ignored inside a week, which is how a channel stops being
read -- so the transcripts check is not a refinement, it is the difference
between a signal and noise.

Non-VPC on purpose: there is no NAT and no CloudWatch or SNS endpoint inside
the VPC (only S3, DynamoDB and cognito-idp), so nothing in there can publish a
metric. This function needs S3 and CloudWatch and no database at all, which is
what makes it a plain scheduled worker rather than the in-VPC/out-of-VPC pair
the rest of the pipeline needs.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
METRIC_NAMESPACE = os.environ.get("BACKLOG_METRIC_NAMESPACE", "FieldSight/Pipeline")

# A request younger than this is in flight, not late. Extraction of a long
# session takes minutes, and finalize re-drives, so anything tighter would
# alarm on the normal path.
GRACE_MINUTES = int(os.environ.get("BACKLOG_GRACE_MINUTES", "45"))

# The alarm is for NEW loss. Historical debt -- the nine sessions from August
# found when this was written -- must not hold the metric above zero forever,
# because an alarm that is always red is an alarm nobody reads. Those are
# reported in the logs and in the return value, never in the metric.
WINDOW_DAYS = int(os.environ.get("BACKLOG_WINDOW_DAYS", "3"))

REQUEST_PREFIX = "extraction_requests/"


def _s3():
    return boto3.client("s3")


def _keys(client, prefix):
    """Every key under a prefix. Paginated: list_objects_v2 truncates at 1000
    and a silent truncation here would UNDER-report the backlog, i.e. fail in
    the direction that looks healthy."""
    out = []
    for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=S3_BUCKET, Prefix=prefix):
        out.extend(o["Key"] for o in page.get("Contents", []))
    return out


def _objects(client, prefix):
    for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=S3_BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            yield o


def scan(client, now=None):
    """(recent_lost, all_lost, skipped) for the current bucket state.

    `skipped` is counted and logged rather than dropped: a request whose JSON
    will not parse, or which names no folder, is a fault in the enqueuer and
    would otherwise be indistinguishable from a healthy fulfilled request.
    """
    now = now or datetime.now(timezone.utc)
    cutoff_new = now - timedelta(minutes=GRACE_MINUTES)
    cutoff_old = now - timedelta(days=WINDOW_DAYS)

    done = set(_keys(client, "extractions/"))
    tx_by_day = {}
    recent, everything, skipped = [], [], 0

    for obj in _objects(client, REQUEST_PREFIX):
        if obj["LastModified"] > cutoff_new:
            continue                                   # still in flight
        try:
            body = client.get_object(Bucket=S3_BUCKET, Key=obj["Key"])["Body"].read()
            req = json.loads(body)
            folder, date = req["userFolder"], req["date"]
            base = req["sessionBase"]
        except Exception:                              # noqa: BLE001
            skipped += 1
            logger.warning("backlog: unreadable request %s", obj["Key"])
            continue

        if f"extractions/{folder}/{date}/{base}.json" in done:
            continue

        # THE DISCRIMINATOR. No transcripts means VAD found no speech and there
        # was nothing to summarise -- 42 of 53 unfulfilled requests on prod are
        # this, and counting them would bury the eleven that matter.
        day = (folder, date)
        if day not in tx_by_day:
            tx_by_day[day] = _keys(client, f"transcripts/{folder}/{date}/")
        sid = base[3:] if base.startswith("sid") else base
        if not any(sid in k for k in tx_by_day[day]):
            continue

        item = {"folder": folder, "date": date, "session": base,
                "requested_at": obj["LastModified"].isoformat()}
        everything.append(item)
        if obj["LastModified"] >= cutoff_old:
            recent.append(item)

    return recent, everything, skipped


def lambda_handler(event, context):
    client = _s3()
    recent, everything, skipped = scan(client)

    for item in everything:
        logger.info("backlog: transcribed but never extracted -- %s", json.dumps(item))
    if skipped:
        logger.warning("backlog: %d request(s) could not be read", skipped)

    # ALWAYS PUBLISHED, including the zero. An alarm on a metric that only
    # appears when something is wrong cannot tell "healthy" from "the checker
    # stopped running", and this whole function exists because absence was
    # invisible.
    boto3.client("cloudwatch").put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[{"MetricName": "ExtractionBacklog",
                     "Value": len(recent), "Unit": "Count"}],
    )
    logger.info("backlog: recent=%d total=%d window_days=%d grace_min=%d",
                len(recent), len(everything), WINDOW_DAYS, GRACE_MINUTES)
    return {"recent": len(recent), "total": len(everything),
            "skipped": skipped, "items": recent}
