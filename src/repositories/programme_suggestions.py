"""Repository for programme_progress_suggestions (migration 0008) -- Task 1
of the programme<->item feedback plan; see
docs/superpowers/specs/2026-07-12-programme-item-feedback-design.md (S3 D3,
S4) and docs/superpowers/plans/2026-07-12-programme-item-feedback.md (Task 1).

Style mirrors src/repositories/observations.py (module-level SQL,
conn.cursor(row_factory=dict_row).execute(...).fetchone()/.fetchall()).
jsonb binding follows src/repositories/chunks.py's Jsonb() convention.
"""
import hashlib

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_COLS = ("id, site_id, task_id, topic_id, topic_title, topic_summary, topic_user_id, "
         "report_date, source_s3_key, task_name, task_status_before, task_progress_before, "
         "suggested_status, suggested_progress, confidence, match_evidence, dedupe_key, "
         "state, decided_by, decided_at, applied_status, applied_progress, "
         "created_at, updated_at")


def _norm(s) -> str:
    return " ".join((s or "").lower().split())


def _dedupe_key(site_id, task_id, report_date, topic_title) -> str:
    """Stable across topic uuid churn (topics are deleted/re-inserted on
    reprocess and bulk-deleted on nightly supersession, topics.py:60-101):
    keys on (site, task, day, normalized title) instead."""
    raw = f"{site_id}|{task_id}|{report_date}|{_norm(topic_title)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def upsert_suggestion(conn, *, site_id, task_id, topic_id, topic_title, topic_summary,
                      topic_user_id, report_date, source_s3_key, task_name,
                      task_status_before, task_progress_before, suggested_status,
                      suggested_progress, confidence, match_evidence) -> dict | None:
    """Idempotent insert keyed on dedupe_key. On conflict, only a still-
    `pending` row is touched (re-points topic_id at the freshest topic uuid
    and bumps updated_at); a confirmed/rejected/stale row is left alone --
    if the WHERE guard excludes the conflicting row, RETURNING yields
    nothing and this returns None."""
    dedupe_key = _dedupe_key(site_id, task_id, report_date, topic_title)
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO programme_progress_suggestions ("
        f"site_id, task_id, topic_id, topic_title, topic_summary, topic_user_id, "
        f"report_date, source_s3_key, task_name, task_status_before, task_progress_before, "
        f"suggested_status, suggested_progress, confidence, match_evidence, dedupe_key) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        f"ON CONFLICT (dedupe_key) DO UPDATE SET topic_id = EXCLUDED.topic_id, updated_at = now() "
        f"WHERE programme_progress_suggestions.state = 'pending' "
        f"RETURNING {_COLS}",
        (site_id, task_id, topic_id, topic_title, topic_summary, topic_user_id,
         report_date, source_s3_key, task_name, task_status_before, task_progress_before,
         suggested_status, suggested_progress, confidence, Jsonb(match_evidence or {}),
         dedupe_key),
    ).fetchone()


def list_for_site(conn, site_id, state='pending') -> list[dict]:
    """state=None returns all states; otherwise filtered to that state.
    Newest report_date first, then newest created_at within a date."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM programme_progress_suggestions "
        f"WHERE site_id=%s AND (%s::text IS NULL OR state=%s) "
        f"ORDER BY report_date DESC, created_at DESC",
        (site_id, state, state),
    ).fetchall()


def get(conn, suggestion_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM programme_progress_suggestions WHERE id=%s",
        (suggestion_id,),
    ).fetchone()


def decide(conn, suggestion_id, state, decided_by, applied_status=None,
          applied_progress=None) -> dict | None:
    """Guards WHERE state='pending' -- a suggestion can only be decided
    once; deciding an already-decided row returns None (no double-decide)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE programme_progress_suggestions SET state=%s, decided_by=%s, "
        f"decided_at=now(), applied_status=%s, applied_progress=%s, updated_at=now() "
        f"WHERE id=%s AND state='pending' "
        f"RETURNING {_COLS}",
        (state, decided_by, applied_status, applied_progress, suggestion_id),
    ).fetchone()


def mark_stale(conn, suggestion_id) -> dict | None:
    """Target task vanished from programme.json, or the source topic was
    superseded before review. Guards WHERE state='pending' like decide()."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE programme_progress_suggestions SET state='stale', updated_at=now() "
        f"WHERE id=%s AND state='pending' "
        f"RETURNING {_COLS}",
        (suggestion_id,),
    ).fetchone()
