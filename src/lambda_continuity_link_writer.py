"""
Lambda: continuity-link-writer -- hop 3 of cross-day item continuity.

Spec: docs/superpowers/specs/2026-11-09-cross-day-continuity-design.md (X3, X8, X9, X10).

In-VPC (psycopg direct to Aurora). The non-VPC cross-day linker (hop 2) calls the model, runs
`continuity_xday.judge`, then invokes this function RequestResponse. Every write of one
invocation is ONE transaction (`with get_connection() as conn:` commits on a clean exit and
rolls back on an exception). Errors propagate -- nothing here is swallowed -- so hop 2 sees a
FunctionError and fails closed (X10: a failed hop 3 leaves no links and no records).

Payload contract (hop 2 follows it)
-----------------------------------
Event:
  {"request": <the hop-1 request (continuity_xday.build_request + trigger fields) with
               EVERY text field removed: new_items[].text and candidates[].text dropped>,
   "links":   [{"src_stable_id", "src_item_id", "src_kind", "dst_stable_id"}, ...],
               -- judge()'s accepted links, as returned
   "claims":  [{"src_stable_id", "dst_stable_id", "alias", "outcome", "guard"}, ...],
               -- judge()'s out["claims"], as returned (every claim, accepted and rejected)
   "model":   {"provider", "model", "model_version"}   -- optional; provider defaults to
               "unknown" (decision_records.provider is NOT NULL)}
  Read from `request`: site_id, extraction_key, run_hash, question_set, date, and new_items[]
  {stable_id, kind, first_date?, first_seen_recorded_at?} (the src's recording order: its
  own first_date / first_seen_recorded_at when present, else the request's `date`).
  `guard` is any string judge emits (malformed, existence, kind, self, echo_*, one_to_one,
  fuse, ...) or null; it is recorded as given, never interpreted.

Return: {"inserted": n, "skipped": {reason: n}, "records": n}
  inserted -- new `continues` rows; skipped -- links not inserted, by reason; records -- new
  decision_records rows written by this invocation (claims + the run record; a re-delivery
  writes 0).

Skip reasons (X8), per link:
  site_gone  -- the request's site no longer exists (nothing at all is written);
  malformed  -- src_kind is not an item kind, or a stable_id is missing;
  src_gone   -- no live row of src_kind under the request's extraction key on the site
                carries the src stable_id, nor (when src_item_id is non-null) that item_id;
  src_ambiguous -- more than one such row;
  self       -- the resolved src is the dst;
  dst_gone   -- the dst stable_id has no live row of that kind on the request's site;
  time_order -- the dst is not recorded strictly earlier than the src (X5's order,
                continuity_xday.recorded_earlier). The dst's order is RE-READ from the DB --
                its first-seen row's topic report_date, as item_history.find_items derives
                first_date -- never the request's copy and never created_at;
  duplicate  -- the edge already exists (ON CONFLICT DO NOTHING, migration 0099).

The src row is looked up under the request's extraction key only, so the link's FK names a
row of that pass and `delete_topics_for_source(extraction_key)` cascades it. When the src is
resolved through its item_id, the link carries the row's CURRENT stable_id (the one
event_links readers resolve) and so does the claim record's subject_stable_id; the record's
output keeps hop 2's src_stable_id, because that is part of the re-delivery dedupe key.

Records (X9): one `item_continuity_xday` row per claim -- subject = src kind / src
stable_id, object_ref = dst stable_id, question_set, input_key = extraction key,
input_hash = run_hash, output {src_stable_id, dst_stable_id, outcome, guard}, auto_outcome =
outcome; deduped on (kind, input_key, input_hash, object_ref, output->>'src_stable_id'). A
write-time skip does not change the record (it is hop 2's verdict); it is counted in the
return and in the run record. Plus one `item_continuity_xday_run` row per request
("processed, n claims"), deduped on (kind, input_key, input_hash): subject is a live topic of
the extraction key (subject_type 'topic'), else the site (subject_type 'site' -- invisible to
visible_decision_records_predicate, read by kind + input_key). Ids, enums and counts only.

Logs carry counts and keys only, never item text.

Environment Variables:
    PG*/DATABASE_URL - read by db.connection.get_connection()
"""
import logging

import continuity_xday
from db.connection import get_connection
from deleted_predicates import (
    DELETED_SOURCE_PREDICATE,
    DELETED_TOPIC_PREDICATE,
    visible_topics_predicate,
)
from repositories import decision_records, event_links, sites
from repositories.event_links import KIND_TABLE

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RUN_KIND = "item_continuity_xday_run"


def _s(value):
    return None if value is None else str(value)


