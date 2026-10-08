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

import extraction_pending

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

#: The pipeline alert topic. Empty on a stack that does not create one (ShouldCreateAlerts
#: false): the expedite notice is then simply not sent.
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "").strip()

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


def _sns():
    return boto3.client("sns")


def notify_expedite(m):
    """Tell the alert topic that a customer asked to expedite. True when sent.

    The marker is the ledger: org-api stamps `expedite_requested_at` (and clears
    `expedite_notified_at`), this stamps `expedite_notified_at` after a successful publish.
    Keyed on those two rather than on the `expedite` flag, because the re-drive below
    clears that flag in the very run that notices it. A failed publish stamps nothing, so
    the next 5-minute run tries again."""
    if not m.get("expedite_requested_at") or m.get("expedite_notified_at"):
        return False
    if not ALERT_TOPIC_ARN:
        logger.info("backlog: expedite for %s but no alert topic is configured",
                    m.get("sessionBase"))
        return False
    when = " ".join(p for p in (m.get("date"), m.get("time_range")) if p)
    text = (f"Customer {m.get('expedite_by_name') or 'unknown'} "
            f"({m.get('expedite_by_company') or 'unknown company'}) asked to expedite "
            f"their recording {m.get('sessionBase')} ({when}). "
            f"Attempts so far: {int(m.get('attempts') or 0)}. "
            f"Last error: {m.get('last_error') or 'unknown'}.")
    try:
        _sns().publish(TopicArn=ALERT_TOPIC_ARN, Subject="FieldSight: customer asked to expedite",
                       Message=text)
    except Exception:
        logger.exception("backlog: could not publish the expedite notice for %s",
                         m.get("sessionBase"))
        return False
    m["expedite_notified_at"] = extraction_pending.iso(datetime.now(timezone.utc))
    return True


def _stamp(client, m):
    """Persist `expedite_notified_at` when the marker is not being rewritten by a re-drive."""
    stamp = m.get("expedite_notified_at")
    try:
        extraction_pending.update(client, S3_BUCKET, m["sessionBase"],
                                  lambda fresh: fresh.update(expedite_notified_at=stamp))
    except Exception:
        logger.exception("backlog: could not stamp expedite_notified_at on %s", m.get("sessionBase"))


#: Stop re-driving when the Lambda has less than this left: the marker writes and puts of
#: the next one would be cut off mid-way.
MIN_REMAINING_MS = 20_000


def _expire_promise(client, m, now):
    """A `promised_only` marker older than PROMISED_ONLY_TTL_HOURS: delete it, loudly.
    True when deleted. Counted in the metric by the caller until it is."""
    since = (extraction_pending.parse_iso(m.get("error_email_sent_at"))
             or extraction_pending.parse_iso(m.get("first_failed_at")))
    if not since or now - since < timedelta(hours=extraction_pending.PROMISED_ONLY_TTL_HOURS):
        return False
    try:
        extraction_pending.clear(client, S3_BUCKET, m["sessionBase"])
    except Exception:
        logger.exception("backlog: cannot delete the expired promised_only marker for %s",
                         m["sessionBase"])
        return False
    logger.error("EXTRACTION_PROMISE_EXPIRED %s: the recorder was told notes were on the "
                 "way %s, and no extraction or failure ever arrived; marker deleted",
                 m["sessionBase"], extraction_pending.iso(since))
    return True


