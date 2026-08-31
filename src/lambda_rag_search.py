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

from db.connection import close_cached_connection, get_cached_connection
from repositories import (aliases, chunks, findings, recordings, redactions, scope,
                         sites, topics, users)
import text_normalize

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """Release the pooled Aurora connection on EVERY exit path.

    The module-level cache exists because reconnecting costs ~1-2s and dominated
    search latency. But Aurora treats any open user connection as activity, so a
    warm container holding one keeps the cluster from ever starting its
    auto-pause countdown (Aurora scale-to-zero, spec 2026-08-11). The handler
    has four returns and can raise; a `finally` is the only way to cover them
    all, and wrapping is cheaper than auditing each one forever.
    """
    try:
        return _search(event, context)
    finally:
        close_cached_connection()


_METRIC_UNITS = {
    "duration": "seconds",
    "count_sessions": "sessions",
    "count_photos": "photos",
    "count_findings_safety": "items",
    "count_findings_quality": "items",
}


def _metric(event):
    """Answer a counting question with a number, from SQL, with no model.

    Ask retrieves text chunks and answers from them. "How long did I record
    yesterday" has no textual answer anywhere -- nobody says the duration in the
    meeting, it is a number in a column -- so `metric_slots` routes it here
    instead, and this path never reaches an LLM. A model asked how long you
    recorded says "about two hours" with the fluency of a fact.

    The ACL is the SAME primitive the chunk path uses, resolved the same way from
    the same `sub`, and the caller does not get to name a folder or a site set.
    `mode` is the only thing the event chooses.

    THREE ZEROS, because "0" has more than one cause and two of them read
    identically to a person:

      nothing_visible     the caller can reach no sites at all. Not an answer
                          about recordings -- an answer about the account.
      no_rows_for_that_day  topics exist for the range and `recordings` rows do
                          not. The RealPTT path never registers rows, days
                          before migration 0009 have none, a lake-fed
                          environment has none; measured at 15.4% of days that
                          carry topics. Saying "you recorded nothing" here is
                          the misleading zero `lambda_org_api` was changed to
                          stop producing.
      nothing_recorded    topics do not exist either. The honest zero.

    A findings metric has its own third: `no_topics` means nothing happened in
    range at all, while a zero with topics present means the day had no item of
    that domain -- "no safety issues" is a real and welcome answer, and it must
    not be delivered in the same words as "nothing reached the system".

    `notes` carries only what is non-zero, so a clean answer stays clean and a
    number that is short arrives with the reason attached.
    """
    sub = event.get("sub")
    metric = event.get("metric")
    date_from = event.get("date_from") or None
    date_to = event.get("date_to") or date_from
    out = {"metric": metric, "from": date_from, "to": date_to}

    if metric not in _METRIC_UNITS:
        # Refused, never guessed. Falling through to the nearest metric would
        # answer a different question than the one asked, with a number.
        return dict(out, error="unknown metric")
    if not sub or not date_from:
        return dict(out, error="missing sub or date range")
    out["unit"] = _METRIC_UNITS[metric]

    conn = get_cached_connection()
    caller = users.get_user_by_sub(conn, sub)
    if caller is None:
        logger.info("rag-search metric: caller not provisioned for sub=%s", sub)
        return dict(out, error="caller not provisioned")

    sc = scope.visible_scope(conn, caller)
    site_ids = [str(s) for s in sc["site_ids"]]
    author_ids = ([str(a) for a in sc["author_ids"]]
                  if sc["author_ids"] is not None else None)
    out["scope"] = {"sites": len(site_ids),
                    "authors": len(author_ids) if author_ids is not None else None}

    if not site_ids:
        return dict(out, value=0, n=0, notes={"zero_kind": "nothing_visible"})

    # None means "no company restriction", and this is the ONLY place allowed to
    # ask for it. A cross-company caller's reachable sites belong to other
    # companies, so pinning their own company contradicts the site set and
    # matches nothing -- a platform_admin reaching five sites that recorded all
    # day was told nothing was recorded. Every other caller keeps the pin, which
    # is belt-and-braces over a site set that is already theirs.
    company = None if sc.get("cross_company") else caller["company_id"]

    notes = {}
    if metric.startswith("count_findings_"):
        domain = metric.rsplit("_", 1)[1]
        got = findings.count_by_domain(conn, company, domain,
                                       date_from, date_to,
                                       site_ids=site_ids, author_ids=author_ids)
        value = got["count"]
        for k in ("unlabelled", "null_author", "from_fallback"):
            if got[k]:
                notes[k] = got[k]
        if value == 0:
            notes["zero_kind"] = ("none_in_domain"
                                  if topics.has_topics_in_range(
                                      conn, site_ids, date_from, date_to,
                                      author_ids=author_ids)
                                  else "no_topics")
    else:
        # Tombstones come from Aurora, which this function is already connected
        # to and which the mirror is a copy OF. rag-search is in the VPC with no
        # egress, so an S3 read here would not fail -- it would black-hole until
        # the function timed out (BUG-36) and look like a slow query.
        deleted = redactions.deleted_session_bases(conn, caller["company_id"],
                                                   date_from, date_to)
        got = recordings.range_stats(conn, company, date_from, date_to,
                                     site_ids, author_ids=author_ids,
                                     deleted_bases=deleted)
        value = {"duration": got["duration_s"],
                 "count_sessions": got["sessions"],
                 "count_photos": got["photos"]}[metric]
        for k in ("unmeasured", "unattributed"):
            if got[k]:
                notes[k] = got[k]
        if value == 0:
            # WHICH zero. A photo count of 0 on a day that HAS recordings is an
            # honest "you took no photos", and answering it with "no recording
            # data was registered" is a claim about the pipeline made from a
            # fact about photos. So the row-level zeros are only reachable when
            # the range produced no rows AT ALL -- measured on TEST, where the
            # first run of this route said exactly the wrong one.
            any_rows = got["sessions"] > 0 or got["photos"] > 0
            if any_rows:
                notes["zero_kind"] = "none_of_that_kind"
            elif author_ids is not None:
                # A FOURTH CAUSE, found by asking a real question as a real
                # site_manager on TEST. The rows for that day exist and belong to
                # someone outside this caller's author set, so the range looks
                # empty from here -- and the sentence for the row-level zeros
                # says "no recording data was registered", which is a claim about
                # the pipeline made from a fact about the ACL. The same shape as
                # the photo zero that said the length could not be measured.
                #
                # It does NOT say rows exist elsewhere: SELF+WORKERS exists so a
                # site_manager cannot learn about another site_manager's data,
                # and "there are recordings but not yours" discloses exactly
                # that. The wording just stops speaking for the pipeline and
                # speaks about the caller's own data instead.
                notes["zero_kind"] = "nothing_of_yours"
            else:
                notes["zero_kind"] = ("no_rows_for_that_day"
                                      if topics.has_topics_in_range(
                                          conn, site_ids, date_from, date_to,
                                          author_ids=author_ids)
                                      else "nothing_recorded")

    return dict(out, value=value, n=value, notes=notes)


