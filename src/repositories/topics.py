from psycopg.rows import dict_row

from repositories import findings

_TOPIC_COLS = ("id, site_id, user_id, source_s3_key, report_date, occurred_at, "
               "category, title, summary, created_at")


def upsert_topic(conn, site_id, report_date, title, *, user_id=None, source_s3_key=None,
                 occurred_at=None, category=None, summary=None,
                 action_items=None, safety=None, photos=None) -> dict:
    """Insert a topic with its children. NOTE: currently insert-only —
    no ON CONFLICT dedup. Dedup is instead handled by callers running
    delete_topics_for_scope() first to clear the (site_id, report_date, user_id)
    scope before re-inserting (Phase 4a scope-delete idempotency); insert-only
    semantics here are retained by design."""
    cur = conn.cursor(row_factory=dict_row)
    topic = cur.execute(
        f"INSERT INTO topics (site_id, user_id, source_s3_key, report_date, occurred_at, "
        f"category, title, summary) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_TOPIC_COLS}",
        (site_id, user_id, source_s3_key, report_date, occurred_at, category, title, summary),
    ).fetchone()
    tid = topic["id"]
    for a in (action_items or []):
        conn.execute(
            "INSERT INTO action_items (topic_id, site_id, text, responsible, deadline, priority, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (tid, site_id, a["text"], a.get("responsible"), a.get("deadline"),
             a.get("priority"), a.get("status", "open")),
        )
    for o in (safety or []):
        conn.execute(
            "INSERT INTO safety_observations (topic_id, site_id, observation, risk_level, location, status) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (tid, site_id, o["observation"], o.get("risk_level"), o.get("location"),
             o.get("status", "open")),
        )
    for p in (photos or []):
        conn.execute(
            "INSERT INTO topic_photos (topic_id, s3_key, caption_text) VALUES (%s,%s,%s)",
            (tid, p["s3_key"], p.get("caption_text")),
        )
    return topic


def list_site_topics(conn, site_id, report_date) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TOPIC_COLS} FROM topics WHERE site_id=%s AND report_date=%s "
        f"ORDER BY occurred_at NULLS LAST, created_at",
        (site_id, report_date),
    ).fetchall()


def get_topic_photos(conn, topic_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, s3_key, caption_text, created_at FROM topic_photos "
        "WHERE topic_id=%s ORDER BY created_at",
        (topic_id,),
    ).fetchall()


def delete_topics_for_source(conn, source_s3_key) -> int:
    """Delete topics rows produced from one source report.

    Keyed on source_s3_key — unique per report and immune to identity-
    resolution drift (see chunks.delete_chunks_for_source for the failure
    modes a (site, date, user_id) scope key had — Fable review C1/I1).
    Children (action_items, safety_observations, topic_photos) are removed
    automatically via ON DELETE CASCADE FKs to topics
    (see 0003_dashboard_readmodel.sql) — no separate child deletes needed."""
    cur = conn.execute(
        "DELETE FROM topics WHERE source_s3_key=%s",
        (source_s3_key,),
    )
    return cur.rowcount


def delete_topics_for_source_prefix(conn, source_prefix) -> int:
    """Delete topics rows whose source_s3_key starts with source_prefix.

    Phase 4b nightly-report supersession of session-sourced items: once the
    authoritative daily_report.json for a (date, user_folder) is ingested,
    lambda_ingest calls this with f"extractions/{user_folder}/{date}/" to
    remove that day's session-level (live) extraction topics, so the
    dashboard shows the report version instead of duplicate/stale live
    items. Children cascade-delete the same way as delete_topics_for_source.

    NOTE — LIKE wildcard escaping: SQL LIKE treats both '%' (any run of
    characters) and '_' (any single character) as wildcards. S3 user
    folders are '_'-joined display names (e.g.
    'extractions/Jarley_Trainor/2026-03-02/'), so the underscores in
    source_prefix are literal data, not wildcards — they (and any literal
    '%') must be escaped, or this DELETE would also match unrelated keys
    (e.g. 'extractions/JarleyXTrainor/...'). ESCAPE '\\' designates '\\' as
    the escape character, so '\\_'/'\\%' in the pattern are literal; only
    the trailing '%' appended here (unescaped) is a real wildcard.
    """
    escaped = source_prefix.replace('%', '\\%').replace('_', '\\_')
    cur = conn.execute(
        "DELETE FROM topics WHERE source_s3_key LIKE %s ESCAPE '\\'",
        (escaped + '%',),
    )
    return cur.rowcount


