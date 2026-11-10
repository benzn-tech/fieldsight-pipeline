"""
Lambda: nightly-support-writer -- hop 3 of the nightly J2 turn pick (in-VPC).

Spec: docs/superpowers/specs/2026-11-10-nightly-rematch-agent-design.md §3 J2 (writer), §4.2
(accounting), §5 (silent-write rules).

The non-VPC worker (lambda_nightly_turn_pick) calls the model, parses and guards the answer,
and invokes this function RequestResponse. This function writes, and only writes:
  * one `decision_records` row of kind `nightly_support` per guarded row whose item is still
    live on the request's site (accepted, rejected or abstained);
  * one `event_links` supported_by row (method nightly_turn_pick_v1, score NULL) per accepted
    pick whose row is still live, on the site and holding no supported_by link of ANY method;
  * the model call's usage on the hub run that built the request;
  * claim_type / claim_basis for the session, through claim_type_store.recompute_session,
    when at least one link was written (J3 for tonight's links).

Event (the worker builds it; ids, enums, locators and counts only -- never text):
  {"site_id", "run_id", "extraction_key", "session_base", "question_set", "input_key",
   "rows": [{"stable_id", "row_id", "kind", "turn" ("T<n>"|null), "outcome"
             (accepted|rejected|abstained), "guard" (enum|null), "cross_language" (bool),
             "locator" ({source_stem, start_sec, end_sec, speaker_label, quote_status}|null)}],
   "usage": {"tokens_in", "tokens_out", "model", "cost_usd", "model_call" (bool, default
             true)}}

An `abstained` row with a guard such as `no_transcript`, `unsupported_session`, `no_text` or
`no_window` and no locator is an item the worker never asked the model about; it gets its
nightly_support record like any other live row (so J2 does not re-ask it and the breaker
counts the request answered). `usage.model_call: false` means no model call was made: the
run's model_calls and models are left as they are (event_runs.add_usage model_call=False).

Return: {"written": n, "records": n, "skipped": {"gone": n, "linked": n, "moved_site": n},
         "errors": n}
  gone       -- the row is deleted, superseded or hidden (re-read through deleted_predicates);
               no record, no link
  moved_site -- the row is now on another site; no record (it is another site's item), no link
  linked     -- an accepted pick whose row already holds a supported_by link (any method, e.g.
               the lexical linker's); recorded with guard `already_linked`, auto_outcome
               rejected, and that link is never touched
  errors     -- a row that raised; its own transaction rolled back, the other rows stand

Order and transactions:
  1. The site's CURRENT company (sites.company_id). Unknown site -> raise.
  2. Usage first, in its own transaction: event_runs.add_usage scoped by run_id AND company AND
     site. None (an unknown run, or another tenant's) -> raise, nothing written. run_id is
     request input, so this is the tenant gate for the whole invocation.
  3. Each row in its own transaction, so one bad row does not lose the others.
  4. recompute_session in its own transaction when written > 0. A failure or timeout is
     logged (type name only), returned as `recompute: "failed" | "timed_out"`, and leaves a
     claim_recompute_failures marker (own connection) so the nightly J3 scan retries it. It
     does not raise: a raise would make the worker's S3 retry pay for a second model call.
  The record's `model` is the registry's (model_registry), never the payload's; the usage
  row takes the payload's usage as the worker reported it. A request with `rows: []` only
  records usage (the worker sends one after a paid call that failed).

A raise anywhere before the rows reaches the worker as a FunctionError, and the worker
raises (fail closed). A re-delivered request writes a second record per item (each record
stands for one paid call); links are protected by migration 0102's one-nightly-link-per-row
indexes (insert ON CONFLICT DO NOTHING -> counted `linked`).

Logs carry ids, counts and exception type names only, never item or turn text.

Environment Variables:
    PG*/DATABASE_URL - read by db.connection.get_connection()
"""
import json
import logging

from psycopg.errors import QueryCanceled

import model_registry
import nightly_turn_pick
import turn_name_overlay
from db.connection import get_connection
from deleted_predicates import visible_topics_predicate
from repositories import (claim_type_store, decision_records, event_links, event_runs, nightly,
                          sites)