def _live_src_rows(conn, table, site_id, extraction_key, column, value):
    live = visible_topics_predicate("t")
    return conn.execute(
        f"SELECT c.id, c.stable_id::text FROM {table} c JOIN topics t ON t.id = c.topic_id "
        f"WHERE c.{column} = %s AND c.site_id = %s AND t.source_s3_key = %s AND {live} "
        f"LIMIT 2", (value, site_id, extraction_key)).fetchall()


def _resolve_src(conn, kind, site_id, extraction_key, stable_id, item_id):
    """(row_id, current stable_id) of the src's live row under the extraction key, or a skip
    reason string."""
    table = KIND_TABLE[kind]
    rows = _live_src_rows(conn, table, site_id, extraction_key, "stable_id", stable_id)
    if not rows and item_id is not None:
        rows = _live_src_rows(conn, table, site_id, extraction_key, "item_id", item_id)
    if not rows:
        return "src_gone"
    if len(rows) > 1:
        return "src_ambiguous"
    return rows[0][0], rows[0][1]


def _dst_live(conn, kind, site_id, stable_id):
    live = visible_topics_predicate("t")
    return conn.execute(
        f"SELECT 1 FROM {KIND_TABLE[kind]} c JOIN topics t ON t.id = c.topic_id "
        f"WHERE c.stable_id = %s AND c.site_id = %s AND {live} LIMIT 1",
        (stable_id, site_id)).fetchone() is not None


def _dst_order(conn, kind, site_id, stable_id):
    """The dst's order key `(first_date, first_seen_recorded_at)`: its first-seen row (the
    earliest undeleted row carrying the stable_id on the site, superseded passes included --
    item_history.find_items' `fs` lateral exactly) and that row's topic report_date.

    TODO(Track C): first_seen_recorded_at (meeting_session.opened_at by sid, else the
    transcript-stem timestamp) is not stored yet; hop 1's `_xday_recorded_at` is None for
    the same reason. Until it lands the instant is None and recorded_earlier compares dates
    strictly (a same-date pair is not ordered)."""
    row = conn.execute(
        f"SELECT tx.report_date FROM {KIND_TABLE[kind]} x JOIN topics tx ON tx.id = x.topic_id "
        f"WHERE x.stable_id = %s AND x.site_id = %s "
        f"AND {DELETED_TOPIC_PREDICATE.format(alias='tx')} "
        f"AND {DELETED_SOURCE_PREDICATE.format(alias='tx')} "
        f"ORDER BY x.created_at ASC, x.id ASC LIMIT 1", (stable_id, site_id)).fetchone()
    return continuity_xday.order_key({"first_date": row[0] if row else None})


def _src_order(request, new_item):
    new_item = new_item or {}
    return continuity_xday.order_key({
        "first_date": new_item.get("first_date") or request.get("date"),
        "first_seen_recorded_at": new_item.get("first_seen_recorded_at")})


def _existing_claim_record(conn, key, run_hash, dst, src):
    row = conn.execute(
        "SELECT id FROM decision_records WHERE kind = %s AND input_key = %s "
        "AND input_hash = %s AND object_ref IS NOT DISTINCT FROM %s "
        "AND output->>'src_stable_id' IS NOT DISTINCT FROM %s "
        "ORDER BY created_at, id LIMIT 1",
        (continuity_xday.DECISION_KIND, key, run_hash, dst, src)).fetchone()
    return row[0] if row else None


def _run_subject(conn, site_id, extraction_key):
    row = conn.execute(
        f"SELECT t.id FROM topics t WHERE t.source_s3_key = %s AND t.site_id = %s "
        f"AND {visible_topics_predicate('t')} ORDER BY t.id LIMIT 1",
        (extraction_key, site_id)).fetchone()
    return ("topic", row[0]) if row else ("site", site_id)


def _bump(skipped, reason):
    skipped[reason] = skipped.get(reason, 0) + 1


def _check_link(conn, request, site_id, key, link, new_by_sid):
    """(row_id, current src stable_id) to insert, or a skip reason (spec X8)."""
    kind = link.get("src_kind")
    src, dst = _s(link.get("src_stable_id")), _s(link.get("dst_stable_id"))
    if kind not in KIND_TABLE or not src or not dst:
        return "malformed"
    resolved = _resolve_src(conn, kind, site_id, key, src, _s(link.get("src_item_id")))
    if isinstance(resolved, str):
        return resolved
    if resolved[1] == dst:
        return "self"
    if not _dst_live(conn, kind, site_id, dst):
        return "dst_gone"
    if not continuity_xday.recorded_earlier(_dst_order(conn, kind, site_id, dst),
                                            _src_order(request, new_by_sid.get(src))):
        return "time_order"
    return resolved


