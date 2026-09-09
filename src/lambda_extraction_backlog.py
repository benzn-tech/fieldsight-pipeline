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

#: Marker `lambda_extract_session` writes when it deliberately passes over a
#: session -- no usable speaker turns, so there was nothing to summarise.
#:
#: Kept in step with `lambda_extract_session.SKIP_MARKER_SUFFIX` by a test, not
#: by an import: this function is non-VPC and dependency-free on purpose.
#: Deliberately NOT ".json" -- item-writer is wired to `extractions/` with that
#: suffix and would be invoked on every marker.
SKIP_MARKER_SUFFIX = ".skipped"

#: Group requests are named `extraction_requests/group-<groupId>.json` and
#: carry {groupId, mergedKey, members[]} with no top-level userFolder, so the
#: solo parse raised KeyError and filed every meeting as a fault in the
#: enqueuer. Meetings were therefore never checked at all -- and unlike a solo
#: session a meeting cannot be re-driven: extract_group returns None on an LLM
#: failure rather than raising, and the claim has already set merged_at.
GROUP_REQUEST_MARKER = "group-"


def marker_for(extraction_key):
    """The skip marker beside a given extraction. One rule, so a solo session
    and a merged meeting cannot end up with different conventions."""
    stem = extraction_key[:-len(".json")] if extraction_key.endswith(".json") else extraction_key
    return stem + SKIP_MARKER_SUFFIX


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

    # One listing, two questions: what was published, and when each skip
    # marker was written. The marker's age is load-bearing (see below), so
    # unlike `done` it cannot be a bare set of keys.
    done, marker_at = set(), {}
    for obj in _objects(client, "extractions/"):
        key = obj["Key"]
        if key.endswith(SKIP_MARKER_SUFFIX):
            marker_at[key] = obj["LastModified"]
        else:
            done.add(key)
    tx_by_day = {}
    recent, everything, skipped = [], [], 0

    # Read every request BEFORE judging any of them: a solo session cannot be
    # judged without knowing whether it belongs to a meeting that was merged,
    # and the group request that says so sorts after it.
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

        # Counting rather than crashing is the point of this whole branch, so
        # the shape checks belong beside the parse and not after it. A probe
        # that dies on one bad object publishes no metric at all, which under
        # `TreatMissingData: breaching` fires the alarm AND stops reporting the
        # real backlog until somebody deletes the object by hand.
        if not isinstance(req, dict):
            skipped += 1
            logger.warning("backlog: unreadable request %s", obj["Key"])
            continue

        is_group = (obj["Key"].startswith(REQUEST_PREFIX + GROUP_REQUEST_MARKER)
                    or "members" in req or "groupId" in req)
        if is_group:
            members = req.get("members")
            members = [m for m in members if isinstance(m, dict)] if isinstance(members, list) else []
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

    def transcripts_for(folder, date, base):
        """When each of this session's transcripts landed. Empty means VAD
        found no speech and there was nothing to summarise -- 42 of 53
        unfulfilled requests on prod are this, and counting them would bury the
        eleven that matter."""
        day = (folder, date)
        if day not in tx_by_day:
            tx_by_day[day] = [(o["Key"], o["LastModified"]) for o in
                              _objects(client, f"transcripts/{folder}/{date}/")]
        sid = base[3:] if base.startswith("sid") else base
        return [when for k, when in tx_by_day[day] if sid in k]

    def settled(extraction, landed):
        """Published, or deliberately passed over since the last transcript.

        The marker's timestamp is a PROXY for which transcripts the pass saw,
        and it is not exact under concurrency: a chunk landing between a silent
        pass's listing and its put leaves a marker newer than a transcript it
        never read. Sequential arrivals are safe -- gather_session_segments
        re-reads the whole session, so once any chunk holds speech no later
        pass can reach the no-turns branch -- but a reconnect burst is not
        sequential, and it takes an outage in the same moment to hide anything.
        Recorded rather than closed; the exact fix is for the marker to carry
        the keys it saw and for this to compare sets.
        """
        if extraction in done:
            return True
        marked = marker_at.get(marker_for(extraction))
        return marked is not None and marked >= max(landed)

    # Meetings first, so their members can be recognised below.
    # extract_group merges only the first GROUP_MAX_MEMBERS devices and names
    # the rest in the artifact's omittedMembers. This map covers ALL of them,
    # so a fifth device whose own extraction was also lost is silenced here.
    # Reading omittedMembers is not an option: this role has no GetObject on
    # `extractions/` at all, only ListBucket. Recorded as a known edge.
    covered = {}
    for obj, req in groups:
        merged = req["mergedKey"]
        for m in req["members"]:
            covered[(m.get("userFolder"), m.get("date"), m.get("sessionBase"))] = merged
        landed = []
        for m in req["members"]:
            landed += transcripts_for(m.get("userFolder"), m.get("date"),
                                      m.get("sessionBase") or "")
        if not landed or settled(merged, landed):
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

        # The words are in the meeting's record, filed under the group's key.
        # This was the second false-positive class in the 2026-09-09 alarm: the
        # session it was red for was a member of a group that had merged.
        merged = covered.get((folder, date, base))
        if merged and merged in done:
            continue

        landed = transcripts_for(folder, date, base)
        if not landed:
            continue

        # A transcript FILE is not evidence anybody spoke -- nine of the ten
        # sessions reported on prod held only "[background noise]" or the
        # device saying "Recording started". extract_session already decides
        # this and records the decision; reading it is the only version that
        # cannot drift from what actually runs.
        if settled(f"extractions/{folder}/{date}/{base}.json", landed):
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
