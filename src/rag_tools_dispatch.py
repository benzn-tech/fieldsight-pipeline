"""rag-search `mode: "tools"`: run a batch of Ask tool calls (spec
2026-10-15-ask-retrieval-tools §3.3/§3.4).

In-VPC, one connection, the ACL resolved ONCE by the handler and passed in as
`sc`. Calls run sequentially; every call is isolated, so one failure is
`meta.error="upstream"` for that call and never loses the rest. A call that
cannot start before `deadline_s` returns `meta.error="timeout"` without running.

Args arrive already validated and normalized by `ask_tools.validate_call`
(ask-agent side). Out-of-ACL `site`/`person` values resolve to "no match" --
never to an error that would confirm they exist.

Logs carry tool names, counts, timings and `type(exc).__name__` -- never query,
argument or row text.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone

import ask_tools_log
import project_doc_flag
import text_normalize
from lexical_terms import subject_terms
from repositories import aliases, chunks, sites, topics, users

logger = logging.getLogger()

MAX_CALLS = 4
# Spec §3.4: RagSearchFunction Timeout is 23 s; a batch never plans past 20.
MAX_DEADLINE_S = 20.0
# day_overview_rows fetches DAY_LIMIT + 1 on purpose; the extra row is the
# truncation signal.
DAY_LIMIT = 60
DEFAULT_K = 8
# latest_activity(query): the subject words chose the day; search that day this
# wide so a chunk naming them ranked below k*2 is not lost to the coverage filter
# (_search's own ceiling is 64).
LATEST_DAY_SEARCH_K = 64
# latest_activity(query) runs a day query (served by the GIN text index) AND a full
# search of the chosen day. The deadline governs call START only, so one started
# late can outrun RagSearchFunction's 23 s Timeout -- and a Lambda timeout loses
# the WHOLE batch, not one call. The guard now covers the day search, which costs
# as much as any search. Refuse to start it with less than this many seconds left
# (final review M8).
LATEST_QUERY_MIN_START_S = 5.0
# A search never fetches more than this (rag-search's own list clamp).
MAX_SEARCH_K = 64
_TOPIC_TEXT = ("title", "summary")
# _search already normalizes chunk_text; normalizing it again is not idempotent
# when a right_term contains its wrong_term ("Jon" -> "Jon Smith").
_CHUNK_EXTRA_TEXT = ("topic_title", "topic_summary")


class _NoRows(Exception):
    """Resolution failed in a way the meta already explains: return no rows."""


def _day_before(iso):
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()


def _day_after(iso):
    return (date.fromisoformat(iso) + timedelta(days=1)).isoformat()


def _open_upper_bound():
    """Default `on_or_before` when the caller sent none.

    The Lambda clock is UTC and NZ is up to 14 h ahead, so UTC "today" is often
    NZ yesterday -- anchoring on it would hide the caller's own today (BUG-37).
    Nothing is recorded in the future, so UTC today + 1 day is a bound that
    never excludes a local today and costs nothing. The orchestrator should
    still send the caller's local today (ask_tools injects it)."""
    return (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def _normalize_rows(conn, caller, sc, rows, fields):
    """The alias rewriting `_search` applies, on the given text fields."""
    if not rows:
        return rows
    active = aliases.list_active(conn, caller["company_id"],
                                 site_ids=[str(s) for s in sc["site_ids"]])
    pairs = [{"wrong_term": a["wrong_term"], "right_term": a["right_term"]} for a in active]
    if pairs:
        for r in rows:
            for f in fields:
                if r.get(f):
                    r[f] = text_normalize.normalize(r[f], pairs)
    return rows


def _empty_reason(sc):
    return "nothing_visible" if not sc["site_ids"] else "nothing_in_range"


def _no_match(sc):
    return "no_match" if sc["site_ids"] else "nothing_visible"


def _resolve_site(conn, sc, value, meta):
    """Site UUID or case-insensitive name, ONLY within sc["site_ids"]."""
    if value is None:
        return None
    ids = {str(s).lower() for s in sc["site_ids"]}
    uid = users._canonical_uuid(value)
    if uid is not None:
        if uid in ids:
            return uid
    else:
        want = " ".join(value.split()).lower()
        hits = [str(s["id"]) for s in sites.list_sites_by_ids(conn, sorted(ids))
                if " ".join((s.get("name") or "").split()).lower() == want]
        # Two reachable sites with one name: picking one would silently narrow
        # to the wrong project, so it is no match like any other unresolved name.
        if len(hits) == 1:
            return hits[0]
    meta["empty_reason"] = _no_match(sc)
    raise _NoRows()


def _resolve_person(conn, caller, sc, value, meta):
    """A person_ref that is re-validated now, or a name that resolves to exactly
    one visible person. Anything else: no rows, and the meta says why."""
    if value is None:
        return None
    uid = users._canonical_uuid(value)
    if uid is not None:
        if users.person_in_reach(conn, sc["site_ids"], sc["author_ids"], uid):
            return uid
        found = []
    else:
        found = users.find_visible_people(conn, caller["company_id"], sc["site_ids"],
                                          sc["author_ids"], value)
        if len(found) == 1:
            return found[0]["person_ref"]
    if found:
        meta["person_ambiguous"] = [{"person_ref": p["person_ref"], "name": p["name"]}
                                    for p in found]
        meta["empty_reason"] = "person_ambiguous"
    else:
        meta["person_unresolved"] = True
        meta["empty_reason"] = "person_unresolved"
    raise _NoRows()


def _search_author_folder(conn, caller, sc, user_id, meta):
    """The folder `_search` must be given to narrow to `user_id`.

    `_search` narrows by folder NAME and resolves it company-pinned first. A
    NULL folder would make it drop the author filter (search everyone); a folder
    it resolves to a DIFFERENT user would search the wrong person. Either way:
    no rows, never a wider search.

    A folder-name collision cannot produce the mismatch: migration 0012 made
    `users.folder_name` globally unique, so both lookups can only return the
    user who owns the folder. The check guards against a future change in
    `_search`'s resolution, not a known gap."""
    u = users.get_by_id(conn, user_id)
    folder = u and u.get("folder_name")
    if folder:
        # Mirror _search's own resolution order exactly.
        if sc.get("cross_company"):
            target = users.get_by_folder_name_global(conn, folder)
        else:
            target = (users.get_by_folder_name(conn, caller["company_id"], folder)
                      or users.get_by_folder_name_global(conn, folder))
        if target and str(target["id"]).lower() == user_id:
            return folder
    meta["person_unresolved"] = True
    meta["empty_reason"] = "person_unresolved"
    raise _NoRows()


def _narrowed(sc, site_id, user_id):
    site_ids = [site_id] if site_id else sorted(str(s) for s in sc["site_ids"])
    author_ids = ([user_id] if user_id else
                  (sorted(str(a) for a in sc["author_ids"])
                   if sc["author_ids"] is not None else None))
    return site_ids, author_ids


def _overview(conn, caller, sc, date_from, date_to, kw, meta):
    rows = topics.day_overview_rows(conn, sc["site_ids"], date_from, date_to,
                                    limit=DAY_LIMIT, **kw)
    meta["truncated"] = len(rows) > DAY_LIMIT
    rows = list(rows[:DAY_LIMIT])
    return _normalize_rows(conn, caller, sc, rows, _TOPIC_TEXT)


def _need_embedding(call, meta):
    """A search-shaped call without an embedding is the orchestrator's contract
    break, not a transient failure: bad_args, so the model is not told to retry."""
    if not call.get("query_embedding"):
        meta.update({"error": "bad_args", "field": "query_embedding"})
        raise _NoRows()


def _candidate_pool(call, args):
    """Rows a search hands ask_tools' post-search: k*2, widened to the orchestrator's
    `candidate_k` (the RERANK_CANDIDATE_K pool `_rag_answer` reranks over), capped
    at MAX_SEARCH_K. A malformed candidate_k is ignored, never an error."""
    base = (args.get("k") or DEFAULT_K) * 2
    ck = call.get("candidate_k")
    if not isinstance(ck, int):          # bool or <= 0 lose to base below
        ck = 0
    return min(MAX_SEARCH_K, max(base, ck))


def _run_search(conn, caller, sc, args, call, search_fn, site_id, user_id, meta,
                search_k=None, author_folder=None):
    _need_embedding(call, meta)
    ev = {"sub": caller["sub"], "query_embedding": call.get("query_embedding"),
          "question": args["query"], "k": search_k or _candidate_pool(call, args)}
    if args.get("date_from"):
        ev["date_from"], ev["date_to"] = args["date_from"], args.get("date_to") or args["date_from"]
    if site_id:
        ev["site"] = site_id
    if user_id:
        ev["author"] = author_folder or _search_author_folder(conn, caller, sc, user_id, meta)
    res = search_fn(ev, None) or {}
    if res.get("error"):
        meta["error"] = ("not_provisioned" if "provision" in str(res["error"])
                         else "upstream")
        raise _NoRows()
    rows = list(res.get("chunks") or [])
    return _normalize_rows(conn, caller, sc, rows, _CHUNK_EXTRA_TEXT)


def _day_overview(conn, caller, sc, a, meta, **_):
    site_id = _resolve_site(conn, sc, a.get("site"), meta)
    user_id = _resolve_person(conn, caller, sc, a.get("person"), meta)
    kw = dict(author_ids=sc["author_ids"], site_id=site_id, user_id=user_id)
    rows = _overview(conn, caller, sc, a["date_from"], a.get("date_to") or a["date_from"],
                     kw, meta)
    if not rows:
        meta["empty_reason"] = _empty_reason(sc)
        meta["latest_before"] = {"date": topics.latest_visible_topic_date(
            conn, sc["site_ids"], a["date_from"], **kw), "from": "topics"}
    return rows


def _search_records(conn, caller, sc, a, meta, call, search_fn, source_family, **_):
    site_id = _resolve_site(conn, sc, a.get("site"), meta)
    user_id = _resolve_person(conn, caller, sc, a.get("person"), meta)
    rows = _run_search(conn, caller, sc, a, call, search_fn, site_id, user_id, meta)
    if not rows:
        if a.get("date_from"):
            site_ids, author_ids = _narrowed(sc, site_id, user_id)
            meta["empty_reason"] = _empty_reason(sc)
            meta["latest_before"] = {"date": chunks.latest_visible_date(
                conn, site_ids, author_ids=author_ids,
                on_or_before=_day_before(a["date_from"]),
                source_family=source_family), "from": "chunks"}
        else:
            meta["empty_reason"] = _no_match(sc)
    return rows


def _latest_activity(conn, caller, sc, a, meta, call, search_fn, source_family, **_):
    upto = a.get("on_or_before") or _open_upper_bound()
    if not a.get("query"):
        site_id = _resolve_site(conn, sc, a.get("site"), meta)
        user_id = _resolve_person(conn, caller, sc, a.get("person"), meta)
        kw = dict(author_ids=sc["author_ids"], site_id=site_id, user_id=user_id)
        day = topics.latest_visible_topic_date(conn, sc["site_ids"], _day_after(upto), **kw)
        if not day:
            meta["empty_reason"] = _empty_reason(sc)
            return []
        return _overview(conn, caller, sc, day, day, kw, meta)
    # A missing embedding is the orchestrator's contract break: bad_args, checked
    # before the subject so it is never reported as "no match".
    _need_embedding(call, meta)
    # The day must name the subject (spec 2026-10-20): as many subject words as
    # any chunk names (§8, coverage first -- round 1 sends the raw question), each
    # stemmed or exact. A question with none (only asking words, or purely
    # Chinese) is no_match without any SQL -- the judge falls back to search_records.
    terms = subject_terms(a["query"])
    if not terms:
        meta["empty_reason"] = _no_match(sc)
        return []
    site_id = _resolve_site(conn, sc, a.get("site"), meta)
    user_id = _resolve_person(conn, caller, sc, a.get("person"), meta)
    site_ids, author_ids = _narrowed(sc, site_id, user_id)
    # Fail closed BEFORE the day query if _search could not narrow to this person;
    # the folder resolved here is the one the day's search uses (resolved once).
    folder = _search_author_folder(conn, caller, sc, user_id, meta) if user_id else None
    picked = chunks.latest_matching_date(
        conn, site_ids, on_or_before=upto, subject_terms=terms,
        author_ids=author_ids, source_family=source_family)
    if picked is None:
        meta["empty_reason"] = _no_match(sc)
        return []
    day, k_star = picked
    meta["matched_date"] = day
    # Search that day with the subject words only, not the whole question: the
    # keyword arm takes its top 64 by ts_rank of an OR over the question's terms,
    # and fed "last"/"time"/"talk" a long day's chatter can outrank the one chunk
    # that names the subject (spec 2026-10-20 §3.3). The vector arm still ranks
    # by the question's embedding.
    rows = _run_search(conn, caller, sc,
                       dict(a, query=" ".join(terms), date_from=day, date_to=day), call,
                       search_fn, site_id, user_id, meta, search_k=LATEST_DAY_SEARCH_K,
                       author_folder=folder)
    # Keep the fetched rows at the highest fetched coverage, by the SAME coverage
    # expression that chose the day; a row near only by distance is dropped.
    # `lexical_hit` cannot decide this: it means "found ONLY by the keyword arm",
    # and a small day's matching chunk arrives as its vector copy. The tier is the
    # FETCHED maximum, not k*: the keyword arm matches exact tokens only, so a k*
    # chunk resting on a stem can be missing from the 64 (§8.1 item 2). Below the
    # floor nothing is kept. Then as many as an ordinary search returns for
    # ask_tools' post-search. subject_match is for logs/diagnostics only (§8.1
    # item 5): the orchestrator header does not render it.
    cover = chunks.subject_coverage(conn, [r.get("id") for r in rows], terms)
    k_fetched = max(cover.values(), default=0)
    meta["subject_match"] = {"matched": k_fetched, "of": len(terms), "day_max": k_star}
    if k_fetched < chunks.subject_floor(len(terms)):
        rows = []
    rows = [r for r in rows if cover.get(str(r.get("id")).lower(), 0) >= k_fetched]
    rows = rows[:_candidate_pool(call, a)]
    if not rows:
        meta["empty_reason"] = _no_match(sc)
    return rows


def _find_people(conn, caller, sc, a, meta, **_):
    rows = users.find_visible_people(conn, caller["company_id"], sc["site_ids"],
                                     sc["author_ids"], a["name"])
    if not rows:
        meta["empty_reason"] = _no_match(sc)
    return list(rows)


def _count_metric(conn, caller, sc, a, meta, metric_fn, **_):
    res = metric_fn({"mode": "metric", "sub": caller["sub"], "metric": a["metric"],
                     "date_from": a["date_from"],
                     "date_to": a.get("date_to") or a["date_from"]}) or {}
    if res.get("error"):
        meta["error"] = ("not_provisioned" if "provision" in str(res["error"])
                         else "upstream")
        return []
    zero = (res.get("notes") or {}).get("zero_kind")
    if zero:
        meta["empty_reason"] = zero
    return [res]


def _search_project_documents(conn, caller, sc, a, meta, call, search_fn, **_):
    """Project documents (spec 2026-10-16): through rag-search's own `documents` mode, so
    there is one implementation of document search and its ACL. Off -> bad_args, the same
    answer an unknown tool gets."""
    if not project_doc_flag.on():
        meta.update({"error": "bad_args", "field": "name"})
        raise _NoRows()
    _need_embedding(call, meta)
    site_id = _resolve_site(conn, sc, a.get("site"), meta)
    if not sc["site_ids"]:
        meta["empty_reason"] = "nothing_visible"
        return []
    ev = {"mode": "documents", "sub": caller["sub"], "query_embedding": call["query_embedding"],
          "question": a["query"], "k": a.get("k") or DEFAULT_K}
    if site_id:
        ev["site"] = site_id
    res = search_fn(ev, None) or {}
    if res.get("error"):
        meta["error"] = "not_provisioned" if "provision" in str(res["error"]) else "upstream"
        raise _NoRows()
    rows = list(res.get("documents") or [])
    if not rows:
        meta["empty_reason"] = _no_match(sc)
    return rows


_TOOLS = {"day_overview": _day_overview, "search_records": _search_records,
          "latest_activity": _latest_activity, "find_people": _find_people,
          "count_metric": _count_metric,
          "search_project_documents": _search_project_documents}


def _deadline(event):
    """Seconds this batch may START calls in. Absent -> the 20 s default. 0 means 0:
    no time left, every call times out. Negative, NaN or unparseable -> 0 (fail
    closed: `t0 - start >= nan` is never true, so NaN would mean "no deadline")."""
    raw = event.get("deadline_s")
    if raw is None:
        return MAX_DEADLINE_S
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or v < 0:
        return 0.0
    return min(v, MAX_DEADLINE_S)


# One log-shape helper for both sides of the seam (ask_tools_log, stdlib only).
_sub_hash = ask_tools_log.sub_hash
_arg_shape = ask_tools_log.arg_shape


def run(conn, caller, sc, event, *, search_fn, metric_fn, clock=time.monotonic,
        source_family=None):
    """`{"results": [{"id", "name", "rows", "meta"}]}`, one per call, in order.

    `caller` must carry `sub` (the handler adds it); `sc` is the handler's one
    ACL resolution: {"site_ids": set[str], "author_ids": set[str] | None,
    "company_id", "cross_company"}. `source_family` must be the value `_search`
    passes to search_chunks, so `latest_before` agrees with search_records."""
    calls = event.get("calls") or []
    if len(calls) > MAX_CALLS:
        logger.info("ask tools: batch rejected, %d calls > %d", len(calls), MAX_CALLS)
        return {"results": [{"id": c.get("id") if isinstance(c, dict) else None,
                             "name": c.get("name") if isinstance(c, dict) else None,
                             "rows": [], "meta": {"error": "bad_args", "field": "calls"}}
                            for c in calls]}
    start = clock()
    deadline = _deadline(event)
    who = _sub_hash(caller.get("sub"))
    out = []
    for call in calls:
        call = call if isinstance(call, dict) else {}
        name = call.get("name")
        meta, rows = {}, []
        t0 = clock()
        call_args = call.get("args") if isinstance(call.get("args"), dict) else {}
        if t0 - start >= deadline:
            meta["error"] = "timeout"
        elif name not in _TOOLS:
            meta.update({"error": "bad_args", "field": "name"})
        elif (name == "latest_activity" and call_args.get("query")
              and deadline - (t0 - start) < LATEST_QUERY_MIN_START_S):
            meta["error"] = "timeout"
        else:
            try:
                # One transaction per call: a failed statement rolls back only this
                # call (a SAVEPOINT when already inside a transaction), so the next
                # call never inherits an aborted transaction -- whatever the
                # connection's autocommit mode. Not `with conn:`, which closes it.
                with conn.transaction():
                    rows = _TOOLS[name](conn, caller, sc, call.get("args") or {}, meta,
                                        call=call, search_fn=search_fn,
                                        metric_fn=metric_fn, source_family=source_family)
            except _NoRows:
                rows = []
            except Exception as exc:                       # noqa: BLE001 -- per-call isolation
                logger.warning("ask tool %s failed: %s", name, type(exc).__name__)
                meta, rows = {"error": "upstream"}, []
        meta["ms"] = int((clock() - t0) * 1000)
        fields, days = _arg_shape(call_args)
        logger.info("ask tool %s fields=%s days=%s rows=%d empty=%s err=%s truncated=%s "
                    "ms=%d caller=%s",
                    name if name in _TOOLS else "?", fields, days, len(rows),
                    meta.get("empty_reason"), meta.get("error"), meta.get("truncated"),
                    meta["ms"], who)
        out.append({"id": call.get("id"), "name": name,
                    "rows": json.loads(json.dumps(rows, default=str)), "meta": meta})
    return {"results": out}
