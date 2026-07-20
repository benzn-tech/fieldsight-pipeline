"""
Lambda: fieldsight-embed-report v1.0 — DashScope embedding sidecar writer
(Phase 4d, Task 2).

Non-VPC (needs public internet to reach DashScope; mirrors
lambda_report_generator.py's / claude_utils.py's urllib3 HTTP pattern, both
also non-VPC). This lambda never touches Aurora -- it has no VpcConfig and
no psycopg connection of its own.

Reads a daily_report.json (+ that day's transcripts, via lambda_ingest's
existing `_load_turns(user_folder, date)` -- same function, same chunking as
the in-VPC ingest lambda uses, so both sides describe the exact same set of
chunks), chunks it with the SAME chunking.py the ingest lambda uses
(chunk_report + chunk_transcripts), embeds each unique chunk text with
DashScope text-embedding-v4, and writes an S3 "vector sidecar":
  embeddings/{date}/{user_folder}/vectors.json
    = { sha256(chunk_text[:8000] utf-8).hexdigest(): [1024 floats], ... }

lambda_ingest (in-VPC, no internet egress) reads this sidecar over the S3
gateway endpoint and looks up each chunk's embedding by the SAME hash
(lambda_ingest.embed_from_sidecar). The 8000-char truncation-before-hash here
MUST byte-for-byte match that side's `hashlib.sha256(text[:8000]
.encode('utf-8')).hexdigest()` expression -- see `_chunk_hash` below and
tests/unit/test_lambda_embed_report.py::test_hash_matches_ingest_sidecar*,
a cross-check that imports both modules and proves lambda_ingest.
embed_from_sidecar actually finds what this module writes. If the two sides
ever diverge, EVERY lookup misses and the whole backfill produces zero rows.

Entry point (event shape):
  - S3 event: {"Records": [{"s3": {"object": {
        "key": "reports/<date>/<User_Folder>/daily_report.json"}}}]}
    S3 event notifications encode spaces as '+' and other special chars as
    %XX -- the key is ALWAYS unquote_plus'd before use (matches the real S3
    object key). Keys at any other depth/suffix under reports/ (or under any
    other prefix) are skipped, not errored.

Environment Variables:
    S3_BUCKET   - S3 bucket name (the ingest bucket -- same bucket
                  lambda_ingest reads/writes; production sets both Lambdas'
                  S3_BUCKET to the same value, so reusing lambda_ingest's
                  _load_turns "just works" without any cross-module state
                  syncing)
    CONFIG_KEY  - config/user_mapping.json (unused here -- kept only for env
                  footprint parity with lambda_ingest; no identity bridge
                  runs in this lambda)
    DASHSCOPE_*  - see dashscope_utils.py
"""
import hashlib
import json
import logging
import os
import re
from urllib.parse import unquote_plus

import boto3

import dashscope_utils
import lambda_ingest
import reindex
import text_normalize
from chunking import chunk_report, chunk_transcripts

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
CONFIG_KEY = os.environ.get("CONFIG_KEY", "config/user_mapping.json")

REPORT_KEY_RE = re.compile(r"^reports/([^/]+)/([^/]+)/daily_report\.json$")

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _sidecar_key(date, user_folder):
    return f"embeddings/{date}/{user_folder}/vectors.json"


def _chunk_hash(text):
    """The vector-sidecar map key for a chunk's text -- MUST be byte-
    identical to lambda_ingest.embed_from_sidecar's lookup expression
    (sha256 of the text truncated to 8000 chars, utf-8 encoded)."""
    return hashlib.sha256(text[:8000].encode("utf-8")).hexdigest()