from repositories.event_links import KIND_SRC_COLUMN, KIND_TABLE
from repositories.topics import _topic_not_hidden

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DECISION_KIND = "nightly_support"
PROVIDER = "anthropic"
REGISTRY_TASK = "nightly_turn_pick"
OUTCOMES = ("accepted", "rejected", "abstained")


class _MovedSite(Exception):
    """insert_supported_by found the row on another site: roll the row back."""


def _reread(conn, kind, row_id):
    """(site_id, stable_id, linked) of the row if it is still visible, else None."""
    table, col = KIND_TABLE[kind], KIND_SRC_COLUMN[kind]
    return conn.execute(
        f"SELECT c.site_id::text, c.stable_id::text, "
        f"EXISTS (SELECT 1 FROM event_links e WHERE e.{col} = c.id "
        f"AND e.link_type = 'supported_by') "
        f"FROM {table} c JOIN topics t ON t.id = c.topic_id "
        f"WHERE c.id = %s AND {visible_topics_predicate('t')} AND {_topic_not_hidden('t')}",
        (row_id,)).fetchone()


def _dst_turn(locator):
    return {k: locator.get(k) for k in claim_type_store.DST_TURN_KEYS}


def _write_row(conn, ctx, row, counts):
    kind, row_id = row.get("kind"), row.get("row_id")
    if kind not in KIND_TABLE or not row_id:
        raise ValueError("row has no known kind or no row_id")
    outcome, guard = row.get("outcome"), row.get("guard")
    if outcome not in OUTCOMES:
        raise ValueError("row has no known outcome")
    locator = row.get("locator") if isinstance(row.get("locator"), dict) else None
    if outcome == "accepted" and locator is None:
        raise ValueError("accepted row has no locator")

    found = _reread(conn, kind, row_id)
    if found is None:
        counts["gone"] += 1
        return
    row_site, stable_id, linked = found
    if row_site != ctx["site_id"]:
        counts["moved_site"] += 1
        return

    if outcome == "accepted" and row.get("cross_language") and not ctx["allow_cross_language"]:
        outcome, guard = "rejected", "cross_language_off"
    if outcome == "accepted" and linked:
        outcome, guard = "rejected", "already_linked"
        counts["linked"] += 1

    record = decision_records.insert(
        conn, company_id=ctx["company_id"], site_id=ctx["site_id"], kind=DECISION_KIND,
        subject_type=kind, subject_stable_id=stable_id,
        object_ref=nightly_turn_pick.locator_ref(locator) if locator else None,
        provider=PROVIDER, model=ctx["model"], question_set=ctx["question_set"],
        input_key=ctx["input_key"],
        output={"turn": row.get("turn") if locator else None, "guard": guard},
        auto_outcome=outcome)
    counts["records"] += 1
    if outcome != "accepted":
        return

    try:
        link_id = event_links.insert_supported_by(
            conn, site_id=ctx["site_id"], src_kind=kind, src_row_id=row_id,
            source_s3_key=ctx["extraction_key"], dst_turn=_dst_turn(locator), score=None,
            rule_version=nightly_turn_pick.RULE_VERSION, method=nightly_turn_pick.METHOD,
            decision_record_id=record["id"], on_conflict="skip")
    except ValueError as e:
        raise _MovedSite() from e
    if link_id is None:
        # A concurrent nightly link won the race: this record is ours (written above in this
        # transaction), so it is corrected rather than left saying "accepted".
        conn.execute(
            "UPDATE decision_records SET auto_outcome = 'rejected', output = %s::jsonb "
            "WHERE id = %s",
            (json.dumps({"turn": row.get("turn"), "guard": "already_linked"}), record["id"]))
        counts["linked"] += 1
        return
    counts["written"] += 1


