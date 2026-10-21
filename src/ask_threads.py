"""Ask conversation history: the per-caller thread store.

Spec docs/superpowers/specs/2026-10-22-ask-conversation-history-design.md section 3.

One object per thread at `ask_threads/{thread_prefix(sub)}/{thread_id}.json` in the
data bucket (ASK_TURN_BUCKET). The AskVoiceWorker appends (read, append, put; two
tabs racing on one thread can lose a turn, never the object); org-api lists, reads
and deletes. There is no index object: the list is one ListObjectsV2 on the
caller's prefix.

Nothing here logs a question, an answer, a source key or a document id. Log lines
carry a hash of the caller, thread ids, counts and byte sizes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

import ask_voice_turn

logger = logging.getLogger()

PREFIX = "ask_threads/"
THREAD_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_THREADS = 5
MAX_TURNS = 20
MAX_BYTES = 262144
EXPIRY_S = 30 * 86400
TITLE_CHARS = 80
# Same bound as ask_voice_turn.sweep_old: expiry runs on a user's request.
EXPIRE_MAX_DELETES = 50


def enabled() -> bool:
    """Read per call, never at import (a warm container outlives a redeploy)."""
    return os.environ.get("ASK_THREADS", "") == "true"


def thread_prefix(caller_sub) -> str:
    """The caller's 16-hex segment, taken from owner_prefix so the two stores can
    never hash a caller differently. sub_hash (12 hex) stays log-only."""
    return PREFIX + ask_voice_turn.owner_prefix(caller_sub)[len(ask_voice_turn.PREFIX):]


def thread_key(caller_sub, thread_id) -> str:
    return "%s%s.json" % (thread_prefix(caller_sub), thread_id)


def new_thread_id() -> str:
    return secrets.token_hex(16)


def _valid_id(thread_id) -> bool:
    return isinstance(thread_id, str) and bool(THREAD_RE.match(thread_id))


def _get(s3, bucket, key):
    """The parsed thread, or None when absent or not a thread. Unlike
    ask_voice_turn._read_json this RAISES on anything but a missing key: an
    AccessDenied read as "no thread" would mint a new one on the worker and show
    "no conversations" on the list."""
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        if ask_voice_turn._is_missing(exc):
            return None
        raise
    try:
        doc = json.loads(resp["Body"].read())
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("turns"), list):
        return None
    return doc


def _encode(doc) -> bytes:
    return json.dumps(doc).encode("utf-8")


def _fit(doc):
    """The encoded thread, at most MAX_BYTES, or None when it cannot fit. A last
    turn that would cross the cap keeps `result.answer` only, cut until it fits,
    with `result.truncated: true`. The question and the source manifest are never
    cut: the manifest is what the open-time re-check reads."""
    body = _encode(doc)
    if len(body) <= MAX_BYTES:
        return body
    turn = doc["turns"][-1]
    result = turn.get("result") if isinstance(turn.get("result"), dict) else {}
    answer = result.get("answer") if isinstance(result.get("answer"), str) else ""
    turn["result"] = {"answer": answer, "truncated": True}
    while True:
        body = _encode(doc)
        over = len(body) - MAX_BYTES
        if over <= 0:
            return body
        if not answer:
            return None
        # Every character is at least one encoded byte, so dropping `over`
        # characters drops at least `over` bytes.
        answer = answer[:max(0, len(answer) - over)]
        turn["result"]["answer"] = answer


def _new_doc(thread_id, scope, now):
    return {"v": 1, "thread_id": thread_id, "created_at": now, "updated_at": now,
            "scope": scope, "turns": []}


def append_turn(s3, bucket, caller_sub, thread_id, turn: dict, scope: dict, now: float) -> dict:
    """Append `turn` to the caller's thread and return
    {"thread_id", "rolled_over", "created"}.

    `thread_id` None mints a thread. A supplied id that is missing (evicted,
    expired, another caller's) or already holds MAX_TURNS mints a new thread
    instead, never re-creates the old id, and `rolled_over` says so. `scope` is
    stored only when the thread is created. Raises on any S3 error other than a
    missing key; the caller logs the type and carries on."""
    doc, rolled_over = None, False
    if thread_id is not None:
        if _valid_id(thread_id):
            doc = _get(s3, bucket, thread_key(caller_sub, thread_id))
        if (doc is None or len(doc["turns"]) >= MAX_TURNS
                or not isinstance(doc.get("updated_at"), (int, float))
                or now - doc["updated_at"] > EXPIRY_S):
            doc, rolled_over = None, True
    created = doc is None
    if created:
        doc = _new_doc(new_thread_id(), scope, now)
    doc["turns"].append(turn)
    doc["updated_at"] = now
    body = _fit(doc)
    if body is None and not created:
        # The earlier turns alone fill the object: this turn opens a new thread.
        doc, rolled_over, created = _new_doc(new_thread_id(), scope, now), True, True
        doc["turns"].append(turn)
        body = _fit(doc)
    if body is None:
        raise ValueError("thread turn does not fit")
    s3.put_object(Bucket=bucket, Key=thread_key(caller_sub, doc["thread_id"]), Body=body,
                  ContentType="application/json")
    logger.info("ask thread append: thread=%s turns=%d bytes=%d created=%s rolled_over=%s "
                "sub_hash=%s", doc["thread_id"], len(doc["turns"]), len(body), created,
                rolled_over, ask_voice_turn.sub_hash(caller_sub))
    return {"thread_id": doc["thread_id"], "rolled_over": rolled_over, "created": created}


def _objects(s3, bucket, caller_sub) -> list:
    """Every thread object under the caller's prefix as (key, LastModified epoch)."""
    out, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": thread_prefix(caller_sub)}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents") or []:
            key = obj["Key"]
            name = key[len(thread_prefix(caller_sub)):]
            if name.endswith(".json") and _valid_id(name[:-len(".json")]):
                out.append((key, ask_voice_turn._epoch(obj["LastModified"])))
        if not resp.get("IsTruncated"):
            return out
        token = resp.get("NextContinuationToken")
        if not token:
            return out


