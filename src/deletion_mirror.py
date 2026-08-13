"""The tombstone set, mirrored to S3 for the lambdas that have no database.

Spec:  docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md
Plan:  docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md (phase 6c)

`lambda_report_generator` and `lambda_ask_agent`'s day-scoped path are **not in the VPC**
and hold no database connection. Every SQL-level protection this feature has built — the
read predicates, the renderer's drop, the write-side guard — is invisible to them: they
read transcripts and reports straight off S3 and answer from what they find.

So they get a copy of the answer instead:

    redactions/{folder}/{date}/deleted_sessions.json   ->  {"sessions": ["sid…", …]}

**Aurora stays the authority.** This is a cache with one job, and it is written by the
endpoint inside the same request that writes the tombstones, so it cannot drift silently
without the delete itself failing.

Pure and import-clean by the same rule as `batch_stitch`: the S3 client is injected, so
this module can be tested without AWS and imported by a non-VPC function without dragging
a database driver in behind it.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger()

PREFIX = "redactions/"


def mirror_key(folder: str, date: str) -> str:
    """Where the mirror for one (folder, date) lives.

    Derived in one place and never spelled twice. A writer and a reader that disagree
    about a key is how a whole feature silently does nothing while its tests pass — this
    repo shipped exactly that when the batch map was written under `audio_segments/` and
    read under `transcripts/`, and every batched session fell back to filename arithmetic
    with the suite green.
    """
    return f"{PREFIX}{folder}/{date}/deleted_sessions.json"


def write_mirror(s3, bucket: str, folder: str, date: str, session_ids) -> str:
    """Publish the deleted-session set for one day. Returns the key written."""
    key = mirror_key(folder, date)
    s3.put_object(Bucket=bucket, Key=key,
                  Body=json.dumps({"sessions": sorted(set(session_ids))}),
                  ContentType="application/json")
    logger.info("deletion mirror: wrote %s with %d session(s)", key, len(set(session_ids)))
    return key


def deleted_sessions(s3, bucket: str, folder: str, date: str) -> set:
    """The deleted session ids for one day, or an empty set.

    A missing object is the common case by far — every day without a deletion has no
    mirror — so absence is not an error and must not break the nightly report for
    everyone.

    An unreadable object is different and is LOGGED. A permissions fault here looks exactly
    like "nothing was deleted", and that indistinguishability is what has cost this project
    three separate silent breakages. It still returns empty rather than raising: taking the
    report generator down would be a worse failure than a delay in hiding one day.
    """
    key = mirror_key(folder, date)
    try:
        doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except KeyError:
        return set()
    except Exception as e:
        if type(e).__name__ in ("NoSuchKey", "404"):
            return set()
        logger.warning("deletion mirror %s unreadable (%s) — proceeding as if nothing was "
                       "deleted, which may show content a customer removed",
                       key, type(e).__name__)
        return set()
    return set(doc.get("sessions") or [])


def drop_deleted(keys, session_ids) -> list:
    """The subset of `keys` that does not belong to a deleted session.

    Substring match on the session id, because the id appears inside the filename
    (`…_sid{32hex}_c0001.json`) rather than as a path component.
    """
    if not session_ids:
        return list(keys)
    return [k for k in keys if not any(sid in k for sid in session_ids)]
