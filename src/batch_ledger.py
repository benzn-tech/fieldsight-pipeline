"""Which chunks are in a batch, and who gets to seal it.

Plan: docs/superpowers/plans/2026-08-11-batched-transcription.md (phase 2).
Pure functions over an injected table — no boto3, no environment, nothing at import time,
same rule as `chunk_stitch` and `batch_stitch`. Nothing imports this yet.

State lives in the transcriber's existing `TRANSCRIPT_TABLE` rather than a new one, because
that function already holds `DynamoDBCrudPolicy` on it and a new table is a new CloudFormation
resource, which is a new IAM grant on the deploy role — the omission that rolls a whole stack
back.

Two races, both with a silent losing side:

* **duplicate delivery** — S3 event notifications are at-least-once, so a chunk can arrive
  twice. Registered twice it would be in the batch twice and paid for twice.
* **two sealers** — a batch can be sealed by the arrival that completes it or by the sweep
  that notices it timed out, and those can coincide. Two winners means the same two minutes
  transcribed twice and two artifacts written for one stretch of audio.

Both are settled by a conditional write, not by reading first and then writing.
"""
from __future__ import annotations

DEFAULT_MAX_BATCH = 4

# How long a `sealing` claim may sit before another worker may take it over. The order of
# work is claim → write map → write WAV → mark sealed (the WAV is last because its S3 event
# is what triggers transcription), so a crash in the middle leaves a claim and no artifact.
# Without a re-drive window nothing would ever look at that batch again.
SEAL_RETRY_SECONDS = 900


def _pk(session_id: str) -> str:
    return f"BATCH#{session_id}"


def _member_sk(index: int) -> str:
    return f"CHUNK#{index:04d}"


def _seal_sk(first_index: int) -> str:
    return f"SEAL#{first_index:04d}"


def _is_conditional_failure(exc: Exception) -> bool:
    """boto3 raises a dynamically-built class, so the name is what identifies it.

    Matched by name rather than by import: `botocore.exceptions.ClientError` subclasses are
    generated per service, and importing the resource-level exception at module scope would
    put boto3 in this module's import graph for no other reason.
    """
    return "ConditionalCheckFailed" in type(exc).__name__


# ============================================================
# Registration
# ============================================================

def register_chunk(table, session_id: str, index: int, chunk_key: str, now: int) -> str:
    """Record that this chunk arrived. Returns "registered" or "already_present".

    Conditional on the item not existing, so a duplicate S3 delivery is a no-op rather than
    a second member — and the caller can tell the difference, which matters: only a genuine
    first arrival should go on to consider sealing.
    """
    try:
        table.put_item(
            Item={"PK": _pk(session_id), "SK": _member_sk(index),
                  "chunk_index": index, "chunk_key": chunk_key, "registered_at": now},
            ConditionExpression="attribute_not_exists(SK)",
        )
    except Exception as e:
        if _is_conditional_failure(e):
            return "already_present"
        raise
    return "registered"


def list_members(table, session_id: str) -> list[dict]:
    """Every registered chunk of one session, in index order.

    Scoped to the session's partition key: a query that could see another session's chunks
    would batch two people's audio into one request.
    """
    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": _pk(session_id), ":sk": "CHUNK#"},
    )
    return sorted(resp.get("Items") or [], key=lambda r: int(r["chunk_index"]))


# ============================================================
# Which runs are ready
# ============================================================

# `pending_runs` was removed on 2026-08-13 with `plan_batches`, not deprecated alongside
# `pending_windows`. It grouped consecutive indices, so it could not see a window that
# bridges a VAD-dropped chunk — and it planned over consumed members, which is how a late
# earlier chunk could get a sealed window transcribed and billed twice.


def consumed_indices(rows) -> set:
    """Members already sealed into a batch.

    Kept as a named function rather than an inline comprehension because it is the one
    thing standing between a late upload and a second bill.
    """
    return {int(r["chunk_index"]) for r in rows if r.get("sealed_into") is not None}


