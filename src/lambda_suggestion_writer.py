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
                      match_evidence}, ... ]}
  -> {"written": N}

Environment Variables:
    PG*/DATABASE_URL - read by db.connection.get_connection()
"""
import datetime
import logging

from db.connection import get_connection
from repositories import programme_suggestions

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


def lambda_handler(event, _context):
    suggestions = (event or {}).get("suggestions") or []
    if not suggestions:
        # Guard BEFORE opening a DB connection — an empty batch never
        # touches Aurora.
        return {"written": 0}

    written = 0
    with get_connection() as conn:
        for s in suggestions:
            row = programme_suggestions.upsert_suggestion(conn, **_coerce_report_date(s))
            if row is not None:
                written += 1

    logger.info("suggestion-writer wrote %d/%d suggestions", written, len(suggestions))
    return {"written": written}
