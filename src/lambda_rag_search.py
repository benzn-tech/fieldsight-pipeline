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
empty site_ids list still reaches search_chunks (WHERE site_id = ANY('{}')
matches no rows) rather than being special-cased — same behavior as the
org-api ACL branch it mirrors.
"""
import logging

from db.connection import get_connection
from repositories import chunks, memberships, sites, users
from repositories.acl import resolve_scope

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    sub = event.get("sub")
    k = int(event.get("k", 8))
    qv = event.get("query_embedding")

    if not sub or not qv:
        return {"chunks": [], "error": "missing sub or query_embedding"}

    with get_connection() as conn:
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

        rows = chunks.search_chunks(conn, qv, site_ids, k=k)
        return {"chunks": rows, "site_count": len(site_ids)}
