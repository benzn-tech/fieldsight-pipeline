"""
Lambda: fieldsight-rag-search v1.0 — Phase 5 RAG retrieval (in-VPC)

Invoked directly (not HTTP-routed) by AskAgentFunction as the retrieval hop
of the two-hop Ask flow (see docs/superpowers/plans/2026-07-07-phase-5-rag-ask.md):

    UI POST /api/ask -> ApiFunction (non-VPC, adds caller_sub)
      -> invoke AskAgentFunction (non-VPC): dashscope_utils.embed(question)
          -> invoke RagSearchFunction (this file, in-VPC): ACL -> search_chunks
          -> claude_utils.call_claude synthesizes answer + citations

CRITICAL: this lambda NEVER embeds text and NEVER calls Claude/DashScope.
It runs in-VPC with no NAT / no internet egress (BUG-36) — it only accepts
an already-computed query_embedding and searches Aurora/pgvector with it.

Event:  {"sub": "<cognito sub>", "query_embedding": [1024 floats], "k": 8}
Result: {"chunks": [...], "site_count": N}
        or, on a soft failure, {"chunks": [], "error": "..."} — this
        function never raises so ask-agent can degrade gracefully instead
        of surfacing a 500 to the UI.

ACL mirrors lambda_org_api.list_live_items EXACTLY: resolve_scope(caller's
global_role) == "ALL" (admin/gm) sees every site in their company; anyone
else is narrowed to memberships.accessible_site_ids. Deny-by-default: an
empty site_ids list short-circuits to an empty result BEFORE calling
search_chunks (WHERE site_id = ANY('{}') would match no rows anyway — this
just skips the DB round-trip and makes the deny-by-default case explicit)
rather than being special-cased in the SQL — same net behavior as the
org-api ACL branch it mirrors.
"""
import json
import logging

from db.connection import get_cached_connection
from repositories import chunks, memberships, sites, users
from repositories.acl import resolve_scope

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    sub = event.get("sub")
    try:
        k = int(event.get("k", 8))
    except (TypeError, ValueError):
        k = 8
    k = max(1, min(k, 32))
    qv = event.get("query_embedding")
    date_from = event.get("date_from") or None
    date_to = event.get("date_to") or None

    if not sub or not qv:
        return {"chunks": [], "error": "missing sub or query_embedding"}

    # Reuse a module-level connection across warm invokes — reconnecting to
    # Aurora cost ~1-2s per call and dominated search latency. Read-only path,
    # so no `with`/transaction (psycopg3's `with conn:` would close it).
    conn = get_cached_connection()
    caller = users.get_user_by_sub(conn, sub)
    if caller is None:
        logger.info("rag-search: caller not provisioned for sub=%s", sub)
        return {"chunks": [], "error": "caller not provisioned"}

    # ACL branch mirrors lambda_org_api.list_live_items exactly.
    if resolve_scope(caller["global_role"]) == "ALL":
        site_ids = [s["id"] for s in sites.list_company_sites(conn, caller["company_id"])]
    else:
        site_ids = memberships.accessible_site_ids(
            conn, caller["id"], caller["global_role"])

    if not site_ids:
        return {"chunks": [], "site_count": 0}

    rows = chunks.search_chunks(conn, qv, site_ids, k=k,
                                date_from=date_from, date_to=date_to)
    # search_chunks returns raw psycopg rows: id/site_id/topic_id are uuid.UUID
    # and report_date is datetime.date — Lambda's JSON marshaller can't
    # serialize either (Runtime.MarshalError kills the invoke on any non-empty
    # hit). Coerce to plain strings before returning.
    rows = json.loads(json.dumps(rows, default=str))
    return {"chunks": rows, "site_count": len(site_ids)}
