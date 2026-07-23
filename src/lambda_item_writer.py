"""
Lambda: fieldsight-item-writer v1.0 — realtime extraction ingestion (Phase 4b).

In-VPC (psycopg direct to Aurora; mirrors lambda_ingest's VPC/PG pattern).
Reads one `extractions/{user_folder}/{date}/{session_base}.json` written by
lambda_extract_session (the session-extraction JSON contract -- see that
module's docstring and docs/superpowers/plans/2026-07-07-phase-4b-realtime.md
"Global Constraints"), resolves site/user via the SAME identity bridge as
Phase 4a's nightly ingest, scope-deletes the prior write for that extraction
key, then re-inserts topics (with action_items/safety_observations children).

The identity bridge, topic-child-shape mapping, and the "no seeded company"
guard are REUSED from lambda_ingest by import -- never copied:
  lambda_ingest.resolve_site / resolve_user / _map_action_items / _map_safety
  and the same RuntimeError message on a missing companies row.

Site resolution note: the extraction JSON has no 'site' field (unlike a
daily_report.json, which may carry report['site']) -- declared_site is only
ever stored for record in the extraction JSON, it is NOT consumed for site
attribution here. resolve_site is always called with an empty report dict,
which falls straight through to the user_mapping.json primary_site slug
bridge. A double miss (report has no site AND the mapping bridge also
misses) skips the extraction, zero writes -- exactly like lambda_ingest's
report-level site-bridge miss.

G5b: recordings.site_for_media (the app-tagged site, keyed on the
recording's own session_base) is now consulted FIRST and, when present,
overrides the membership resolver above -- resolve_site is only the
fallback when there is no matching tag.

Idempotency: keyed on source_s3_key = the extraction's own S3 key (delete
then re-insert) -- same source-key idempotency Phase 4a topics/chunks use,
so re-processing the same extraction (e.g. a re-triggered S3 event, or a
later session segment landing and re-writing the same extractions/ key)
never duplicates rows.

Entry point (event shape):
  - S3 event: {"Records": [{"s3": {"object": {
        "key": "extractions/<User_Folder>/<date>/<session_base>.json"}}}]}
    S3 event notifications encode spaces as '+' and other special chars as
    %XX -- the key is ALWAYS unquote_plus'd before use.

Environment Variables:
    S3_BUCKET     - S3 bucket name (the data lake -- IngestBucketName)
    CONFIG_KEY    - S3 key for user/site mapping (default: config/user_mapping.json,
                    read indirectly via lambda_ingest.load_mapping's own env var)
    COMPANY_NAME  - default: FieldSight (mirrors lambda_ingest's default)
    PG*/DATABASE_URL - read by db.connection.get_connection()
"""
import json
import logging
import os
import re
from urllib.parse import unquote_plus

import boto3

import lambda_ingest
import match_request
from db.connection import get_connection
from photo_binding import PHOTOS_PER_TOPIC_CAP  # noqa: F401  (re-export)
from photo_binding import list_pictures as _pb_list_pictures
from photo_binding import photos_for_topics as _photos_for_topics
from repositories import companies, findings, recordings, topics

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
CONFIG_KEY = os.environ.get("CONFIG_KEY", "config/user_mapping.json")
COMPANY_NAME = os.environ.get("COMPANY_NAME", "FieldSight")

EXTRACTIONS_PREFIX = "extractions/"
# Depth-exact: extractions/{user_folder}/{date}/{name}.json -- a key nested
# any deeper (or shallower, or not ending in .json) is not this contract's
# shape and must be skipped rather than guessed at.
EXTRACTION_KEY_RE = re.compile(r"^extractions/([^/]+)/([^/]+)/([^/]+)\.json$")

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _parse_extraction_key(key):
    m = EXTRACTION_KEY_RE.match(key)
    if not m:
        return None
    user_folder, date, session_base = m.group(1), m.group(2), m.group(3)
    return user_folder, date, session_base


# ----------------------------------------------------------
# Task 3 (authority-flip plan) -- time-correlated photo attach.
#
# P2 (2026-07-23 prod-media-binding plan): the matcher and the pictures
# lister now live in photo_binding, shared with lambda_ingest's report path
# (P4) -- a direct import of THIS module from lambda_ingest would be
# circular, since this module imports lambda_ingest for the identity
# bridge. The rule also changed there: strict containment against the
# topic's time_range stranded every prod photo by 1-2 minutes
# (topic_photos: 0 rows across all of prod history). 2026-07-24 correction:
# binding is bounded-tolerance (inside the window, or within
# PHOTO_TOLERANCE_MIN=2 min of an edge; beyond that, no binding at all --
# the never-orphan fallback was removed) -- see photo_binding's docstring.
# The aliases below keep the historical private names importable for
# existing callers and tests.
# ----------------------------------------------------------

