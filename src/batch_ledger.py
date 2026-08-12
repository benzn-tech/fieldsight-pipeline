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

def pending_runs(rows, now: int, deadline_sec: int,
                 max_size: int = DEFAULT_MAX_BATCH) -> list[list[int]]:
    """The runs that may be sealed right now.

    A run of `max_size` consecutive indices is complete and seals immediately. A shorter run
    seals only once its newest member is older than `deadline_sec`.

    **A gap does not seal the run before it.** Sealing `[4,5]` the moment `7` appears would
    permanently exclude a chunk 6 that was merely slow — uploads arrive out of order and can
    be hours late, and a sealed batch is never reopened, so that exclusion is forever.
    Waiting for the deadline is what makes lateness recoverable and the deadline the only
    place the decision is made.

    A chunk dropped as silent is indistinguishable from a lost upload here, and should be:
    both mean the batch stops at that index.
    """
    from batch_stitch import plan_batches

    by_index = {int(r["chunk_index"]): r for r in rows}
    out = []
    for run in plan_batches(by_index.keys(), max_size=max_size):
        if len(run) >= max_size:
            out.append(run)
            continue
        newest = max(int(by_index[i].get("registered_at") or 0) for i in run)
        if now - newest >= deadline_sec:
            out.append(run)
    return out


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
    if existing.get("status") != "sealing":
        return None
    if now - int(existing.get("claimed_at") or 0) < retry_after_sec:
        return None
    table.put_item(Item=item)
    return item


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
