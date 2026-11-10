"""
Lambda: suggestion-writer (Phase — Programme<->Item feedback, Task 2).

In-VPC (psycopg direct to Aurora; mirrors lambda_item_writer's exact
`with get_connection() as conn:` usage — see db/connection.get_connection's
docstring: that context-manager form commits on clean exit, a bare
get_connection()+close() would roll back writes).

This is the in-VPC half of the two-hop programme<->item match: the non-VPC
matcher (Task 3 — internet: DashScope + Claude, no Aurora egress per BUG-36)
Lambda-invokes this writer with a batch of suggestions to insert. Splitting
the hop this way keeps DashScope/Claude calls out of the VPC (which has only
an S3 gateway endpoint, BUG-36) while keeping the Aurora write in-VPC.

Idempotency: delegated entirely to
repositories.programme_suggestions.upsert_suggestion's dedupe_key upsert.
A None return means the dedupe_key hit an already-decided (confirmed/
rejected/stale) row — that is normal, NOT an error, and must not be
re-created or counted as written.

Entry point (event shape):
  {"suggestions": [ {site_id, task_id, topic_id, topic_title, topic_summary,
                      topic_user_id, report_date, source_s3_key, task_name,
                      task_status_before, task_progress_before,
                      suggested_status, suggested_progress, confidence,
                      match_evidence}, ... ],
   "impacts": [ {finding_id, task_id, impact_severity, impact_note,
                  impact_task_name, impact_evidence}, ... ],
   "verdicts": [ {kind, subject_type, subject, subject_is_row_id?,
                  object_ref, site_id, provider, model, model_version,
                  question_set, input_key, input_hash, output, score,
                  threshold, auto_outcome}, ... ],
   "status_suggestions": [ {stable_id, site_id, suggested_status, quote,
                            segment_key, offset_sec, run}, ... ],
   "pick_cards": [ {site_id, suggested_status, quote, segment_key, offset_sec,
                    candidates, run}, ... ]}
  -> {"written": N, "impacts_applied": M, "verdicts_recorded": K, "verdicts_failed": F,
      "status_written": S, "status_skipped": {gone, moved_site, terminal, same,
                                               duplicate, failed},
      "picks_written": P, "picks_skipped": {gone, moved_site, terminal, same,
                                             too_many, duplicate, failed}}

`impacts` is the programme-impact-link plan's Task 3 addition (see
docs/superpowers/plans/2026-07-13-programme-impact-link.md, Task 3): each
entry is one matcher verdict, applied via
repositories.findings.apply_impact as an UPDATE on the finding row, in the
SAME transaction as the suggestion writes above. Backward compatible: a
missing/empty `impacts` key behaves EXACTLY as before this change --
apply_impact is never called and the response carries no
`impacts_applied` key. A None return from apply_impact means the finding
row vanished under nightly supersession or a racing re-extraction (D4/D5
of the plan) -- a NORMAL skip, not an error, so it is simply not counted.

`verdicts` is Track B Task 6a's addition: one decision_records row is
inserted per entry, in the SAME transaction as the suggestion/impact
writes above -- every gated matcher verdict (accepted AND rejected)
becomes a durable row, not just the accepted half `programme_suggestions`/
`findings` ever saw. Backward compatible the same way `impacts` is: a
missing/empty `verdicts` key (an old matcher deploy mid-rollout) behaves
EXACTLY as before this existed, and the response carries no
`verdicts_recorded` key. `site_id` travels on EACH verdict (not just once
on the event) because `decision_records.company_id` is NOT NULL and this
writer has no other way to resolve it -- `_company_id_for_site` looks it
up via `repositories.sites.get_site`, cached per site_id within one
invocation so a batch of verdicts for the same site costs one query, not
N. A verdict whose site_id resolves to no company (or whose
`subject_is_row_id` finding no longer exists) is skipped with a WARNING.
The insert itself (Ruling R14) runs inside its own SAVEPOINT with a
WARNING on failure -- same posture as lambda_item_writer's work_class
records (Ruling R10) -- so ANY verdict-record problem, skip or exception,
degrades only that one record and never aborts the suggestion/impact
writes riding in the same transaction. The response's `verdicts_recorded`/
`verdicts_failed` (present whenever `verdicts` is non-empty) sum to
`len(verdicts)`.

`status_suggestions` and `pick_cards` are the cross-session status reconcile
(r0, Task 3) additions: the matcher heard a spoken status statement in one
session that addresses an item from an EARLIER session. Neither ever writes
action_items.status -- a suggestion lands in action_status_suggestions, a pick
card (one statement, 2-4 possible items) in status_pick_cards, and a person
decides. The matcher is non-VPC and worked from a snapshot, so every item is
RE-READ here (action_items.get_live_by_stable_id) and a status suggestion is
skipped when the item is gone (or the id is malformed), has moved to another
site, is already terminal (done/closed), or already has the suggested status.
Site ids are compared canonically (uuid.UUID on both sides); a malformed
site id on the event never matches, so it counts as `moved_site`.

A pick card applies the SAME per-candidate predicate to every candidate
(`_candidate_drop_reason`): a candidate that is gone, on another site,
terminal, or already at the card's suggested status is dropped -- choosing a
closed candidate from a stale card would PATCH a hand-closed item back open,
and a same-status one is a no-op choice. Survivors keep their given order.
`picks_skipped` counts CARDS, not candidates: a card left with fewer than 2
survivors is skipped and counted under the reason of the drop that took it
below 2 (no candidates at all counts as `gone`); a written card's dropped
candidates are not counted anywhere. More than 4 survivors is skipped as
`too_many` before any insert -- the card is never truncated, because the
system must not choose which items a person gets to pick from.

company_id ALWAYS comes from the re-read row -- any company_id on the event
is ignored. A None from either insert is
the repo's idempotency (that item/anchor already has one, in any state) and
is counted as `duplicate`. Each insert runs in its own SAVEPOINT (same posture
as `_record_verdict`, Ruling R14), so one failure is counted as `failed` and
never aborts the programme suggestions sharing the transaction. Backward
compatible: the status_*/picks_* response keys appear only when the
corresponding input list is non-empty. Quote text is never logged.

Environment Variables:
    PG*/DATABASE_URL - read by db.connection.get_connection()
"""
import datetime
import logging
import uuid