def _list_pictures(prefix):
    """Pictures listing bound to THIS module's S3 client + bucket (the
    shared lister is client-parameterized so lambda_ingest can reuse it)."""
    return _pb_list_pictures(s3(), S3_BUCKET, prefix)


# ----------------------------------------------------------
# Per-extraction write (commit-per-extraction: one `with get_connection()` here)
# ----------------------------------------------------------
def write_extraction_items(date, user_folder, extraction_key):
    raw = s3().get_object(Bucket=S3_BUCKET, Key=extraction_key)["Body"].read()
    extraction = json.loads(raw.decode("utf-8"))

    with get_connection() as conn:
        # I-3: serialize concurrent writers on this extraction key. Delete-
        # then-insert is not concurrency-safe on its own (two overlapping
        # invocations for the same key could interleave their delete/insert
        # pairs), and upsert_topic is INSERT-only (no ON CONFLICT dedup) --
        # an xact-scoped advisory lock keyed on the extraction key forces
        # concurrent writers for the SAME key to run one at a time.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (extraction_key,))

        # I-4: Fargate next-evening catch-up downloads can produce a session
        # extraction that lands AFTER that day's nightly report has already
        # been ingested. Without this guard a late-landing extraction would
        # re-insert topics with no future supersession ever coming --
        # permanently-dangling live rows alongside the authoritative report.
        # Post authority-flip (Task 7, spec §6): once AUTHORITY_FLIP defers
        # for a day, lambda_ingest stops writing report topics for it, so
        # report topics only exist for zero-extraction fallback days; this
        # guard keeps that rare day duplicate-free.
        report_source_key = f"reports/{date}/{user_folder}/daily_report.json"
        report_already_ingested = conn.execute(
            "SELECT 1 FROM topics WHERE source_s3_key=%s LIMIT 1",
            (report_source_key,),
        ).fetchone()
        if report_already_ingested is not None:
            reason = "nightly report already ingested — late session extraction superseded"
            logger.info("%s: %s", extraction_key, reason)
            return {"skipped": True, "reason": reason}

        company = lambda_ingest.resolve_company(conn, user_folder)
        if company is None:
            # Same guard + message as lambda_ingest.ingest_report (Fable
            # minor 6): an unseeded org DB would otherwise surface as an
            # opaque 'NoneType' subscript error on every extraction.
            raise RuntimeError(
                f"org company {COMPANY_NAME!r} not found — run the org seed "
                "(fieldsight-*-org-seed) before ingesting")

        # G5b: the app stamps the in-app project pick onto recordings.site_id.
        # That explicit tag is authoritative over the recorder's membership
        # (and is the ONLY way an admin-account recording -- resolve_site returns
        # None for ALL scope -- attributes to a site). Fall through to the legacy
        # membership resolver only when there is no matching, company-valid tag.
        session_base = _parse_extraction_key(extraction_key)[2]
        site = recordings.site_for_media(conn, company["id"], user_folder, date, session_base) \
            or lambda_ingest.resolve_site(conn, company["id"], {}, user_folder)
        if site is None:
            reason = (f"identity bridge miss: user_folder={user_folder!r} -- "
                      f"skipping extraction, zero writes")
            logger.warning("%s: %s", extraction_key, reason)
            return {"skipped": True, "reason": reason}

        user_id = lambda_ingest.resolve_user(conn, company["id"], user_folder)

        # Source-key idempotency (Phase 4a pattern): clear this extraction's
        # prior rows before re-inserting.
        topics.delete_topics_for_source(conn, extraction_key)

        # Task 3 (authority-flip plan) -- list the pictures prefix ONCE per
        # invocation (paginator, outside the per-topic loop below), then
        # pure-match photos to topics by time_range before the loop uses it.
        pictures_prefix = f"users/{user_folder}/pictures/{date}/"
        photo_objects = _list_pictures(pictures_prefix)
        extraction_topics = extraction.get("topics", [])
        photos_by_topic = _photos_for_topics(photo_objects, extraction_topics)

        topics_n = 0
        collected_topics = []
        for i, t in enumerate(extraction_topics):
            mapped_action_items = lambda_ingest._map_action_items(t.get("action_items"))
            matched_photos = photos_by_topic.get(i, [])
            # Sanitize work_class/work_confidence before the upsert (Fable
            # review #7): the columns carry CHECK constraints (work_class IN
            # ('work','non_work'); work_confidence is real) so a raw bad LLM
            # value (e.g. "personal", or a non-numeric confidence) would
            # raise inside this transaction and abort the whole session's
            # topics/findings write. Invalid -> NULL (legacy/unclassified,
            # which enforcement treats as work).
            _wc = t.get("work_class")
            _wc = _wc if _wc in ("work", "non_work") else None
            try:
                _wconf = float(t["work_confidence"]) if t.get("work_confidence") is not None else None
            except (TypeError, ValueError):
                _wconf = None
            row = topics.upsert_topic(
                conn, site["id"], date, t.get("topic_title", ""),
                user_id=user_id, source_s3_key=extraction_key,
                category=t.get("category"), summary=t.get("summary"),
                action_items=mapped_action_items,
                # Phase F Task 23 (D8 retirement, spec §8): no `safety=` kwarg
                # here anymore -- findings.insert_findings below is now the
                # ONLY Aurora write for this topic's safety data, so
                # upsert_topic's own safety_observations INSERT loop never
                # fires. t['safety_flags'] (still derived by lambda_extract_
                # session._derive_safety_flags) is intentionally left
                # untouched in the extraction JSON -- chunking.py and
                # lambda_ask_agent.py still read it for RAG embedding text;
                # only this Aurora dual-write is stopped. safety_observations
                # the TABLE stays in place, unread by this writer, for
                # rollback.
                time_range=t.get("time_range"), participants=t.get("participants"),
                work_class=_wc, work_confidence=_wconf, is_mixed=(t.get("is_mixed") is True),
                photos=[{"s3_key": p["key"], "caption_text": None} for p in matched_photos],
            )
            # Task 2 (programme-impact-link plan) -- persist this topic's
            # rich extraction findings in the SAME transaction as the topic
            # upsert (inherits the I-3 advisory lock + I-4 supersession
            # guard already established above). Legacy extraction JSON with
            # no 'findings' key (pre-#46 extractions still in S3, and the
            # report/ingest path which never has findings) -> t.get(...) or
            # [] -> insert_findings returns [] -> zero rows, zero crash.
            finding_rows = findings.insert_findings(
                conn, row["id"], site["id"], t.get("findings") or [])

            # Snapshot for the match_requests/ artifact (Task 4) -- the
            # non-VPC MatcherFunction reads this, never Aurora directly, so
            # every field it needs (the durable topic id + the same
            # title/summary/action-item text just written) is captured here.
            # The durable finding uuids are what the impact matcher (Task 4)
            # will match against and the suggestion-writer (Task 3) will
            # UPDATE by.
            collected_topics.append({
                "topic_id": str(row["id"]),
                "title": t.get("topic_title", ""),
                "summary": t.get("summary"),
                "user_id": str(user_id) if user_id is not None else None,
                "action_items": [{"text": a["text"]} for a in mapped_action_items],
                "findings": [{
                    "finding_id": str(f["id"]),
                    "observation": f["observation"],
                    "domain": f["domain"],
                    "severity": f["severity"],
                    "entity_name": f["entity_name"],
                    "entity_trade": f["entity_trade"],
                } for f in finding_rows],
            })
            topics_n += 1

    logger.info("item-writer wrote extraction=%s topics=%d", extraction_key, topics_n)

    # AFTER the connection block commits -- the topics referenced in the
    # artifact must be durable before the matcher can act on them. Only
    # emit when something was actually written (mirrors the zero-write
    # skip above); an empty extraction's zero topics never reaches here
    # anyway since collected_topics would be empty.
    if collected_topics:
        match_request.emit(s3(), S3_BUCKET, site["id"], date, extraction_key, collected_topics)

    return {"skipped": False, "topics": topics_n}


# ----------------------------------------------------------
# Entry point — S3 event
# ----------------------------------------------------------
def lambda_handler(event, context):
    event = event or {}
    results = []
    for record in event.get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])
        parsed = _parse_extraction_key(key)
        if parsed is None:
            logger.warning("skipping non-extraction S3 key: %s", key)
            continue
        user_folder, date, _session_base = parsed
        results.append(write_extraction_items(date, user_folder, key))
    return {"results": results}
