"""
Lambda: fieldsight-ingest v1.0 — dashboard read-model ingestion (Phase 4a)

In-VPC (psycopg direct to Aurora; mirrors OrgApiFunction's VPC/PG pattern).
Reads a daily_report.json (+ that day's transcripts), resolves site/user via
an identity bridge into the org tables (Phase 3), scope-deletes the prior
ingest for that (site_id, report_date, user_id), then re-inserts topics
(with action_items/safety_observations children) and Titan-embedded
report_chunks (topic + transcript_window rows). Idempotent: rerunning the
same report produces the same rows, never duplicates.

Entry points (event shapes):
  - S3 event: {"Records": [{"s3": {"object": {
        "key": "reports/<date>/<User_Folder>/daily_report.json"}}}]}
    S3 event notifications encode spaces as '+' and other special chars as
    %XX -- the key is ALWAYS unquote_plus'd before use (matches the real S3
    object key, since S3 itself doesn't URL-encode the stored key).
  - Manual single: {"date": "2026-03-02", "user": "Jarley_Trainor"}
  - Backfill: {"backfill": true} -- lists the `reports/` prefix, ingests
    every */daily_report.json found. Per-item failures are isolated (one
    report's exception does not stop the rest); returns
    {"processed": N, "skipped": [{"key","reason"}], "failed": [{"key","error"}]}.

Identity bridge (never invents a site -- real 2026-03-20 case: report['site']
== 'BD Opportunity Brainstorm', not a real site). DB-driven since the Phase 1
identity-directory consolidation (migration 0007 + org-seed enrollment) --
no more folder/display-name-matching heuristics against user_mapping.json:
  1. report['site'] (display name) -> sites.get_company_site_by_name.
  2. miss -> resolve the REPORTING USER via users.get_by_folder_name(
     company_id, user_folder) -> their first accessible site via
     memberships.accessible_site_ids -> sites.get_site.
  3. still miss -> SKIP the whole report (zero writes), return a reason string.
user_id bridge: users.get_by_folder_name(company_id, user_folder) -- a direct
  company+folder_name lookup (login users get folder_name backfilled onto
  their existing row; field_only reporters -- device-only, no Cognito login
  -- are enrolled with folder_name directly, org-seed Task 2). Miss -> user_id
  stays None (nullable column) -- this does NOT skip the report, unlike a
  site-bridge miss.

Embeddings (Phase 4d): Bedrock is retired from this lambda. This lambda runs
in-VPC with no internet egress, so it never calls an embedding API directly.
Instead, a separate non-VPC `embed-report` lambda pre-computes embeddings
(DashScope text-embedding-v4) for a report's chunks and writes them to an S3
"vector sidecar": embeddings/{date}/{user}/vectors.json, a JSON object
mapping sha256(chunk_text[:8000]) -> a 1024-float vector. This lambda loads
that sidecar (S3 gateway endpoint, no internet needed) and looks up each
chunk's embedding by the SAME hash (sha256 of the chunk text truncated to the
same 8000 chars -- the truncation must match on both sides or every lookup
misses). The looked-up vector is formatted as a '[f1,f2,...]' string, bound
through insert_chunk's %s::vector cast (no pgvector/numpy packing, no new
Lambda layer -- see repositories/chunks.py). A missing hash raises (the
report_chunks.embedding column is NOT NULL -- there is no "insert with a
blank embedding" fallback; it means embed-report hasn't run for that chunk
yet).
"""
import hashlib
import json
import logging
import os
import re
from urllib.parse import unquote_plus

import boto3

from chunking import chunk_report, chunk_transcripts
from db.connection import get_connection
from repositories import chunks, companies, memberships, sites, topics, users
from transcript_utils import normalize_transcript

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
CONFIG_KEY = os.environ.get("CONFIG_KEY", "config/user_mapping.json")
COMPANY_NAME = os.environ.get("COMPANY_NAME", "FieldSight")