def embed_report(date, user_folder, report_key):
    raw = s3().get_object(Bucket=S3_BUCKET, Key=report_key)["Body"].read()
    report = json.loads(raw.decode("utf-8"))

    # Same function the in-VPC ingest lambda uses to gather that day's
    # transcripts -- reused as-is rather than reimplemented (see module
    # docstring re: why no cross-module client/env syncing is needed).
    turns = lambda_ingest._load_turns(user_folder, date)
    chunks = chunk_report(report) + chunk_transcripts(report, turns)

    if not chunks:
        logger.info("no chunks for %s -- skipping (no sidecar written)", report_key)
        return {"report": report_key, "chunks": 0, "vectors": 0}

    # Dedupe by hash before calling DashScope: identical chunk_text (e.g. two
    # topics that render an identical header/summary) is only embedded once.
    unique_by_hash = {}
    for c in chunks:
        h = _chunk_hash(c["chunk_text"])
        unique_by_hash.setdefault(h, c["chunk_text"])

    hashes = list(unique_by_hash.keys())
    texts = [unique_by_hash[h] for h in hashes]
    embeddings = dashscope_utils.embed(texts)  # batches of <=10 handled inside

    vectors = dict(zip(hashes, embeddings))

    s3().put_object(
        Bucket=S3_BUCKET,
        Key=_sidecar_key(date, user_folder),
        Body=json.dumps(vectors),
        ContentType="application/json",
    )

    logger.info(
        "embedded report=%s chunks=%d unique_vectors=%d",
        report_key, len(chunks), len(vectors),
    )
    return {"report": report_key, "chunks": len(chunks), "vectors": len(vectors)}


REINDEX_REQUEST_RE = re.compile(
    r"^reindex_requests/[^/]+/[^/]+/[^/]+\.json$")


def embed_reindex_request(key):
    """Non-VPC reindex worker (spec §5.3). Embeds the corrected topic chunks +
    this topic's alias-normalized transcript windows, writes the vectors
    result artifact for the in-VPC apply step. Transcript S3 is read, never
    written (D4)."""
    req = json.loads(s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode("utf-8"))
    chunks_out = [dict(c) for c in req.get("topic_chunks", [])]

    # Rebuild THIS topic's transcript windows from the immutable report doc.
    if req.get("report_key") and req.get("topic_seq") is not None:
        try:
            raw = s3().get_object(Bucket=S3_BUCKET, Key=req["report_key"])["Body"].read()
            report = json.loads(raw.decode("utf-8"))
            one = [t for t in report.get("topics", [])
                   if t.get("topic_id") == req["topic_seq"]]
            if one:
                turns = lambda_ingest._load_turns(req["folder"], req["date"])
                for c in chunk_transcripts({"topics": one, **{k: report.get(k)
                                            for k in ("user_name", "site", "report_date")}}, turns):
                    text = text_normalize.normalize(c["chunk_text"], req.get("aliases") or [])
                    chunks_out.append({"chunk_type": c["chunk_type"], "chunk_text": text,
                                       "metadata": c["metadata"]})
        except Exception:
            logger.exception("reindex %s: transcript window rebuild failed (topic chunks only)", key)

    if not chunks_out:
        logger.info("reindex %s: no chunks -- skipping", key)
        return {"reindex": key, "chunks": 0}

    embeddings = dashscope_utils.embed([c["chunk_text"] for c in chunks_out])
    for c, e in zip(chunks_out, embeddings):
        c["embedding"] = e

    result = {
        "topic_id": req["topic_id"], "site_id": req["site_id"],
        "user_id": req.get("user_id"), "report_date": req["report_date"],
        "source_s3_key": req.get("source_s3_key"), "chunks": chunks_out,
    }
    vkey = reindex.vectors_key(req["date"], req["folder"], req["topic_id"])
    s3().put_object(Bucket=S3_BUCKET, Key=vkey,
                    Body=json.dumps(result), ContentType="application/json")
    logger.info("reindex embedded %s chunks=%d", key, len(chunks_out))
    return {"reindex": key, "chunks": len(chunks_out)}


def lambda_handler(event, context):
    event = event or {}
    results = []
    for record in event.get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])
        if key.endswith(".vectors.json"):
            continue                                    # apply-side input, not ours
        if REINDEX_REQUEST_RE.match(key):
            results.append(embed_reindex_request(key))
            continue
        m = REPORT_KEY_RE.match(key)
        if not m:
            logger.warning("skipping non-report S3 key: %s", key)
            continue
        date, user_folder = m.group(1), m.group(2)
        results.append(embed_report(date, user_folder, key))
    return {"results": results}
