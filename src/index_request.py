"""
Index request builder for the session search index.

item-writer (live path), ingest apply / backfill and the org-api edit path all
need to say the same thing -- "this session's topics changed, re-index it" --
so they share one builder and one emitter. The request is the S3 artifact the
non-VPC embed-report lambda reads; it carries the LIVE topics in the exact
shape `session_index.build_chunks` consumes, plus the session members whose
turns the embedder reads from the lake.

Same channel and placement rule as match_request: built from a short read
connection AFTER the writer's own transaction commits, so every topic id in
the request is durable, and written with lake_put so the object is attributed
to the owning company.

Request key: index_requests/{date}/{folder}/{base}.json -- deterministic, so
re-processing a session overwrites its request. Vectors land beside it under
index_vectors/.
"""
import json
import logging
import os
import time

from psycopg.rows import dict_row

from deleted_predicates import visible_topics_predicate
from lake_put import lake_put
from repositories import redactions as redactions_repo
from repositories import topics as topics_repo
from session_scope import parse_extraction_key

logger = logging.getLogger()

REQUEST_PREFIX = "index_requests/"
VECTORS_PREFIX = "index_vectors/"


def request_key(date, folder, base):
    return f"{REQUEST_PREFIX}{date}/{folder}/{base}.json"


def vectors_key(date, folder, base):
    return f"{VECTORS_PREFIX}{date}/{folder}/{base}.json"


def live_topic_ids(conn, extraction_key):
    """Ids of the topics this extraction key currently owns that a reader may see
    (not deleted, not superseded)."""
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT t.id FROM topics t WHERE t.source_s3_key = %s "
        f"AND {visible_topics_predicate('t')}",
        (extraction_key,),
    ).fetchall()
    return [str(r["id"]) for r in rows]


def _decision(d):
    # topics.decisions is jsonb holding the model's objects; tolerate a bare string.
    if isinstance(d, dict):
        return {"decision": d.get("decision") or "", "rationale": d.get("rationale") or ""}
    return {"decision": str(d), "rationale": ""}


def _question(q):
    if isinstance(q, dict):
        return {"question": q.get("question") or ""}
    return {"question": str(q)}


def _action(a):
    deadline = a.get("deadline_text") or a.get("deadline")
    return {
        "action": a.get("text") or "",
        "responsible": a.get("responsible"),
        "deadline": str(deadline) if deadline else None,
    }


def _topic(full):
    """A topics.get_topic_full() row -> the topic shape session_index.build_chunks reads.
    decisions / open_questions are jsonb columns on the topic row itself, so they ride
    along in get_topic_full; only action_items and findings are child tables."""
    return {
        "id": str(full["id"]),
        "title": full.get("title"),
        "summary": full.get("summary"),
        "time_range": full.get("time_range"),
        "category": full.get("category"),
        "participants": list(full.get("participants") or []),
        "work_class": full.get("work_class"),
        "decisions": [_decision(d) for d in full.get("decisions") or []],
        "questions": [_question(q) for q in full.get("open_questions") or []],
        "action_items": [_action(a) for a in full.get("action_items") or []],
        "findings": [
            {"observation": f.get("observation") or "", "severity": f.get("severity")}
            for f in full.get("findings") or []
        ],
    }


def _members(user_folder, report_date, session_base, replaces):
    """One entry per session whose turns feed this request. A group request reads the
    turns of every merged member (named by the member extraction keys it replaces);
    a solo request reads its own."""
    if session_base.startswith("grp"):
        # A group key is never a session of its own: its turns live in its members. With no
        # parseable members the embedder resolves them from the extraction itself.
        out = []
        for key in replaces:
            parsed = parse_extraction_key(key)
            if parsed:
                folder, date, base = parsed
                out.append({"user_folder": folder, "date": date, "session_base": base})
        if not out:
            logger.warning("group %s has no parseable members (replaces=%r); "
                           "the embedder must resolve them", session_base, list(replaces))
        return out
    return [{"user_folder": user_folder, "date": report_date, "session_base": session_base}]


def build(conn, extraction_key, *, site_id, user_id, owner_company_id, report_date,
          user_folder, session_base, tier, extracted_at, member_bases=(), replaces=()):
    topics = []
    ids = live_topic_ids(conn, extraction_key)
    # visible_topics_predicate drops only scope='deleted' redactions. A topic with ANY
    # other active redaction (scope 'analysis' / 'all') is still live here, so ship it as
    # non_work: session_index then drops its topic chunk and its span from every window.
    redacted = set(map(str, redactions_repo.list_active_for_topics(conn, ids))) if ids else set()
    for tid in ids:
        full = topics_repo.get_topic_full(conn, tid)
        if full is not None:
            t = _topic(full)
            if str(tid) in redacted:
                t["work_class"] = "non_work"
            topics.append(t)
    return {
        "extraction_key": extraction_key,
        "site_id": str(site_id),
        "user_id": str(user_id) if user_id else None,
        "owner_company_id": str(owner_company_id),
        "report_date": str(report_date),
        "user_folder": user_folder,
        "session_base": session_base,
        "tier": tier,
        "extracted_at": extracted_at,
        "member_bases": list(member_bases),
        "replaces": list(replaces),
        "members": _members(user_folder, report_date, session_base, replaces),
        "topics": topics,
    }


def emit(s3_client, bucket, request):
    """Write the request for the embedder. Returns the S3 key."""
    key = request_key(request["report_date"], request["user_folder"], request["session_base"])
    lake_put(
        bucket,
        key,
        json.dumps(request),
        client=s3_client,
        owner_hint={
            "folder": request["user_folder"],
            "session_base": request["session_base"],
            "company_id": request["owner_company_id"],
        },
        ContentType="application/json",
    )
    return key


def emit_failed_metric(extraction_key, metric="SearchIndexRequestFailed"):
    """One EMF line for a request that could not be built or written (or, with `metric`,
    a vectors object that could not be applied). Printed, not logged (EMF needs bare
    JSON). Never raises."""
    try:
        print(json.dumps({
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "FieldSight",
                    "Dimensions": [["Stage"]],
                    "Metrics": [{"Name": metric, "Unit": "Count"}],
                }],
            },
            "Stage": os.environ.get("STAGE", "unknown"),
            metric: 1,
            "extraction_key": extraction_key,
        }))
    except Exception:
        logger.warning("could not emit the %s metric for %s", metric, extraction_key)