def redrive(client, now=None, rng=None, time_left_ms=None):
    """Re-put the original request of every extraction_pending marker that is due.

    Re-putting the request object is the trigger: the extract-session notification is
    ObjectCreated:* on extraction_requests/, so a put fires the same final pass the
    first request did. The marker is rescheduled BEFORE the put, so a put that fails
    leaves a marker that is merely late, and a pass that fails again finds the next
    attempt already pushed out. An `expedite` marker is due at once.

    Backoff 5, 15, 30, 60 min, then hourly, then every 6 h once the marker has been
    failing for 24 h; every new `next_attempt_at` carries 0-120 s of jitter. A model-kind
    marker is never given up on (a recording the model cannot read today must still be
    read when the model comes back); a parse-kind marker is re-driven at most 6 times and
    then marked `gave_up_at` (still counted, still listed for the web, Expedite allows one
    more). One tick re-drives at most REDRIVE_PER_TICK markers, expedited first and then
    oldest first, and stops early when `time_left_ms()` falls under MIN_REMAINING_MS.

    Returns (redriven, stale) -- the session bases re-put, and the number of markers older
    than ALARM_AGE_MINUTES (the ExtractionPending metric)."""
    now = now or datetime.now(timezone.utc)
    redriven, stale = [], 0
    candidates = []
    alarm_age = timedelta(minutes=extraction_pending.ALARM_AGE_MINUTES)
    for m in extraction_pending.list_markers(client, S3_BUCKET):
        first = extraction_pending.parse_iso(m.get("first_failed_at"))
        is_stale = bool(first and now - first >= alarm_age)
        if m.get("promised_only"):
            # The recorder was promised notes and no extraction has failed or arrived:
            # nothing to re-drive. Counted once stale -- a promise that outlives
            # ALARM_AGE_MINUTES is what the alarm is for -- until the TTL deletes it.
            if is_stale and not _expire_promise(client, m, now):
                stale += 1
            continue
        if is_stale:
            stale += 1
        notified = notify_expedite(m)
        due = extraction_pending.parse_iso(m.get("next_attempt_at"))
        if not (m.get("expedite") or due is None or due <= now):
            if notified:
                _stamp(client, m)
            continue
        if not m.get("request_key"):
            logger.warning("backlog: marker for %s has no request_key -- cannot re-drive",
                           m["sessionBase"])
            if notified:
                _stamp(client, m)
            continue
        if (m.get("failure_kind") == extraction_pending.KIND_PARSE and not m.get("expedite")
                and int(m.get("attempts") or 0) >= extraction_pending.PARSE_REDRIVE_MAX):
            # A model that answers unusably six times in a row is not going to get better
            # on the seventh. Stop paying for it; leave the marker (alarm, banner).
            if not m.get("gave_up_at"):
                stamp_gave_up = extraction_pending.iso(now)
                logger.error("EXTRACTION_PARSE_GAVE_UP %s: %d re-drives all ended in an "
                             "unusable answer; no more automatic re-drives",
                             m["sessionBase"], int(m.get("attempts") or 0))
                try:
                    extraction_pending.update(
                        client, S3_BUCKET, m["sessionBase"],
                        lambda fresh: fresh.update(gave_up_at=stamp_gave_up))
                except Exception:
                    logger.exception("backlog: cannot record gave_up_at on %s", m["sessionBase"])
            elif notified:
                _stamp(client, m)
            continue
        candidates.append((m, notified))

    # Expedited first, then the oldest failure first.
    candidates.sort(key=lambda c: (not c[0].get("expedite"),
                                   c[0].get("first_failed_at") or ""))
    for n, (m, notified) in enumerate(candidates):
        base = m["sessionBase"]
        out_of_time = time_left_ms is not None and time_left_ms() < MIN_REMAINING_MS
        if n >= extraction_pending.REDRIVE_PER_TICK or out_of_time:
            # The rest wait for the next tick; their expedite notice is still recorded.
            for rest, rest_notified in candidates[n:]:
                if rest_notified:
                    _stamp(client, rest)
            logger.info("backlog: re-drive stopped after %d (%s); %d marker(s) wait",
                        len(redriven), "time" if out_of_time else "per-tick cap",
                        len(candidates) - n)
            break
        request_key = m["request_key"]
        try:
            body = client.get_object(Bucket=S3_BUCKET, Key=request_key)["Body"].read()
        except Exception:
            logger.exception("backlog: cannot read request %s for %s", request_key, base)
            if notified:
                _stamp(client, m)
            continue
        written = {}

        def reschedule(fresh, m=m, written=written):
            # The marker as it is NOW: a success that cleared it in the meantime is not
            # resurrected (update() returns None for a gone marker), and one that someone
            # else already re-drove is left alone.
            fdue = extraction_pending.parse_iso(fresh.get("next_attempt_at"))
            if not (fresh.get("expedite") or fdue is None or fdue <= now):
                return False
            attempts = int(fresh.get("attempts") or 0) + 1
            fresh["attempts"] = attempts
            fresh["expedite"] = False
            fresh["next_attempt_at"] = extraction_pending.iso(extraction_pending.jittered(
                now + timedelta(minutes=extraction_pending.next_delay_minutes(
                    fresh, attempts, now)), rng))
            if m.get("expedite_notified_at"):
                fresh["expedite_notified_at"] = m["expedite_notified_at"]
            written.update(fresh)
            return True

        try:
            if extraction_pending.update(client, S3_BUCKET, base, reschedule) is None:
                logger.info("backlog: %s was cleared or re-driven meanwhile -- not re-putting",
                            base)
                continue
            client.put_object(Bucket=S3_BUCKET, Key=request_key, Body=body,
                              ContentType="application/json")
        except Exception:
            logger.exception("backlog: re-drive of %s failed", base)
            continue
        logger.info("backlog: re-drove %s (attempt %d, next at %s)",
                    base, written["attempts"], written["next_attempt_at"])
        redriven.append(base)
    return redriven, stale