def write_support(conn, event):
    """Everything hop 3 does, on `conn`. Each step runs in `conn.transaction()` (a real
    transaction on an autocommit connection, a savepoint inside a caller's transaction)."""
    event = event or {}
    site_id = str(event.get("site_id") or "")
    run_id = event.get("run_id")
    extraction_key = event.get("extraction_key")
    session_base = event.get("session_base")
    if not site_id or not run_id or not extraction_key or not session_base:
        raise ValueError("event lacks site_id/run_id/extraction_key/session_base")
    usage = event.get("usage") or {}
    rows = [r for r in event.get("rows") or [] if isinstance(r, dict)]
    entry = model_registry.get(REGISTRY_TASK)

    site = sites.get_site(conn, site_id)
    if site is None:
        raise RuntimeError(f"nightly support writer: site {site_id} not found")
    company_id = str(site["company_id"])

    with conn.transaction():
        run = event_runs.add_usage(
            conn, run_id, company_id, site_id, tokens_in=usage.get("tokens_in"),
            tokens_out=usage.get("tokens_out"), model=usage.get("model"),
            cost_usd=usage.get("cost_usd"), model_call=usage.get("model_call") is not False)
    if run is None:
        raise RuntimeError(f"nightly support writer: run {run_id} is not a run of site "
                           f"{site_id}; nothing written")

    ctx = {"site_id": site_id, "company_id": company_id, "extraction_key": extraction_key,
           "input_key": event.get("input_key"),
           "question_set": event.get("question_set") or nightly_turn_pick.PROMPT_HASH,
           "model": entry["model"], "allow_cross_language": bool(
               entry.get("allow_cross_language"))}
    counts = {"written": 0, "records": 0, "gone": 0, "linked": 0, "moved_site": 0,
              "errors": 0}
    for row in rows:
        before = dict(counts)
        try:
            with conn.transaction():
                _write_row(conn, ctx, row, counts)
        except _MovedSite:
            counts.clear()
            counts.update(before, moved_site=before["moved_site"] + 1)
        except Exception as e:  # noqa: BLE001 -- one bad row must not lose the others
            counts.clear()
            counts.update(before, errors=before["errors"] + 1)
            logger.warning("nightly support writer: row %s failed: %s",
                           row.get("row_id"), type(e).__name__)

    out = {"written": counts["written"], "records": counts["records"],
           "skipped": {"gone": counts["gone"], "linked": counts["linked"],
                       "moved_site": counts["moved_site"]},
           "errors": counts["errors"]}
    if counts["written"] > 0:
        reason = None
        try:
            with conn.transaction():
                result = claim_type_store.recompute_session(
                    conn, company_id=company_id, site_id=site_id, session_base=session_base,
                    statement_timeout="15s")
            if isinstance(result, dict) and result.get("timed_out"):
                reason = "timed_out"
            elif isinstance(result, dict) and result.get("failed"):
                reason = "failed"
        except QueryCanceled:
            reason = "timed_out"
        except Exception as e:  # noqa: BLE001 -- the links stand; see the module docstring
            reason = "failed"
            logger.warning("nightly support writer: recompute failed key=%s: %s",
                           extraction_key, type(e).__name__)
        out["recompute"] = reason or "done"
        if reason:
            # J3's marker, so the nightly scan retries the recompute.
            _mark_recompute_failed(company_id, session_base, reason)

    logger.info("nightly support writer: site=%s run=%s key=%s rows=%d written=%d records=%d "
                "skipped=%s errors=%d", site_id, run_id, extraction_key, len(rows),
                out["written"], out["records"], out["skipped"], out["errors"])
    return out


def _mark_recompute_failed(company_id, session_base, reason):
    """Upsert the session's claim_recompute_failures marker on a connection and transaction
    of its own (as the voiceprint writer does: a cancelled statement can leave the writer's
    connection unusable). Keyed like the voiceprint writer's marker (the normalised
    `sid{32hex}`). Best-effort: a failure is logged by type and never raised."""
    key = turn_name_overlay.session_base(session_base) or session_base
    try:
        with get_connection() as conn:
            nightly.mark_recompute_failed(conn, company_id, key, reason)
    except Exception as e:  # noqa: BLE001
        logger.warning("nightly support writer: recompute marker not written for %s: %s",
                       key, type(e).__name__)


def lambda_handler(event, _context):
    # autocommit: each conn.transaction() above is its own real transaction.
    with get_connection(autocommit=True) as conn:
        return write_support(conn, event)
