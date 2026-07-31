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

ACL: ALWAYS routes through repositories/scope.visible_scope, the SAME
primitive the dashboard uses, so Search/Ask apply the identical per-author
tier (e.g. site_manager -> SELF+WORKERS, never other site managers/PM/GM)
on top of the site-level reach. There is no legacy/fallback mode — this is
a new consumer with no pre-graded behavior to preserve, so it always fails
SAFE (narrowest scope) rather than failing open to site-only. Deny-by-default:
an empty site_ids list short-circuits to an empty result BEFORE calling
search_chunks (WHERE site_id = ANY('{}') would match no rows anyway — this
just skips the DB round-trip and makes the deny-by-default case explicit).
"""
import json
import logging

from db.connection import get_cached_connection
from repositories import aliases, chunks, scope, sites, users
import text_normalize

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
    site_filter = event.get("site") or None  # scope search to ONE project (within ACL)

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

    # Fail-safe: ALWAYS scope through the dashboard's ACL primitive (per-author
    # graded visibility). No GRADED_ROLES gate here — rag-search is a new
    # consumer with no legacy behavior to preserve, and failing open to
    # site-only would over-share. org-api keeps its own GRADED_ROLES gate.
    sc = scope.visible_scope(conn, caller)
    site_ids = [str(s) for s in sc["site_ids"]]
    author_ids = ([str(a) for a in sc["author_ids"]]
                  if sc["author_ids"] is not None else None)

    # Project-scoped search: `site_filter` is normally the site UUID (what the
    # UI's top-bar selector actually sends), so check it against the caller's
    # already-resolved accessible set first; only fall back to the legacy
    # project-SLUG lookup when it isn't a UUID already in reach. Either way
    # this narrows within the caller's accessible sites (deny-by-default — an
    # unknown or inaccessible id/slug yields []). Ask never passes site (stays
    # cross-project).
    if site_filter:
        sset = {str(s) for s in site_ids}
        if str(site_filter) in sset:                  # UUID already in reach
            site_ids = [str(site_filter)]
        else:                                         # legacy: treat as project slug
            matched = sites.get_company_site_by_slug(conn, caller["company_id"], site_filter)
            matched_id = matched["id"] if matched else None
            site_ids = [s for s in site_ids if str(s) == str(matched_id)]

    if not site_ids:
        return {"chunks": [], "site_count": 0}

    rows = chunks.search_chunks(conn, qv, site_ids, k=k, author_ids=author_ids,
                                date_from=date_from, date_to=date_to)
    # Synthesis-time safety net (spec §4): normalize retrieved chunk text with
    # the company's active aliases, so a chunk not yet re-embedded still reads
    # corrected before the LLM. site_ids here are the caller's accessible sites.
    active = aliases.list_active(conn, caller["company_id"], site_ids=[str(s) for s in site_ids])
    alias_pairs = [{"wrong_term": a["wrong_term"], "right_term": a["right_term"]}
                   for a in active]
    if alias_pairs:
        for r in rows:
            if r.get("chunk_text"):
                r["chunk_text"] = text_normalize.normalize(r["chunk_text"], alias_pairs)

    # search_chunks returns raw psycopg rows: id/site_id/topic_id are uuid.UUID
    # and report_date is datetime.date -- Lambda's JSON marshaller can't
    # serialize either. Coerce to plain strings before returning.
    rows = json.loads(json.dumps(rows, default=str))
    return {"chunks": rows, "site_count": len(site_ids)}