_TOPIC_COLS_JOINED = (
    "t.id, t.site_id, t.user_id, t.source_s3_key, t.report_date, t.occurred_at, "
    "t.category, t.title, t.summary, t.created_at"
)


def list_topics_for_date(conn, site_ids, report_date) -> list[dict]:
    """Dashboard multi-site read for one report_date: topics scoped to
    site_ids (a caller-computed ACL list — ALL sites for an admin, or
    memberships.accessible_site_ids for a scoped worker/PM), joined with
    each topic's site_name and user_name, plus its action_items and
    safety_observations children (two follow-up queries scoped to the
    topic ids the first query returned, then grouped in Python — mirrors
    get_topic_photos' per-topic-id pattern but batched instead of N+1).

    is_live is computed in Python (source_s3_key LIKE 'extractions/%') --
    True for session-sourced (not-yet-superseded) live extraction items,
    False for nightly-report-sourced items.

    Also attaches `findings` (migration 0010, programme-impact-link plan
    Task 5) via a THIRD batched child query -- same one-query-for-all-topics
    shape as action_items/safety_observations above, using
    repositories.findings.list_for_topics. Rows are exposed AS-IS (raw flat
    columns: observation/domain/severity/entity_name/entity_trade/
    programme_task_id/impact_severity/impact_note/impact_task_name/...).
    NOTE: the design spec §4 sketches a nested `programme_impact: {task_id,
    severity, note}` sub-object -- that reshape is a UI-phase mapping
    concern, not a read-path one; consumers (e.g. /live-items) get the flat
    columns straight off the `findings` table, same as every other child.

    Empty site_ids -> [] without a round-trip (mirrors sites.list_sites_by_ids)."""
    if not site_ids:
        return []

    topic_rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TOPIC_COLS_JOINED}, "
        f"s.name AS site_name, (u.first_name || ' ' || u.last_name) AS user_name "
        f"FROM topics t "
        f"LEFT JOIN sites s ON s.id = t.site_id "
        f"LEFT JOIN users u ON u.id = t.user_id "
        f"WHERE t.site_id = ANY(%s) AND t.report_date=%s "
        f"ORDER BY t.occurred_at NULLS LAST, t.created_at",
        (list(site_ids), report_date),
    ).fetchall()

    if not topic_rows:
        return []

    topic_ids = [t["id"] for t in topic_rows]
    action_items_by_topic = {}
    for a in conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, text, responsible, deadline, priority, status, created_at "
        "FROM action_items WHERE topic_id = ANY(%s) ORDER BY created_at",
        (topic_ids,),
    ).fetchall():
        action_items_by_topic.setdefault(a["topic_id"], []).append(a)

    safety_by_topic = {}
    for s in conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, observation, risk_level, location, status, created_at "
        "FROM safety_observations WHERE topic_id = ANY(%s) ORDER BY created_at",
        (topic_ids,),
    ).fetchall():
        safety_by_topic.setdefault(s["topic_id"], []).append(s)

    findings_by_topic = {}
    for f in findings.list_for_topics(conn, topic_ids):
        findings_by_topic.setdefault(f["topic_id"], []).append(f)

    for t in topic_rows:
        t["action_items"] = action_items_by_topic.get(t["id"], [])
        t["safety_observations"] = safety_by_topic.get(t["id"], [])
        t["findings"] = findings_by_topic.get(t["id"], [])
        t["is_live"] = bool(t["source_s3_key"]) and t["source_s3_key"].startswith("extractions/")

    return topic_rows