def _base_times(rows):
    """chunk index -> device base time, out of the filename that carries it.

    Parsed rather than stored so a row written before this change still plans correctly,
    and so a filename shape the real parser cannot read fails here instead of silently
    getting a made-up time.
    """
    from transcript_utils import extract_base_time_from_filename

    out = {}
    for r in rows:
        key = r.get("chunk_key") or ""
        base = extract_base_time_from_filename(key.rsplit("/", 1)[-1])
        if base is not None:
            out[int(r["chunk_index"])] = base
    return out


def _bypass_sk(index: int) -> str:
    return f"BYPASS#{index:04d}"


def bucket_for_index(row, window_sec: float = 120.0):
    """The wall-clock bucket a member row belongs to, from its filename."""
    from batch_stitch import bucket_of
    from transcript_utils import extract_base_time_from_filename

    base = extract_base_time_from_filename((row.get("chunk_key") or "").rsplit("/", 1)[-1])
    return None if base is None else bucket_of(base, window_sec)


def bypass_status(table, session_id: str, index: int):
    """Whether THIS member was handed back to the per-chunk path.

    Member-scoped on purpose. The transcriber asks "was this chunk bypassed" to decide
    whether a copy-to-self event falls through to transcription, and under bucket seal keys
    a lookup by chunk index against the seal record misses entirely — after which the
    re-driven audio is neither batched nor transcribed.
    """
    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": _pk(session_id), ":sk": _bypass_sk(index)},
    )
    items = resp.get("Items") or []
    return items[0].get("status") if items else None


def pending_buckets(rows, now: int, grace_sec: int, window_sec: float = 120.0,
                    cap: int = DEFAULT_MAX_BATCH, table=None, session_id=None) -> dict:
    """`{"ready": [(bucket, [idx…])…], "stragglers": [idx…]}`. Supersedes `pending_windows`.

    The bucket comes from each chunk's own base time, so every worker computes the same key
    for the same chunk however much of the session it can see. That is what makes the claim
    able to exclude a rival: under the previous greedy anchor, two workers with different
    snapshots produced two different keys over the same chunks and neither saw the other —
    123 batch objects for 153 chunks on a real replay.

    **Stragglers are the other half, and without them this change is worse than the bug it
    fixes.** A worker that saw 3 of a bucket's 4 chunks seals it; the 4th then plans into a
    bucket whose claim is already `sealed`, and a sealed claim is never re-driven by design.
    That member would never be transcribed by anything. It is handed to the per-chunk path
    instead. Detecting it needs the table, so a caller that passes none simply gets no
    straggler list — the planning half still works for the pure tests.
    """
    from batch_stitch import plan_buckets

    consumed = consumed_indices(rows)
    times = _base_times(rows)
    by_index = {int(r["chunk_index"]): r for r in rows}
    planned = plan_buckets(times, window_sec=window_sec, consumed=consumed)

    ready, stragglers = [], []
    for bucket, idxs in sorted(planned.items()):
        if table is not None and session_id is not None                 and seal_status(table, session_id, bucket) == "sealed":
            stragglers.extend(idxs)
            continue
        if len(idxs) >= cap or _bucket_is_closed(idxs, times, bucket, window_sec):
            ready.append((bucket, idxs))
            continue
        newest = max(int(by_index[i].get("registered_at") or 0) for i in idxs)
        if now - newest >= grace_sec:
            ready.append((bucket, idxs))

    # Unplaceable members travel alone rather than vanishing, same rule as before.
    for r in rows:
        i = int(r["chunk_index"])
        if i not in times and i not in consumed:
            stragglers.append(i)
    return {"ready": ready, "stragglers": sorted(set(stragglers))}


def _bucket_is_closed(idxs, times, bucket, window_sec) -> bool:
    """True when a member of a LATER bucket has already registered.

    Nothing can still join a bucket whose successor has arrived, so it seals on arrival
    exactly as the contiguous-window rule did — the common case does not wait for grace.
    """
    from batch_stitch import bucket_of

    return any(bucket_of(t, window_sec) > bucket for t in times.values())