REPORTS_PREFIX = "reports/"
REPORT_KEY_RE = re.compile(r"^reports/([^/]+)/([^/]+)/daily_report\.json$")
EMBEDDINGS_KEY_RE = re.compile(r"^embeddings/([^/]+)/([^/]+)/vectors\.json$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_s3_client = None
_mapping_cache = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def load_mapping() -> dict:
    """Load + cache config/user_mapping.json for the module's lifetime
    (warm Lambda container) -- mirrors lambda_org_seed.load_mapping."""
    global _mapping_cache
    if _mapping_cache is None:
        obj = s3().get_object(Bucket=S3_BUCKET, Key=CONFIG_KEY)
        _mapping_cache = json.loads(obj["Body"].read().decode("utf-8"))
    return _mapping_cache


# ----------------------------------------------------------
# Embeddings (S3 vector sidecar -- Bedrock retired, Phase 4d)
# ----------------------------------------------------------
def _sidecar_key(report_key: str) -> str:
    """reports/{date}/{user}/daily_report.json -> embeddings/{date}/{user}/
    vectors.json -- the S3 vector-sidecar path the non-VPC embed-report
    lambda writes and this (in-VPC) lambda reads."""
    m = REPORT_KEY_RE.match(report_key)
    if not m:
        raise ValueError(f"unexpected report key shape: {report_key!r}")
    date, user_folder = m.group(1), m.group(2)
    return f"embeddings/{date}/{user_folder}/vectors.json"


def _load_vectors(bucket: str, sidecar_key: str) -> dict:
    """Fetch + parse the vector-sidecar JSON: {sha256(chunk_text[:8000]):
    [1024 floats]}. Plain S3 GET -- reachable in-VPC via the S3 gateway
    endpoint, no internet required."""
    obj = s3().get_object(Bucket=bucket, Key=sidecar_key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def embed_from_sidecar(text: str, vectors: dict) -> str:
    """Look up a chunk's precomputed embedding by sha256(text[:8000]) and
    format it as the '[...]' string insert_chunk binds via ::vector.

    The 8000-char truncation-before-hash MUST match what embed-report hashes
    on its side (dashscope_utils / lambda_embed_report) -- if the two sides
    truncate differently, EVERY lookup here misses (this is the single most
    load-bearing detail of the sidecar contract).
    """
    h = hashlib.sha256(text[:8000].encode("utf-8")).hexdigest()
    try:
        vec = vectors[h]
    except KeyError:
        raise KeyError(
            f"no precomputed vector for chunk hash {h[:12]} — embed-report must run first"
        )
    return "[" + ",".join(repr(v) for v in vec) + "]"


# ----------------------------------------------------------
# Identity bridge
# ----------------------------------------------------------
def resolve_site(conn, company_id, report, user_folder):
    """report['site'] direct match -> the reporting user's own site
    membership (folder_name -> user row -> their first accessible site, all
    DB lookups, no user_mapping.json name heuristic) -> None (caller skips;
    never creates a site)."""
    name = (report.get("site") or "").strip()   # tolerate stray whitespace (Fable minor 7)
    if name:
        site = sites.get_company_site_by_name(conn, company_id, name)
        if site:
            return site

    user = users.get_by_folder_name(conn, company_id, user_folder)
    if user and memberships.resolve_scope(user["global_role"]) != "ALL":
        # F4 (Fable review): only use this fallback for non-ALL scope
        # (field_only/worker/site_manager) users. accessible_site_ids
        # returns EVERY company site for ALL scope (admin/gm) with no
        # ordering, so site_ids[0] would be an arbitrary site -- an
        # admin/gm has no single "home" site to attribute a report to, so
        # skip (None/caller-skips) rather than guess.
        site_ids = memberships.accessible_site_ids(conn, user["id"], user["global_role"])
        if site_ids:
            return sites.get_site(conn, site_ids[0])
    return None


def resolve_user(conn, company_id, user_folder):
    """Direct company+folder_name lookup against the identity directory.
    Miss -> None (nullable column; does not skip the report)."""
    row = users.get_by_folder_name(conn, company_id, user_folder)
    return row["id"] if row else None


# ----------------------------------------------------------
# Report-topic child shape mapping (report JSON -> repositories/topics.py)
# ----------------------------------------------------------
def _map_action_items(items):
    """Report action_items use 'action' for the task text; action_items'
    DB column is 'text' (see repositories/topics.py). 'deadline' in reports
    is free text ('EOD', 'Tomorrow 08:00', ...) per lambda_report_generator's
    schema, but the column is a SQL date -- only pass through values that
    already look like an ISO date, else drop to NULL rather than have a
    real ingest 500 on a strptime-hostile string."""
    out = []
    for a in items or []:
        deadline = a.get("deadline")
        if not (isinstance(deadline, str) and _ISO_DATE_RE.match(deadline)):
            deadline = None
        out.append({
            "text": a.get("action", ""),
            "responsible": a.get("responsible"),
            "deadline": deadline,
            "priority": a.get("priority"),
        })
    return out


def _map_safety(flags):
    """Report safety_flags has no 'location' field, and safety_observations
    has no column for 'recommended_action' -- that text is still preserved
    in the topic chunk's embedded text (chunking._topic_text); it's just not
    duplicated into a structured column here."""
    return [{
        "observation": s.get("observation", ""),
        "risk_level": s.get("risk_level"),
    } for s in (flags or [])]


# ----------------------------------------------------------
# Transcripts for a (user_folder, date)
# ----------------------------------------------------------
def _load_turns(user_folder, date):
    """List transcripts/{user_folder}/{date}/*.json, normalize each (skip
    unparseable ones), flatten speaker_turns with a caller-added 'src' key
    (the source transcript filename) for chunk_transcripts."""
    prefix = f"transcripts/{user_folder}/{date}/"
    turns = []
    paginator = s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            filename = key.rsplit("/", 1)[-1]
            raw = s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                # One corrupt transcript file must not fail the whole
                # report — skip it (makes the docstring's promise true;
                # Fable review minor 3).
                logger.warning("unparseable transcript skipped: %s", key)
                continue
            normalized = normalize_transcript(data, filename)
            if normalized is None:
                continue
            for turn in normalized["speaker_turns"]:
                # Turns without a resolvable base time can't be sorted or
                # assigned — drop them instead of TypeError-ing the report
                # (Fable review minor 2).
                if turn.get("abs_start") is None:
                    continue
                turns.append({**turn, "src": filename})
    return turns


# ----------------------------------------------------------
# Per-report ingest (commit-per-report: one `with get_connection()` here)
# ----------------------------------------------------------
def ingest_report(date, user_folder, report_key):
    raw = s3().get_object(Bucket=S3_BUCKET, Key=report_key)["Body"].read()
    report = json.loads(raw.decode("utf-8"))

    # A report with zero chunks (no topics AND no transcript turns) gets no
    # sidecar from embed-report; the event path never fires ingest for it, but
    # the backfill path (lists reports/) would. Treat a missing sidecar as a
    # clean skip, not a failure (Fable review M3).
    try:
        vectors = _load_vectors(S3_BUCKET, _sidecar_key(report_key))
    except s3().exceptions.NoSuchKey:
        reason = "no vector sidecar (zero-chunk report or embed-report not yet run)"
        logger.info("%s: %s", report_key, reason)
        return {"skipped": True, "reason": reason}

    with get_connection() as conn:
        company = companies.get_company_by_name(conn, COMPANY_NAME)
        if company is None:
            # Unseeded org DB would otherwise surface as an opaque
            # 'NoneType' subscript error on every report (Fable minor 6).
            raise RuntimeError(
                f"org company {COMPANY_NAME!r} not found — run the org seed "
                "(fieldsight-*-org-seed) before ingesting")
        site = resolve_site(conn, company["id"], report, user_folder)
        if site is None:
            reason = (f"identity bridge miss: report.site={report.get('site')!r}, "
                      f"user_folder={user_folder!r} -- skipping, zero writes")
            logger.warning("%s: %s", report_key, reason)
            return {"skipped": True, "reason": reason}

        user_id = resolve_user(conn, company["id"], user_folder)

        # Source-key idempotency: clear everything THIS report produced
        # before re-inserting. Keyed on source_s3_key, not (site, date,
        # user_id) — a NULL-user scope key let two same-site/same-date
        # reports (MPI1 + MPI2, both unresolved users) delete each other,
        # and identity fixes + rerun would duplicate (Fable review C1/I1).
        chunks.delete_chunks_for_source(conn, report_key)
        topics.delete_topics_for_source(conn, report_key)
        # Nightly report supersedes that day's session-sourced (live
        # extraction) items — Phase 4b.
        topics.delete_topics_for_source_prefix(conn, f"extractions/{user_folder}/{date}/")

        topic_seq_to_id = {}
        for t in report.get("topics", []):
            row = topics.upsert_topic(
                conn, site["id"], date, t.get("topic_title", ""),
                user_id=user_id, source_s3_key=report_key,
                category=t.get("category"), summary=t.get("summary"),
                action_items=_map_action_items(t.get("action_items")),
                safety=_map_safety(t.get("safety_flags")),
            )
            # None keys stay out of the map: a literal "topic_id": null topic
            # must not adopt the unassigned transcript windows (Fable minor 1).
            if t.get("topic_id") is not None:
                topic_seq_to_id[t.get("topic_id")] = row["id"]

        chunks_n = 0
        for c in chunk_report(report):
            embedding = embed_from_sidecar(c["chunk_text"], vectors)
            chunks.insert_chunk(
                conn, site["id"], date, c["chunk_type"], c["chunk_text"], embedding,
                user_id=user_id, source_s3_key=report_key,
                topic_id=topic_seq_to_id.get(c["topic_seq"]),
                metadata=c["metadata"],
            )
            chunks_n += 1

        turns = _load_turns(user_folder, date)
        for c in chunk_transcripts(report, turns):
            embedding = embed_from_sidecar(c["chunk_text"], vectors)
            chunks.insert_chunk(
                conn, site["id"], date, c["chunk_type"], c["chunk_text"], embedding,
                user_id=user_id, source_s3_key=report_key,
                topic_id=topic_seq_to_id.get(c["topic_seq"]),
                metadata=c["metadata"],
            )
            chunks_n += 1

    topics_n = len(topic_seq_to_id)
    logger.info("ingested report=%s topics=%d chunks=%d", report_key, topics_n, chunks_n)
    return {"skipped": False, "topics": topics_n, "chunks": chunks_n}


# ----------------------------------------------------------
# Backfill (per-item failure isolation)
# ----------------------------------------------------------
def _parse_report_key(key):
    m = REPORT_KEY_RE.match(key)
    if not m:
        return None
    return m.group(1), m.group(2)


def _parse_embeddings_key(key):
    """embeddings/{date}/{user_folder}/vectors.json -> (date, user_folder).
    This is the S3 event key shape the ingest trigger now fires on (migrated
    from reports/ -- embed-report writes the sidecar, which is what signals
    "this report's chunks are ready to ingest")."""
    m = EMBEDDINGS_KEY_RE.match(key)
    if not m:
        return None
    return m.group(1), m.group(2)


def _list_report_keys():
    paginator = s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=REPORTS_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/daily_report.json"):
                yield key


def run_backfill():
    processed, skipped, failed = 0, [], []
    for key in _list_report_keys():
        parsed = _parse_report_key(key)
        if parsed is None:
            # Bookkeep instead of silently dropping (Fable minor 4): a
            # daily_report.json at an unexpected depth is worth surfacing.
            skipped.append({"key": key, "reason": "unexpected key shape (depth)"})
            continue
        date, user_folder = parsed
        try:
            result = ingest_report(date, user_folder, key)
        except Exception as e:
            # Per-item isolation: one bad report must not roll back or
            # abort the rest of the backfill (each report commits its own
            # `with get_connection()` transaction in ingest_report).
            logger.exception("backfill: %s failed", key)
            failed.append({"key": key, "error": str(e)})
            continue
        if result.get("skipped"):
            skipped.append({"key": key, "reason": result.get("reason")})
        else:
            processed += 1
    return {"processed": processed, "skipped": skipped, "failed": failed}


# ----------------------------------------------------------
# Entry point
# ----------------------------------------------------------
def lambda_handler(event, context):
    event = event or {}
    if event.get("backfill"):
        return run_backfill()

    if "Records" in event:
        results = []
        for record in event["Records"]:
            key = unquote_plus(record["s3"]["object"]["key"])
            parsed = _parse_embeddings_key(key)
            if parsed is None:
                logger.warning("skipping non-embeddings S3 key: %s", key)
                continue
            date, user_folder = parsed
            report_key = f"{REPORTS_PREFIX}{date}/{user_folder}/daily_report.json"
            results.append(ingest_report(date, user_folder, report_key))
        return {"results": results}

    date = event["date"]
    user_folder = event["user"]
    key = f"{REPORTS_PREFIX}{date}/{user_folder}/daily_report.json"
    return ingest_report(date, user_folder, key)