from db.connection import get_connection
from repositories import (action_items, decision_records, findings, programme_suggestions,
                          sites, status_pick_cards, status_suggestions)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _coerce_report_date(suggestion: dict) -> dict:
    """JSON gives report_date as an ISO string; the column is `date`. Leave
    other None-able fields (topic_id, topic_user_id, topic_summary,
    suggested_status, suggested_progress, task_status_before,
    task_progress_before) as-is — upsert_suggestion accepts None for them."""
    report_date = suggestion.get("report_date")
    if isinstance(report_date, str):
        suggestion = dict(suggestion, report_date=datetime.date.fromisoformat(report_date))
    return suggestion


def _company_id_for_site(conn, site_id, cache):
    """company_id for `site_id`, memoised in `cache` for the life of one
    invocation -- a batch of verdicts almost always shares one site
    (one match_requests/ artifact = one site, module docstring of
    lambda_programme_matcher.py), so this is one query, not N. None when
    the site row does not exist (or site_id is None -- an old-shaped
    verdict dict missing the field)."""
    if site_id not in cache:
        site = sites.get_site(conn, site_id) if site_id is not None else None
        cache[site_id] = site["company_id"] if site else None
    return cache[site_id]


def _record_verdict(conn, entry, company_id_cache):
    """Insert one decision_records row for one matcher verdict (Track B
    Task 6a). Returns True on a successful insert, False on a skip -- never
    raises.

    `subject_is_row_id` (set by `_build_impact_verdict_record` for a
    programme_impact verdict) means `entry["subject"]` is a
    `findings.id` ROW id, not a `findings.stable_id` -- the matcher is
    deliberately non-VPC (BUG-36, no Aurora egress) and can only carry the
    row id it read from the match_requests/ artifact; this in-VPC writer
    resolves id -> stable_id here, in SQL, before the record is
    written.

    Ruling R14: the INSERT itself runs inside its OWN SAVEPOINT
    (`conn.transaction()`, same posture as lambda_item_writer's
    `_record_work_class_decision`/Ruling R10) -- catching an exception in
    Python does NOT un-abort the enclosing transaction Postgres already
    aborted, so without a real SAVEPOINT one malformed verdict entry (e.g.
    a NOT NULL violation) would take the suggestion/impact writes it shares
    a transaction with down too. A pre-insert skip (no company_id, a
    vanished finding row) never reaches the SAVEPOINT at all -- there is
    nothing to roll back."""
    company_id = _company_id_for_site(conn, entry.get("site_id"), company_id_cache)
    if company_id is None:
        logger.warning("decision record skipped: no company for site_id=%s", entry.get("site_id"))
        return False

    subject_stable_id = entry.get("subject")
    if entry.get("subject_is_row_id"):
        subject_stable_id = findings.get_stable_id(conn, subject_stable_id)
        if subject_stable_id is None:
            logger.warning("decision record skipped: finding row %s not found", entry.get("subject"))
            return False

    try:
        with conn.transaction():
            decision_records.insert(
                conn, company_id=company_id, site_id=entry.get("site_id"),
                kind=entry["kind"], subject_type=entry["subject_type"],
                subject_stable_id=subject_stable_id, object_ref=entry.get("object_ref"),
                provider=entry["provider"], model=entry.get("model"),
                model_version=entry.get("model_version"), question_set=entry.get("question_set"),
                input_key=entry.get("input_key"), input_hash=entry.get("input_hash"),
                output=entry["output"], score=entry.get("score"), threshold=entry.get("threshold"),
                auto_outcome=entry["auto_outcome"],
            )
    except Exception:
        logger.warning("decision record insert failed for kind=%s subject=%s -- "
                       "suggestions/impacts in this batch are unaffected",
                       entry.get("kind"), entry.get("subject"))
        return False
    return True