def pending_windows(rows, now: int, grace_sec: int,
                    window_sec: float = 120.0,
                    cap: int = DEFAULT_MAX_BATCH) -> list[list[int]]:
    """The wall-clock windows that may be sealed right now. Replaces `pending_runs`.

    **Two clocks, one job each.** Membership is decided on the device base times in the
    filenames, only ever compared to each other. Readiness is decided on `registered_at`,
    the server epoch — a window seals once its newest member has been quiet for
    `grace_sec`. Judging readiness on the device clock would find a backlogged session's
    window long closed and seal its first chunk alone, with zero effective grace, while its
    sisters were still uploading.

    **The common case does not wait.** A window whose indices are contiguous can gain no
    new member once its successor has registered outside it, or once the cap is hit, so it
    seals on arrival exactly as `pending_runs` did. Making everything wait for grace would
    add 150 s to 383 batches in order to remove it from 26.

    **A window with an interior hole always waits.** The hole is either a VAD drop that is
    never coming or an upload that is merely slow, and no event distinguishes them. Grace
    is the only arbiter of that, and it stays the only one.
    """
    consumed = consumed_indices(rows)
    times = _base_times(rows)
    by_index = {int(r["chunk_index"]): r for r in rows}
    known = sorted(times)

    from batch_stitch import plan_windows

    # A member whose filename the parser cannot read has no place on the device clock, so it
    # cannot be given a window — but it must not be dropped either: that would leave a
    # registered chunk nothing ever seals and nothing ever transcribes, which is audio gone
    # with no error anywhere. It travels alone. One extra request; the words survive.
    unplaceable = [int(r["chunk_index"]) for r in rows
                   if int(r["chunk_index"]) not in times
                   and int(r["chunk_index"]) not in consumed]

    out = []
    for window in plan_windows(times, window_sec=window_sec, cap=cap, consumed=consumed) \
            + [[i] for i in sorted(unplaceable)]:
        if _cannot_grow(window, times, known, window_sec, cap):
            out.append(window)
            continue
        newest = max(int(by_index[i].get("registered_at") or 0) for i in window)
        if now - newest >= grace_sec:
            out.append(window)
    return out


def _cannot_grow(window, times, known, window_sec, cap) -> bool:
    """True when no chunk that has not arrived could still join this window."""
    if window != list(range(window[0], window[0] + len(window))):
        return False                       # an interior index is missing; it may yet land
    if len(window) >= cap:
        return True
    nxt = window[-1] + 1
    if nxt not in times or window[0] not in times:
        # Either the successor has not registered, or this window's own anchor has no
        # readable base time (an unplaceable member travelling alone). Subtracting a
        # missing anchor raised KeyError, which propagated out of `pending_windows` --
        # so EVERY arrival for that session errored and `_seal_tail_batches` swallowed
        # it, leaving the whole session's audio sealed by nothing and transcribed by
        # nothing. Wait for grace instead; it is the safe answer to both.
        return False
    return (times[nxt] - times[window[0]]).total_seconds() >= window_sec


# ============================================================
# Sealing
# ============================================================

def claim_seal(table, session_id: str, first_index: int, members, now: int,
               retry_after_sec: int = SEAL_RETRY_SECONDS):
    """Take ownership of sealing this batch, or return None if someone else has it.

    Conditional on there being no claim at all. A claim that is already `sealed` is never
    re-driven — that would buy a second paid transcription for a batch that is already
    correct — while a `sealing` claim older than `retry_after_sec` is taken over, because
    the alternative is a batch nothing ever looks at again.

    The stale takeover reads before it writes, which is a race in principle. It is bounded
    to two workers both finding the same 15-minute-old abandoned claim in the same instant,
    and the cost is one duplicated batch rather than a permanently stuck one. Tightening it
    would need a version attribute in the condition, and that is worth doing only if the
    logs ever show it happening.
    """
    item = {"PK": _pk(session_id), "SK": _seal_sk(first_index),
            "status": "sealing", "members": list(members), "claimed_at": now}
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(SK)")
        return item
    except Exception as e:
        if not _is_conditional_failure(e):
            raise

    existing = _get_seal(table, session_id, first_index)
    if existing is None:
        return None
    # A stale `bypassed` record is retakeable for the same reason a stale `sealing` one
    # is: it means the copy-to-self never completed. It can only be reached while the
    # member is still unconsumed -- once consumed, the planner never proposes it again
    # and nothing calls this -- so there is no path by which a bypass that DID work
    # gets re-driven and paid for twice.
    if existing.get("status") not in ("sealing", "bypassed"):
        return None
    if now - int(existing.get("claimed_at") or 0) < retry_after_sec:
        return None
    table.put_item(Item=item)
    return item