NOTICE_PREFIX = "notices/external_member/"
NOTICE_SENT_PREFIX = "notices/sent/external_member/"
#: A notice older than this is dropped (ERROR log): the SES sandbox rejects an
#: unverified recipient forever, and a stale "you were added" mail is worse than none.
NOTICE_EXPIRY = timedelta(hours=24)


def _notice_mail(n):
    site = n.get("site_name") or "a project"
    company = n.get("inviting_company") or "A company"
    role = n.get("role") or "member"
    name = (n.get("to_name") or "").strip()
    subject = f"You've been added to {site} on FieldSight"
    text = (f"Hi {name}," if name else "Hi,") + "\n\n" + (
        f"{company} has added you to the project \"{site}\" on FieldSight "
        f"as {role}.\n\n"
        "Sign in with the same FieldSight account you already use: the project "
        "now appears in your site list. Your own company and your other "
        "projects are unchanged.\n")
    return subject, text


def send_external_member_notices(client, now=None, sender=None):
    """Email each notice org-api left under notices/external_member/, exactly once.

    org-api is in the VPC with no route to SES, so it only writes the notice. Success
    moves it to notices/sent/ (copy, then delete: a crash between the two re-sends at
    worst once rather than losing it). Failure -- including an SES-sandbox rejection of
    an unverified recipient -- leaves it for the next tick; after NOTICE_EXPIRY it is
    deleted and logged at ERROR. A stub sender sends nothing, so it never marks a
    notice sent. Returns (sent, failed, expired) counts."""
    now = now or datetime.now(timezone.utc)
    sent_n = failed = expired = 0
    objs = list(_objects(client, NOTICE_PREFIX))
    if not objs:
        return 0, 0, 0
    if sender is None:
        import email_sender
        sender = email_sender.get_sender()
        if isinstance(sender, email_sender.StubEmailSender):
            logger.warning("backlog: %d external-member notice(s) waiting but "
                           "EMAIL_SENDER is not 'ses'; nothing sent", len(objs))
            sender = None
    for o in objs:
        key = o["Key"]
        try:
            modified = o.get("LastModified")
            if modified and now - modified >= NOTICE_EXPIRY:
                client.delete_object(Bucket=S3_BUCKET, Key=key)
                expired += 1
                logger.error("backlog: external-member notice %s expired unsent "
                             "after %s; deleted", key, NOTICE_EXPIRY)
                continue
            if sender is None:
                failed += 1
                continue
            n = json.loads(client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
            subject, text = _notice_mail(n)
            sender.send(n["to_email"], subject, text)
            dest = NOTICE_SENT_PREFIX + key[len(NOTICE_PREFIX):]
            client.copy_object(Bucket=S3_BUCKET, Key=dest,
                               CopySource={"Bucket": S3_BUCKET, "Key": key})
            client.delete_object(Bucket=S3_BUCKET, Key=key)
            sent_n += 1
            logger.info("backlog: sent external-member notice %s", key)
        except Exception:                              # noqa: BLE001
            failed += 1
            logger.exception("backlog: external-member notice %s not sent; "
                             "will retry", key)
    return sent_n, failed, expired


def emit_pending_metric(stale):
    """ExtractionPending as an EMF log line: CloudWatch extracts the metric from the
    function's own log output, which needs no PutMetricData grant and no log metric
    filter (the deploy role can create neither)."""
    print(json.dumps({
        "_aws": {"Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                 "CloudWatchMetrics": [{"Namespace": METRIC_NAMESPACE,
                                        "Dimensions": [["Stage"]],
                                        "Metrics": [{"Name": "ExtractionPending",
                                                     "Unit": "Count"}]}]},
        "Stage": METRIC_STAGE or "unknown",
        "ExtractionPending": stale}))


def lambda_handler(event, context):
    client = _s3()
    # Fast schedule: only the re-drive. The hourly schedule does it too, then the scan.
    try:
        left = getattr(context, "get_remaining_time_in_millis", None)
        redriven, stale = redrive(client, time_left_ms=left)
        emit_pending_metric(stale)
    except Exception:
        # Never let the re-drive take the backlog probe down with it; the metric is
        # simply not emitted (notBreaching), and the log says why.
        logger.exception("backlog: extraction_pending re-drive failed")
        redriven, stale = [], 0
    try:
        send_external_member_notices(client)
    except Exception:
        logger.exception("backlog: external-member notices failed")
    if (event or {}).get("task") == "redrive":
        return {"redriven": redriven, "pending_stale": stale}
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
            "skipped": skipped, "items": recent,
            "redriven": redriven, "pending_stale": stale}