def write_links(conn, event):
    """Everything hop 3 does, on `conn` (the caller owns the transaction)."""
    event = event or {}
    request = event.get("request") or {}
    site_id, key, run_hash = (_s(request.get("site_id")), request.get("extraction_key"),
                              request.get("run_hash"))
    if not site_id or not key or not run_hash:
        raise ValueError("request lacks site_id/extraction_key/run_hash")
    links = [l for l in event.get("links") or [] if isinstance(l, dict)]
    claims = [c for c in event.get("claims") or [] if isinstance(c, dict)]
    model = event.get("model") or {}
    provider = model.get("provider") or "unknown"
    question_set = request.get("question_set") or continuity_xday.XDAY_QUESTION_SET

    skipped = {}
    site = sites.get_site(conn, site_id)
    if site is None:
        if links:
            skipped["site_gone"] = len(links)
        logger.warning("xday link writer: site gone key=%s links=%d claims=%d",
                       key, len(links), len(claims))
        return {"inserted": 0, "skipped": skipped, "records": 0}
    company_id = site["company_id"]

    new_by_sid = {_s(n.get("stable_id")): n for n in request.get("new_items") or []
                  if isinstance(n, dict) and n.get("stable_id")}

    def record(kind, subject_type, subject, object_ref, output, auto_outcome):
        return decision_records.insert(
            conn, company_id=company_id, site_id=site_id, kind=kind,
            subject_type=subject_type, subject_stable_id=subject, object_ref=object_ref,
            provider=provider, model=model.get("model"),
            model_version=model.get("model_version"), question_set=question_set,
            input_key=key, input_hash=run_hash, output=output,
            auto_outcome=auto_outcome)["id"]

    # Accepted links by pair; a link without a matching claim is recorded as an accepted claim.
    link_by_pair = {}
    for l in links:
        link_by_pair.setdefault((_s(l.get("src_stable_id")), _s(l.get("dst_stable_id"))), l)
    work = [dict(c, src_stable_id=_s(c.get("src_stable_id")),
                 dst_stable_id=_s(c.get("dst_stable_id"))) for c in claims]
    claimed = {(c["src_stable_id"], c["dst_stable_id"]) for c in work
               if c.get("outcome") == "accepted"}
    for pair, l in link_by_pair.items():
        if pair not in claimed:
            work.append({"src_stable_id": pair[0], "dst_stable_id": pair[1],
                         "outcome": "accepted", "guard": None})

    inserted = written = unrecorded = 0
    done_pairs = set()
    for c in work:
        src, dst, outcome = c["src_stable_id"], c["dst_stable_id"], c.get("outcome")
        pair = (src, dst)
        link = link_by_pair.get(pair) if outcome == "accepted" else None
        if link is not None and pair in done_pairs:
            link = None                       # a repeated accepted claim: one link
        check = None
        if link is not None:
            done_pairs.add(pair)
            check = _check_link(conn, request, site_id, key, link, new_by_sid)

        kind = (link or {}).get("src_kind") or (new_by_sid.get(src) or {}).get("kind")
        subject = check[1] if isinstance(check, tuple) else src
        record_id = _existing_claim_record(conn, key, run_hash, dst, src)
        if record_id is None:
            if kind not in KIND_TABLE or not subject:
                unrecorded += 1
            else:
                record_id = record(continuity_xday.DECISION_KIND, kind, subject, dst,
                                   {"src_stable_id": src, "dst_stable_id": dst,
                                    "outcome": outcome, "guard": c.get("guard")},
                                   outcome if outcome in ("accepted", "rejected") else "rejected")
                written += 1

        if link is None:
            continue
        if isinstance(check, str):
            _bump(skipped, check)
            continue
        row_id, current = check
        link_id = event_links.insert_continues(
            conn, site_id=site_id, src_kind=kind, src_row_id=row_id, src_stable_id=current,
            dst_stable_id=dst, source_s3_key=key, rule_version=continuity_xday.RULE_VERSION,
            decision_record_id=record_id)
        if link_id is None:
            _bump(skipped, "duplicate")
        else:
            inserted += 1

    already_run = conn.execute(
        "SELECT 1 FROM decision_records WHERE kind = %s AND input_key = %s AND input_hash = %s "
        "LIMIT 1", (RUN_KIND, key, run_hash)).fetchone() is not None
    if not already_run:
        subject_type, subject = _run_subject(conn, site_id, key)
        record(RUN_KIND, subject_type, subject, None,
               {"claims": len(claims), "links": len(links), "inserted": inserted,
                "skipped": dict(skipped), "unrecorded_claims": unrecorded,
                "trigger": request.get("trigger")},
               "processed")
        written += 1

    logger.info("xday link writer: key=%s run=%s claims=%d links=%d inserted=%d skipped=%s "
                "records=%d unrecorded=%d", key, run_hash, len(claims), len(links), inserted,
                ",".join(f"{k}:{v}" for k, v in sorted(skipped.items())) or "-", written,
                unrecorded)
    return {"inserted": inserted, "skipped": skipped, "records": written}


def lambda_handler(event, _context):
    with get_connection() as conn:
        return write_links(conn, event)