def evict(s3, bucket, caller_sub) -> int:
    """Delete the caller's oldest threads by LastModified until MAX_THREADS remain.
    Returns the number deleted."""
    objs = sorted(_objects(s3, bucket, caller_sub), key=lambda o: o[1], reverse=True)
    deleted = 0
    for key, _ in objs[MAX_THREADS:]:
        s3.delete_object(Bucket=bucket, Key=key)
        deleted += 1
    if deleted:
        logger.info("ask thread evict: deleted=%d sub_hash=%s", deleted,
                    ask_voice_turn.sub_hash(caller_sub))
    return deleted


def _title(doc) -> str:
    first = doc["turns"][0] if doc["turns"] and isinstance(doc["turns"][0], dict) else {}
    q = first.get("question")
    return q[:TITLE_CHARS] if isinstance(q, str) else ""


def list_threads(s3, bucket, caller_sub, now) -> list:
    """The caller's live threads, newest first, at most MAX_THREADS:
    [{thread_id, title, updated_at, turns}]. Objects older than EXPIRY_S are
    skipped and deleted (bounded). Any S3 error other than a missing key raises:
    an AccessDenied must not read as "no conversations"."""
    live, deleted = [], 0
    for key, modified in _objects(s3, bucket, caller_sub):
        if now - modified > EXPIRY_S:
            if deleted < EXPIRE_MAX_DELETES:
                s3.delete_object(Bucket=bucket, Key=key)
                deleted += 1
            continue
        live.append((key, modified))
    if not live:
        return []
    with ThreadPoolExecutor(max_workers=min(len(live), MAX_THREADS + 2)) as pool:
        docs = list(pool.map(lambda o: _get(s3, bucket, o[0]), live))
    out = []
    for (key, modified), doc in zip(live, docs):
        if doc is None:
            continue        # evicted or deleted between the list and the read
        updated = doc.get("updated_at")
        out.append({"thread_id": doc.get("thread_id") or key.rsplit("/", 1)[-1][:-len(".json")],
                    "title": _title(doc),
                    "updated_at": updated if isinstance(updated, (int, float)) else modified,
                    "turns": len(doc["turns"])})
    out.sort(key=lambda t: t["updated_at"], reverse=True)
    return out[:MAX_THREADS]


def get_thread(s3, bucket, caller_sub, thread_id, now=None):
    """The caller's thread, or None when the id is malformed, absent or expired.
    `now` defaults to the clock."""
    if not _valid_id(thread_id):
        return None
    doc = _get(s3, bucket, thread_key(caller_sub, thread_id))
    if doc is None:
        return None
    now = time.time() if now is None else now
    updated = doc.get("updated_at")
    if isinstance(updated, (int, float)) and now - updated > EXPIRY_S:
        return None
    return doc


def delete_thread(s3, bucket, caller_sub, thread_id) -> None:
    """Idempotent: deleting an absent key succeeds in S3. A malformed id is a no-op."""
    if not _valid_id(thread_id):
        return
    s3.delete_object(Bucket=bucket, Key=thread_key(caller_sub, thread_id))