def mark_members_consumed(table, session_id: str, indices, batch_first_index: int) -> None:
    """Record that these members now belong to a sealed batch.

    Written after BOTH artifacts and before `mark_sealed`, so a crash before they exist
    leaves the members unconsumed and the stale claim retakeable — the re-drive window
    survives. Once the artifacts are on S3 the batch is real, and consuming its members is
    what stops a late sibling re-planning and re-billing the same audio.

    The row is rewritten in full rather than replaced by a marker: `chunk_key` is the only
    record of which object a member was, and losing it makes a sealed batch impossible to
    diagnose afterwards.
    """
    wanted = {int(i) for i in indices}
    for row in list_members(table, session_id):
        if int(row["chunk_index"]) not in wanted:
            continue
        item = dict(row)
        item["sealed_into"] = int(batch_first_index)
        table.put_item(Item=item)


def seal_status(table, session_id: str, first_index: int):
    """"sealing" | "sealed" | "bypassed" | None for the batch anchored at this index."""
    existing = _get_seal(table, session_id, first_index)
    return existing.get("status") if existing else None


def mark_bypassed(table, session_id: str, index: int, now: int) -> None:
    """A window of one was handed back to the per-chunk path; no artifact exists.

    Recorded with its own status rather than as `sealed`, so a later question of "why is
    there no `_bn` object for this stretch" has an answer in the ledger instead of looking
    like the seal that never happened.
    """
    # `claimed_at` as well as `bypassed_at`: the staleness check in `claim_seal` reads
    # `claimed_at`, and a record without one is `now - 0` old — i.e. instantly abandoned, so
    # the 900-second window that is supposed to bound the two-sealers race did not exist for
    # bypassed records at all. `bypassed_at` stays because it says WHEN, which `claimed_at`
    # on a bypass record would read as a claim that is still in flight.
    item = {"status": "bypassed", "bypassed_at": now, "claimed_at": now}
    # BOTH keys. The seal-shaped one keeps the existing claim/takeover machinery working;
    # the member-scoped one is what the transcriber can actually look up when the seal key
    # is a bucket rather than this chunk's index.
    table.put_item(Item={"PK": _pk(session_id), "SK": _seal_sk(index), **item})
    table.put_item(Item={"PK": _pk(session_id), "SK": _bypass_sk(index), **item})


def release_claim(table, session_id: str, first_index: int, claimed_at: int) -> bool:
    """Give up a claim whose work failed, so the next tick can retry instead of waiting.

    A seal that raises leaves a `sealing` claim behind, and that claim silences every later
    attempt for `SEAL_RETRY_SECONDS`. For a session's TAIL that is not a delay but a
    permanent loss: `_seal_tail_batches` runs once per session at finalize and nothing calls
    it again, so the takeover never fires and those chunks are never transcribed. Twice on
    TEST, both times a missing S3 grant, both times found by reading logs.

    Conditional on the claim still being OURS. An unconditional delete would let a slow
    failing worker wipe the claim of the worker that took over from it, after which both
    would seal the same audio.
    """
    try:
        table.delete_item(
            Key={"PK": _pk(session_id), "SK": _seal_sk(first_index)},
            ConditionExpression="#s = :sealing AND claimed_at = :mine",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":sealing": "sealing", ":mine": claimed_at},
        )
        return True
    except Exception as e:
        if _is_conditional_failure(e):
            return False            # someone else holds it, or it finished
        raise


def mark_sealed(table, session_id: str, first_index: int, now: int) -> None:
    """The artifacts are written; this batch is finished and must never be re-driven."""
    table.put_item(Item={"PK": _pk(session_id), "SK": _seal_sk(first_index),
                         "status": "sealed", "sealed_at": now})


def _get_seal(table, session_id: str, first_index: int):
    resp = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": _pk(session_id), ":sk": _seal_sk(first_index)},
    )
    items = resp.get("Items") or []
    return items[0] if items else None
