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

#: Multi-device meetings are enqueued as `extraction_requests/group-<id>.json`
#: carrying {groupId, mergedKey, members[]} and NO top-level userFolder. The
#: solo parse raised KeyError on them, so every meeting since the feature
#: shipped was filed as a fault in the enqueuer and never checked.
#:
#: That matters more than it would for a solo session, because a meeting is
#: never re-driven: extract_group returns None on an LLM failure rather than
#: raising -- deliberately, the members' own reports have already gone out --
#: and the finalize claim has already set merged_at. A lost merge is permanent
#: and this probe is the only thing that would ever notice it.
GROUP_REQUEST_MARKER = "group-"


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

    # Read every request BEFORE judging any of them. A solo session cannot be
    # judged without knowing whether it belongs to a meeting that merged, and
    # `group-` sorts after the hex-named solo requests.
    solo, groups = [], []
    for obj in _objects(client, REQUEST_PREFIX):
        if obj["LastModified"] > cutoff_new:
            continue                                   # still in flight
        try:
            body = client.get_object(Bucket=S3_BUCKET, Key=obj["Key"])["Body"].read()
            req = json.loads(body)
        except Exception:                              # noqa: BLE001
            skipped += 1
            logger.warning("backlog: unreadable request %s", obj["Key"])
            continue

        # Shape checks sit beside the parse, not after it: a probe that dies on
        # one bad object publishes no metric, which under TreatMissingData
        # breaching fires the alarm AND stops reporting the real backlog until
        # somebody deletes the object by hand.
        if not isinstance(req, dict):
            skipped += 1
            logger.warning("backlog: unreadable request %s", obj["Key"])
            continue

        is_group = (obj["Key"].startswith(REQUEST_PREFIX + GROUP_REQUEST_MARKER)
                    or "members" in req or "groupId" in req)
        if is_group:
            members = req.get("members")
            members = ([m for m in members if isinstance(m, dict)]
                       if isinstance(members, list) else [])
            if not (req.get("groupId") and req.get("mergedKey") and members):
                skipped += 1
                logger.warning("backlog: group request %s is missing "
                               "groupId/mergedKey/members", obj["Key"])
                continue
            groups.append((obj, dict(req, members=members)))
        elif not (req.get("userFolder") and req.get("date") and req.get("sessionBase")):
            skipped += 1
            logger.warning("backlog: unreadable request %s", obj["Key"])
        else:
            solo.append((obj, req))

    def somebody_spoke(folder, date, base):
        """Both discriminators, for one session.

        No transcripts at all means VAD found no speech. Transcripts that hold
        only markers and device announcements mean the recogniser found none --
        a transcript FILE is not evidence that anybody spoke.
        """
        if not (folder and date and base):
            return False
        day = (folder, date)
        if day not in tx_by_day:
            tx_by_day[day] = _keys(client, f"transcripts/{folder}/{date}/")
        sid = base[3:] if base.startswith("sid") else base
        mine = [k for k in tx_by_day[day] if sid in k]
        return bool(mine) and any(_has_real_speech(client, k) for k in mine)

    # Meetings first, so their members can be recognised below.
    covered = {}
    for obj, req in groups:
        merged = req["mergedKey"]
        # extract_group merges only the first GROUP_MAX_MEMBERS devices and
        # names the rest in the artifact's omittedMembers, which this role
        # cannot read -- it has ListBucket on `extractions/` and no GetObject.
        # So a fifth device whose own extraction was also lost is silenced here.
        # Recorded as a known edge rather than guessed at.
        for m in req["members"]:
            covered[(m.get("userFolder"), m.get("date"), m.get("sessionBase"))] = merged
        if merged in done:
            continue
        if not any(somebody_spoke(m.get("userFolder"), m.get("date"), m.get("sessionBase"))
                   for m in req["members"]):
            continue
        lead = req["members"][0]
        item = {"folder": lead.get("userFolder"), "date": lead.get("date"),
                "session": f"grp{req['groupId']}",
                "requested_at": obj["LastModified"].isoformat()}
        everything.append(item)
        if obj["LastModified"] >= cutoff_old:
            recent.append(item)

    for obj, req in solo:
        folder, date, base = req["userFolder"], req["date"], req["sessionBase"]

        if f"extractions/{folder}/{date}/{base}.json" in done:
            continue

        # The words are in the meeting's record, under the group's key. This was
        # the second false-positive class in the 2026-09-09 alarm: the session it
        # was red for was a member of a group that had already merged.
        merged = covered.get((folder, date, base))
        if merged and merged in done:
            continue

        if not somebody_spoke(folder, date, base):
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