def _search(event, context):
    # The metric route answers with a number instead of chunks. Placed first
    # because everything below it is about embeddings, and a metric question
    # carries none.
    if event.get("mode") == "metric":
        return _metric(event)

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
    # Opt-in, and Ask is the only caller that opts in. Search (mode=search)
    # sends the dates a person picked in the UI and wants exactly those days --
    # widening a filtered LIST would show rows outside the filter they set.
    widen = bool(event.get("widen_when_empty"))

    # What the answer will be based on. Carried on EVERY return below, empty
    # search included: a caller reading `result.get("basis")` and finding None
    # cannot tell "this search matched nothing" from "this deploy predates the
    # field", and ask-agent's fallback in that case is to report the range it
    # ASKED for as the range actually used.
    #
    # Named `basis` and not `scope` deliberately. `scope` already means two
    # other things on this path -- the request's report/transcript/both, and
    # `repositories.scope`, the ACL primitive imported in this very file.
    basis = {"from": date_from, "to": date_to, "widened": False}

    if not sub or not qv:
        return {"chunks": [], "error": "missing sub or query_embedding", "basis": basis}

    # Reuse a module-level connection across warm invokes — reconnecting to
    # Aurora cost ~1-2s per call and dominated search latency. Read-only path,
    # so no `with`/transaction (psycopg3's `with conn:` would close it).
    conn = get_cached_connection()
    caller = users.get_user_by_sub(conn, sub)
    if caller is None:
        logger.info("rag-search: caller not provisioned for sub=%s", sub)
        return {"chunks": [], "error": "caller not provisioned", "basis": basis}

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
        return {"chunks": [], "site_count": 0, "basis": basis}

    rows = chunks.search_chunks(conn, qv, site_ids, k=k, author_ids=author_ids,
                                date_from=date_from, date_to=date_to)

    # "Nothing yesterday" must not become an empty answer. Retry on the nearest
    # day this caller can actually see, at or BEFORE the range they asked
    # about: widening forward would answer a question about last week with
    # something recorded after it. The nearest day is found under the search's
    # own visibility rules -- a deleted recording must not disclose its date by
    # being the day the answer widens onto.
    #
    # Logged either way. "It ran and there was nowhere to widen to" and "it
    # never ran" are the same observation otherwise, which is how a whole
    # feature stays broken unnoticed here.
    if widen and not rows and (date_from or date_to):
        latest = chunks.latest_visible_date(
            conn, site_ids, author_ids=author_ids, on_or_before=date_to or date_from)
        logger.info("rag-search: %s..%s empty; nearest visible day is %s",
                    date_from, date_to, latest)
        if latest:
            widened_rows = chunks.search_chunks(conn, qv, site_ids, k=k,
                                                author_ids=author_ids,
                                                date_from=latest, date_to=latest)
            # Claimed ONLY if the retry actually returned something. Setting it
            # unconditionally reports "based on 2026-07-18" over zero excerpts --
            # a statement about a day nothing was read from. It cannot happen
            # today only because `build_search_sql` has no distance threshold, so
            # a date with rows always yields rows; the other search path has one
            # (0.55), and the day this one gains it, an unconditional flag would
            # start lying with nothing to catch it.
            if widened_rows:
                rows = widened_rows
                basis = {"from": latest, "to": latest, "widened": True}
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
    return {"chunks": rows, "site_count": len(site_ids), "basis": basis}