_TERMINAL = ("done", "closed")
_MAX_PICK_CANDIDATES = 4   # status_pick_cards CHECK: 2-4 candidates


def _canon_uuid(value):
    """Canonical string form of a uuid (any case, with or without braces/dashes), or None
    when `value` is missing or malformed -- never raises."""
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _candidate_drop_reason(row, site_id, suggested_status):
    """Why a re-read item must NOT be offered for `suggested_status` on `site_id`, or None
    when it may. Shared by status suggestions and pick-card candidates. `site_id` is
    already canonical (None when the event's was malformed, which never matches)."""
    if row is None:
        return "gone"
    if site_id is None or _canon_uuid(row["site_id"]) != site_id:
        return "moved_site"
    if row["status"] in _TERMINAL:
        return "terminal"
    if row["status"] == suggested_status:
        return "same"
    return None


def _write_status_suggestion(conn, entry):
    """Re-read the item, then insert one status suggestion. Returns "written" or the skip
    reason (gone / moved_site / terminal / same / duplicate / failed) -- never raises
    from the insert."""
    row = action_items.get_live_by_stable_id(conn, entry.get("stable_id"))
    reason = _candidate_drop_reason(row, _canon_uuid(entry.get("site_id")),
                                    entry.get("suggested_status"))
    if reason is not None:
        return reason
    try:
        with conn.transaction():
            made = status_suggestions.insert_suggestion(
                conn, row["company_id"], row["site_id"], row["stable_id"],
                entry["suggested_status"], entry.get("quote"), entry["segment_key"],
                entry["offset_sec"], entry["run"])
    except Exception:
        logger.warning("status suggestion insert failed for stable_id=%s -- the rest of "
                       "this batch is unaffected", entry.get("stable_id"))
        return "failed"
    return "written" if made is not None else "duplicate"


