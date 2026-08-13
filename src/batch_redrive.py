"""Come back for the audio nothing else came back for.

Spec:  docs/superpowers/specs/2026-08-13-burst-arrival-defects.md
Plan:  docs/superpowers/plans/2026-08-13-burst-arrival-defects.md (phase 6)

Two failures reach this layer and nothing else catches either.

**A batch that was sealed and never transcribed.** 27 of these on one replay, all HTTP 429
from the provider's concurrency ceiling. Each was caught by a per-record `except`, recorded
as `status: error`, and returned 200 — so S3's async retry never fired, there is no DLQ, and
the ledger says `sealed` so no planner will look at it again. About 54 minutes of audio,
recovered only by copying each key onto itself by hand.

**A bucket that came due while nobody was listening.** Readiness is elapsed quiet, and the
sealer only runs when a chunk arrives. Live capture is fine — the next chunk re-checks — but
a device that uploads its backlog in one burst and then goes silent leaves every bucket open.
That is not hypothetical: it is what the acceptance replay did, 153 chunks and zero batches.

The re-drive is a copy of the object onto its own key, the same mechanism the singleton
bypass uses and the one that recovered those 27 by hand. `MetadataDirective=REPLACE` is
required — S3 refuses a copy onto the same key when nothing about it changes.

Bounded on purpose. A sweep that retries forever is a bill that grows forever, so the attempt
count lives on the seal record and exhaustion is an ERROR naming the batch.
"""
from __future__ import annotations

import logging

import batch_stitch

logger = logging.getLogger()

DEFAULT_REDRIVE_AFTER_SEC = 900
DEFAULT_MAX_ATTEMPTS = 3


def _transcript_key(batch_key: str):
    """Where the transcriber writes this batch's transcript.

    Derived, never hand-built: a reader and a writer that disagree about a key is precisely
    how every batched TEST session silently fell back to filename arithmetic while the whole
    suite stayed green.
    """
    if not batch_key.startswith("audio_segments/"):
        return None
    return "transcripts/" + batch_key[len("audio_segments/"):].rsplit(".", 1)[0] + ".json"


def _age_seconds(last_modified) -> float:
    from datetime import datetime, timezone

    if last_modified.tzinfo is None:
        last_modified = last_modified.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last_modified).total_seconds()


def _attempts(table, session_id, batch_key) -> int:
    from batch_ledger import _pk

    sk = f"REDRIVE#{batch_key.rsplit('/', 1)[-1]}"
    resp = table.query(KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                       ExpressionAttributeValues={":pk": _pk(session_id), ":sk": sk})
    items = resp.get("Items") or []
    return int(items[0].get("redrive_count") or 0) if items else 0


def _record_attempt(table, session_id, batch_key, n) -> None:
    from batch_ledger import _pk

    table.put_item(Item={"PK": _pk(session_id),
                         "SK": f"REDRIVE#{batch_key.rsplit('/', 1)[-1]}",
                         "redrive_count": n})


def redrive_untranscribed(s3, bucket, session_id, table, prefix=None,
                          redrive_after_sec: int = DEFAULT_REDRIVE_AFTER_SEC,
                          max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """Re-drive every batch WAV of this session that has no transcript. Never raises.

    `prefix` narrows the S3 listing; the caller knows the user/date folder and passing it
    keeps this from walking the whole lake.
    """
    candidates, redriven, exhausted = 0, 0, 0
    try:
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix or "audio_segments/")
        for page in pages:
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if not key.endswith(".wav") or not batch_stitch.is_batch_key(key):
                    continue
                if session_id not in key:
                    continue
                if _age_seconds(obj["LastModified"]) < redrive_after_sec:
                    continue
                tk = _transcript_key(key)
                if tk is None:
                    continue
                try:
                    s3.head_object(Bucket=bucket, Key=tk)
                    continue                      # it has a transcript; nothing to do
                except Exception:
                    pass
                candidates += 1
                n = _attempts(table, session_id, key)
                if n >= max_attempts:
                    exhausted += 1
                    logger.error("batch: giving up on %s after %s re-drives — it has no "
                                 "transcript and never will without help",
                                 key.rsplit("/", 1)[-1], n)
                    continue
                s3.copy_object(Bucket=bucket, Key=key,
                               CopySource={"Bucket": bucket, "Key": key},
                               MetadataDirective="REPLACE", ContentType="audio/wav")
                _record_attempt(table, session_id, key, n + 1)
                redriven += 1
    except Exception:
        logger.exception("batch: the re-drive sweep failed for %s", session_id)
    # Zero included, always. A guard that speaks only on failure cannot be told apart from
    # one that never ran, which is how three missing IAM grants each looked like nothing.
    logger.info("batch: redrive sweep, candidates=%d redriven=%d exhausted=%d",
                candidates, redriven, exhausted)
    return {"candidates": candidates, "redriven": redriven, "exhausted": exhausted}
