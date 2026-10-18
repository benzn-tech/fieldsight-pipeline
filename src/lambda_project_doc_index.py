"""lambda_project_doc_index.py -- read, chunk and embed one project document.

Non-VPC: it calls DashScope, and org-api / Ingest are in-VPC with no outbound (BUG-36).
Triggered by org-api writing project_doc_requests/{company}/{id}.json (wired by
scripts/wire-s3-events.sh, BUG-33). Writes project_doc_vectors/{company}/{id}.json, which
Ingest applies in one transaction. No database, no psycopg, and it never imports
lambda_ingest (spec ruling 9).

R9 -- never stuck pending: every request whose key parses ends in an artifact, failures
included (status error/unreadable with a reason the person reads). MaximumRetryAttempts is
0 on the function, so a failed run is not re-run into a second bill; the person retries.
Embedding runs against a deadline: the invocation's remaining time minus
WRITE_RESERVE_SECONDS. One DashScope batch can still outlast the reserve in the worst case
(4 attempts x 60 s); then the Lambda is killed with nothing written and the UI's
"Taking too long -- Retry" after 20 minutes is the backstop.

Spec: docs/superpowers/specs/2026-10-16-project-documents-design.md (Flow, R9, R13).
"""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3

import dashscope_utils
import project_doc_chunks
import project_doc_text
import project_doc_types
from lake_put import lake_put

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
REQUEST_RE = re.compile(rf"^project_doc_requests/(?P<company>{_UUID})/(?P<id>{_UUID})\.json$")
WRITE_RESERVE_SECONDS = 60

MSG_FAILED = "Something went wrong reading this document -- try again."
MSG_TOO_SLOW = "Too large to index in one go -- try again, or split the document."
MSG_TOO_MANY = "This document is too long to index -- split it into parts."
MSG_EMBED = "Could not index this document right now -- try again."

logger.info("project docs readers: docx=%s pdf=%s", project_doc_text.DOCX_AVAILABLE,
            project_doc_text.PDF_AVAILABLE)

_clock = time.monotonic   # test seam
_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def vectors_key(company, doc_id):
    return f"project_doc_vectors/{company}/{doc_id}.json"


class _OutOfTime(Exception):
    pass


class _EmbedFailed(Exception):
    pass


def _artifact(company, doc_id, request, status, reason=None, pages=None, chars=None,
              chunks=None):
    return {"document_id": doc_id, "company_id": company,
            "site_id": request.get("siteId"), "series_id": request.get("seriesId"),
            "revision": request.get("revision"), "status": status, "reason": reason,
            "pages": pages, "chars": chars, "chunks": chunks or [],
            "finished_at": datetime.now(timezone.utc).isoformat()}


#: project_doc_chunks.embedding is vector(1024) (migration 0088) and IngestFunction rejects
#: any other length (repositories.project_docs.EMBED_DIM). Passed explicitly so a changed
#: DASHSCOPE_EMBED_DIM default can never make every document fail at apply time.
EMBED_DIM = 1024


def _embed(texts, deadline_at):
    vectors = []
    for i in range(0, len(texts), dashscope_utils.BATCH_SIZE):
        if _clock() >= deadline_at:
            raise _OutOfTime()
        try:
            vectors.extend(dashscope_utils.embed(texts[i:i + dashscope_utils.BATCH_SIZE],
                                                 dim=EMBED_DIM))
        except RuntimeError as e:
            logger.warning("project doc embed failed: %s", e)
            raise _EmbedFailed()
    return vectors


def index(company, doc_id, request, deadline_at):
    """The artifact for one request (pure of S3 writes)."""
    src = request.get("sourceKey") or ""
    if not src.startswith(f"project_doc_sources/{company}/{doc_id}."):
        logger.error("project doc %s: source %s is outside its own prefix -- refused", doc_id, src)
        return _artifact(company, doc_id, request, "error", MSG_FAILED)
    ext = project_doc_types.ext_of(src)
    obj = s3().get_object(Bucket=S3_BUCKET, Key=src)
    size = obj.get("ContentLength")
    if size is not None and size > project_doc_types.MAX_BYTES:
        obj["Body"].close()
        return _artifact(company, doc_id, request, "unreadable", project_doc_text.MSG_TOO_BIG)
    data = obj["Body"].read()
    try:
        doc = project_doc_text.extract(data, ext)
    except project_doc_text.Unreadable as e:
        return _artifact(company, doc_id, request, "unreadable", e.reason, pages=e.pages)
    except project_doc_text.NotConfigured as e:
        return _artifact(company, doc_id, request, "error", e.reason)
    # Postgres rejects NUL in text and pypdf can emit it; strip before chunking so headings
    # and chunk text are both clean and a block that was only NULs drops out.
    blocks = [dict(b, text=(b.get("text") or "").replace("\x00", "")) for b in doc["blocks"]]
    chunks = project_doc_chunks.chunk(blocks)
    if not chunks:
        return _artifact(company, doc_id, request, "unreadable", project_doc_text.MSG_EMPTY,
                         pages=doc["pages"], chars=doc["chars"])
    if len(chunks) > project_doc_types.MAX_CHUNKS:
        return _artifact(company, doc_id, request, "unreadable", MSG_TOO_MANY,
                         pages=doc["pages"], chars=doc["chars"])
    try:
        vectors = _embed([project_doc_chunks.embed_text(c) for c in chunks], deadline_at)
    except _OutOfTime:
        return _artifact(company, doc_id, request, "error", MSG_TOO_SLOW,
                         pages=doc["pages"], chars=doc["chars"])
    except _EmbedFailed:
        return _artifact(company, doc_id, request, "error", MSG_EMBED,
                         pages=doc["pages"], chars=doc["chars"])
    return _artifact(company, doc_id, request, "indexed", pages=doc["pages"],
                     chars=doc["chars"],
                     chunks=[dict(c, embedding=v) for c, v in zip(chunks, vectors)])


def _process(key, context):
    m = REQUEST_RE.match(key)
    if not m:
        logger.warning("project doc: ignoring %s (not a request key)", key)
        return
    company, doc_id = m.group("company"), m.group("id")
    remaining = (context.get_remaining_time_in_millis() / 1000.0) if context else 840.0
    deadline_at = _clock() + max(0.0, remaining - WRITE_RESERVE_SECONDS)
    request = {}
    try:
        request = json.loads(s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
                             .decode("utf-8"))
        if (not isinstance(request, dict) or str(request.get("companyId")) != company
                or str(request.get("id")) != doc_id):
            logger.error("project doc: %s does not match its own key -- refused", key)
            request = {}
            result = _artifact(company, doc_id, request, "error", MSG_FAILED)
        else:
            result = index(company, doc_id, request, deadline_at)
    except Exception:  # noqa: BLE001 - R9: the person must get an answer, not a pending row
        logger.exception("project doc %s failed", doc_id)
        result = _artifact(company, doc_id, request if isinstance(request, dict) else {},
                           "error", MSG_FAILED)
    lake_put(S3_BUCKET, vectors_key(company, doc_id), json.dumps(result, ensure_ascii=False),
             owner_hint={"company_id": company, "site_id": result.get("site_id")},
             client=s3(), ContentType="application/json")
    logger.info("project doc %s: %s chunks=%d pages=%s", doc_id, result["status"],
                len(result["chunks"]), result.get("pages"))


def lambda_handler(event, context):
    for rec in (event or {}).get("Records", []):
        key = ((rec.get("s3") or {}).get("object") or {}).get("key")
        if key:
            _process(unquote_plus(key), context)
    return {"ok": True}