def _write_pick_card(conn, entry):
    """Re-read every candidate (de-duplicated, given order kept) and drop each one
    `_candidate_drop_reason` rejects. Returns "written", "duplicate", "failed", "too_many"
    (more than 4 survivors -- never truncated), or, when fewer than 2 survive, the reason of
    the drop that took the card below 2 ("gone" for an empty list). Never raises from the
    insert."""
    site_id = _canon_uuid(entry.get("site_id"))
    suggested_status = entry.get("suggested_status")
    candidates, seen = [], set()
    for candidate in entry.get("candidates") or []:
        key = _canon_uuid(candidate) or str(candidate)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    survivors, possible, below_two_reason = [], len(candidates), None
    for candidate in candidates:
        row = action_items.get_live_by_stable_id(conn, candidate)
        reason = _candidate_drop_reason(row, site_id, suggested_status)
        if reason is None:
            survivors.append(row)
            continue
        possible -= 1
        if possible < 2 and below_two_reason is None:
            below_two_reason = reason
    if len(survivors) < 2:
        return below_two_reason or "gone"
    if len(survivors) > _MAX_PICK_CANDIDATES:
        logger.warning("pick card skipped: %d live candidates (max %d) for segment_key=%s "
                       "offset_sec=%s", len(survivors), _MAX_PICK_CANDIDATES,
                       entry.get("segment_key"), entry.get("offset_sec"))
        return "too_many"
    first = survivors[0]
    try:
        with conn.transaction():
            made = status_pick_cards.insert_pick_card(
                conn, first["company_id"], first["site_id"], entry["suggested_status"],
                entry.get("quote"), entry["segment_key"], entry["offset_sec"],
                [str(r["stable_id"]) for r in survivors], entry["run"])
    except Exception:
        logger.warning("pick card insert failed for segment_key=%s offset_sec=%s -- the rest "
                       "of this batch is unaffected",
                       entry.get("segment_key"), entry.get("offset_sec"))
        return "failed"
    return "written" if made is not None else "duplicate"


def lambda_handler(event, _context):
    suggestions = (event or {}).get("suggestions") or []
    impacts = (event or {}).get("impacts") or []
    verdicts = (event or {}).get("verdicts") or []
    status_entries = (event or {}).get("status_suggestions") or []
    pick_entries = (event or {}).get("pick_cards") or []
    if not (suggestions or impacts or verdicts or status_entries or pick_entries):
        # Guard BEFORE opening a DB connection — an empty batch never
        # touches Aurora. Every list must be empty: a verdicts-only (or
        # status-only / picks-only) payload must still open the connection.
        return {"written": 0}

    written = 0
    impacts_applied = 0
    verdicts_recorded = 0
    with get_connection() as conn:
        for s in suggestions:
            row = programme_suggestions.upsert_suggestion(conn, **_coerce_report_date(s))
            if row is not None:
                written += 1

        for entry in impacts:
            row = findings.apply_impact(
                conn, entry["finding_id"],
                task_id=entry["task_id"],
                impact_severity=entry["impact_severity"],
                impact_note=entry.get("impact_note"),
                impact_task_name=entry.get("impact_task_name"),
                impact_evidence=entry.get("impact_evidence") or {},
            )
            if row is not None:
                impacts_applied += 1

        company_id_cache = {}
        for entry in verdicts:
            if _record_verdict(conn, entry, company_id_cache):
                verdicts_recorded += 1

        status_written = 0
        status_skipped = dict.fromkeys(
            ("gone", "moved_site", "terminal", "same", "duplicate", "failed"), 0)
        for entry in status_entries:
            outcome = _write_status_suggestion(conn, entry)
            if outcome == "written":
                status_written += 1
            else:
                status_skipped[outcome] += 1

        picks_written = 0
        picks_skipped = dict.fromkeys(
            ("gone", "moved_site", "terminal", "same", "too_many", "duplicate", "failed"), 0)
        for entry in pick_entries:
            outcome = _write_pick_card(conn, entry)
            if outcome == "written":
                picks_written += 1
            else:
                picks_skipped[outcome] += 1
    verdicts_failed = len(verdicts) - verdicts_recorded

    logger.info("suggestion-writer wrote %d/%d suggestions, applied %d/%d impacts, "
                "recorded %d/%d verdicts (%d failed)",
                written, len(suggestions), impacts_applied, len(impacts),
                verdicts_recorded, len(verdicts), verdicts_failed)
    result = {"written": written}
    if impacts:
        result["impacts_applied"] = impacts_applied
    if verdicts:
        result["verdicts_recorded"] = verdicts_recorded
        result["verdicts_failed"] = verdicts_failed
    if status_entries:
        logger.info("suggestion-writer status suggestions: wrote %d/%d, skipped %s",
                    status_written, len(status_entries), status_skipped)
        result["status_written"] = status_written
        result["status_skipped"] = status_skipped
    if pick_entries:
        logger.info("suggestion-writer pick cards: wrote %d/%d, skipped %s",
                    picks_written, len(pick_entries), picks_skipped)
        result["picks_written"] = picks_written
        result["picks_skipped"] = picks_skipped
    return result
