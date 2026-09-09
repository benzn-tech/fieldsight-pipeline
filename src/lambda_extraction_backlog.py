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
# Which stack published this. Without it both stacks write the SAME series and
# the alarm cannot tell them apart -- test's backlog would page prod, and worse,
# if the PROD probe died while a test publisher lived, `TreatMissingData:
# breaching` would never trip. That is precisely the "the checker stopped"
# scenario this function exists to detect, defeated by a missing dimension.
METRIC_STAGE = os.environ.get("BACKLOG_METRIC_STAGE", "").strip()

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


# Words a transcript can contain while nobody actually said anything. Non-speech
# markers come from the recogniser; the device announcements come from the
# device itself and are already stripped one layer down by
# lambda_extract_session's DEVICE_ANNOUNCEMENT_PATTERNS -- so a session that
# holds only these produced no extraction *correctly*, and reporting it as lost
# work is a false alarm.
_NON_SPEECH = ("[background noise]", "[music]", "[silence]", "[inaudible]",
               "recording started", "recording stopped")
# Kept for reference, not used as the test. A length floor was the obvious
# mechanism and it does not work: one of the nine false cases was "Recording
# started." three times over -- 55 characters of the device talking to itself,
# which clears any floor worth setting. What separates them is not how much text
# there is but whether any of it is SPEECH, so the markers are removed first and
# the question is asked of what remains.
_MIN_SPEECH_CHARS = 40


def _has_real_speech(client, key):
    """Did a person actually say something in this transcript?

    Fails OPEN: any error reading or parsing the object counts as speech, so a
    transient S3 problem produces a false alarm rather than silently hiding a
    genuinely lost session. The expensive mistake here is under-reporting.
    """
    try:
        raw = client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
        doc = json.loads(raw)
    except Exception:                                  # noqa: BLE001
        logger.warning("backlog: could not read transcript %s -- counting it "
                       "as speech", key)
        return True
    try:
        text = (doc.get("results", {}).get("transcripts") or [{}])[0].get(
            "transcript", "")
    except Exception:                                  # noqa: BLE001
        return True
    stripped = (text or "").strip()
    low = stripped.lower()
    if not low:
        return False

    # Remove every non-speech marker FIRST, then ask whether anything is left.
    # Length alone is not enough: one of the ten real cases was "Recording
    # started." three times over, which is 55 characters of the device talking
    # to itself and would clear any sensible length floor.
    residue = low
    for marker in _NON_SPEECH:
        residue = residue.replace(marker, " ")
    residue = residue.strip(" .,-")
    if not residue:
        return False

    # Something survived. Short-but-real is still real: a worker saying "the
    # slab cracked" is four words, and guessing that brevity means worthless is
    # how the one true loss in ten would be discarded.
    return True


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
        mine = [k for k in tx_by_day[day] if sid in k]
        if not mine:
            continue

        # A TRANSCRIPT FILE IS NOT EVIDENCE THAT ANYBODY SPOKE. The test above
        # asks whether an object exists; this asks the question that was meant.
        #
        # Measured 2026-09-09 on the ten sessions this alarm was reporting:
        # NINE of them had transcripts holding "[background noise]", "" or
        # nothing but the device saying "Recording started." Exactly one had a
        # real conversation -- two speakers, "they don't meet the requirements".
        # An alarm that is ninety percent noise gets ignored inside a week, and
        # this codebase has already learned that once: counting the 42 silent
        # requests alongside the 11 real ones would have parked the number at 42
        # forever.
        #
        # Cost is bounded: only sessions that already failed both cheaper tests
        # reach here, which on prod is single digits per run.
        if not any(_has_real_speech(client, k) for k in mine):
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
    datum = {"MetricName": "ExtractionBacklog",
             "Value": len(recent), "Unit": "Count"}
    if METRIC_STAGE:
        datum["Dimensions"] = [{"Name": "Stage", "Value": METRIC_STAGE}]
    boto3.client("cloudwatch").put_metric_data(
        Namespace=METRIC_NAMESPACE, MetricData=[datum],
    )
    logger.info("backlog: recent=%d total=%d window_days=%d grace_min=%d",
                len(recent), len(everything), WINDOW_DAYS, GRACE_MINUTES)
    return {"recent": len(recent), "total": len(everything),
            "skipped": skipped, "items": recent}
