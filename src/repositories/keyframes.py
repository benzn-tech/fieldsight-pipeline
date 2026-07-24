"""Keyframe tombstones + generation/deletion telemetry (video-keyframe Q7).

Tombstones are keyed on the full s3_key -- the only identifier stable across
item-writer's delete-then-reinsert topic churn. Events store ONLY structural
signal (0023_classification_feedback posture): no image, no transcript text.

Used by BOTH lambda_org_api (delete endpoint) and lambda_keyframe (generator);
both are in-VPC with psycopg + PG env, so these are ordinary DB reads/writes and
add zero new AWS calls (BUG-36-clean).
"""
from psycopg.rows import dict_row


def add_tombstone(conn, s3_key, company_id, topic_id, deleted_by):
    """Idempotent (ON CONFLICT DO NOTHING). True iff a NEW tombstone row was
    written; False when the key was already tombstoned (idempotent re-delete)."""
    row = conn.execute(
        "INSERT INTO keyframe_tombstones (s3_key, company_id, topic_id, deleted_by) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT (s3_key) DO NOTHING RETURNING s3_key",
        (s3_key, company_id, topic_id, deleted_by),
    ).fetchone()
    return row is not None


def get_tombstone(conn, s3_key):
    """The tombstone row (dict) or None -- the delete endpoint's idempotent
    re-delete path resolves the owning company from here when the topic_photos
    row is already gone."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT s3_key, company_id, topic_id, deleted_by, created_at "
        "FROM keyframe_tombstones WHERE s3_key=%s", (s3_key,)).fetchone()


def tombstoned_subset(conn, s3_keys) -> set:
    """Which of these keys are tombstoned. One ANY(%s) query; empty input ->
    empty set WITHOUT touching the DB (the generator calls this once per
    request, and an audio-only day plans zero frames)."""
    keys = list(s3_keys)
    if not keys:
        return set()
    rows = conn.execute(
        "SELECT s3_key FROM keyframe_tombstones WHERE s3_key = ANY(%s::text[])",
        (keys,)).fetchall()
    # rows are plain tuples on a default cursor; guard for dict_row too.
    return {(r["s3_key"] if isinstance(r, dict) else r[0]) for r in rows}


def record_event(conn, event, *, company_id=None, site_id=None, topic_category=None,
                 work_class=None, duration_min=None, n_frames_generated=None,
                 frame_index=None):
    """Append one 'generated'/'deleted' telemetry row (append-only, never read
    on a hot path). Structural signal only -- no s3_key, no caption, no text.
    Returns the row."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO keyframe_events (company_id, site_id, topic_category, "
        "work_class, duration_min, n_frames_generated, frame_index, event) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "RETURNING id, company_id, site_id, topic_category, work_class, "
        "duration_min, n_frames_generated, frame_index, event, created_at",
        (company_id, site_id, topic_category, work_class, duration_min,
         n_frames_generated, frame_index, event)).fetchone()
