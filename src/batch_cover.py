"""Which chunk wavs a batch already covers, so a listing can play each stretch of speech once.

Batching stitches several chunk wavs into one batch wav and writes it BESIDE them, with a
`*_batch_map.json` naming every member's key. A flat listing of the prefix therefore holds
the same speech twice. The map is the only authority on membership: the batch filename's
`c{first}_bn{count}` is not, because a chunk VAD rejected is bridged -- on prod 2026-09-10,
`c1035_bn4` holds 1035, 1036, 1037 and 1039.

Fail open, always. A map that is missing, forbidden or malformed hides nothing: the worst
this module may cause is a duplicate on screen, never a recording that vanished.

No boto3 and no psycopg here -- the read is injected, so the in-VPC org-api and the legacy
gateway share this without either one's client leaking into the other.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import batch_stitch

logger = logging.getLogger()

# One day measured at 215 batches, behind API Gateway's 29 s limit: read maps in parallel.
MAX_MAP_READERS = 16


def _members(get_bytes, batch_key):
    map_key = batch_stitch.map_key_for_audio(batch_key)
    try:
        doc = json.loads(get_bytes(map_key))
    except Exception as exc:  # noqa: BLE001 - any failure means "hide nothing"
        logger.warning("batch_cover: map unreadable for %s (%s) -- nothing hidden",
                       batch_key.rsplit("/", 1)[-1], type(exc).__name__)
        return set()
    members = doc.get("members") if isinstance(doc, dict) else None
    if not isinstance(members, list):
        return set()
    return {m["chunk_key"] for m in members
            if isinstance(m, dict) and isinstance(m.get("chunk_key"), str)
            and m["chunk_key"] and not batch_stitch.is_batch_key(m["chunk_key"])}


def covered_chunk_keys(get_bytes, keys, max_workers=MAX_MAP_READERS):
    """Union of the chunk keys named by the maps of the batch wavs among `keys`.

    `get_bytes(key) -> bytes` may raise; a raise contributes nothing. Non-batch keys are
    never read.
    """
    batches = [k for k in keys if batch_stitch.is_batch_key(k)]
    if not batches:
        return set()
    covered = set()
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(batches)))) as pool:
        for members in pool.map(lambda k: _members(get_bytes, k), batches):
            covered |= members
    return covered
