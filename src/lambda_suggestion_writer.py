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
                  threshold, auto_outcome}, ... ]}
  -> {"written": N, "impacts_applied": M, "verdicts_recorded": K}

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
`subject_is_row_id` finding no longer exists) is skipped with a WARNING,
never allowed to abort the suggestion/impact writes riding in the same
transaction.

Environment Variables:
    PG*/DATABASE_URL - read by db.connection.get_connection()
"""
import datetime
import logging

from db.connection import get_connection
from repositories import decision_records, findings, programme_suggestions, sites

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
    Task 6a). Returns True on a successful insert, False on a skip (never
    raises -- a bad verdict entry must not abort the suggestion/impact
    writes riding in the same transaction; the caller decides how to make
    that true, e.g. by wrapping the whole `verdicts` loop in a SAVEPOINT).

    `subject_is_row_id` (set by `_build_impact_verdict_record` for a
    programme_impact verdict) means `entry["subject"]` is a
    `findings.id` ROW id, not a `findings.stable_id` -- the matcher is
    deliberately non-VPC (BUG-36, no Aurora egress) and can only carry the
    row id it read from the match_requests/ artifact; this in-VPC writer
    resolves id -> stable_id here, in SQL, before the record is
    written."""
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
    return True


def lambda_handler(event, _context):
    suggestions = (event or {}).get("suggestions") or []
    impacts = (event or {}).get("impacts") or []
    verdicts = (event or {}).get("verdicts") or []
    if not suggestions and not impacts and not verdicts:
        # Guard BEFORE opening a DB connection — an empty batch never
        # touches Aurora. All three lists must be empty: a verdicts-only
        # payload (no suggestions/impacts this run -- every verdict was
        # rejected) must still open the connection.
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

    logger.info("suggestion-writer wrote %d/%d suggestions, applied %d/%d impacts, "
                "recorded %d/%d verdicts",
                written, len(suggestions), impacts_applied, len(impacts),
                verdicts_recorded, len(verdicts))
    result = {"written": written}
    if impacts:
        result["impacts_applied"] = impacts_applied
    if verdicts:
        result["verdicts_recorded"] = verdicts_recorded
    return result
