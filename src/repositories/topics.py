from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from repositories import findings

_TOPIC_COLS = ("id, site_id, user_id, source_s3_key, report_date, occurred_at, "
               "category, title, summary, time_range, participants, source, created_at")


def _escape_like(prefix) -> str:
    """Escapes SQL LIKE wildcards ('%' and '_') in a literal prefix so it can
    be safely matched with a trailing '%' appended by the caller. S3 user
    folders are '_'-joined display names (e.g.
    'extractions/Jarley_Trainor/2026-03-02/'), so underscores in prefix are
    literal data, not wildcards -- they (and any literal '%') must be
    escaped, or a LIKE using this prefix would also match unrelated keys
    (e.g. 'extractions/JarleyXTrainor/...'). Pair with ESCAPE '\\' in the
    SQL so '\\_'/'\\%' are treated as literal characters."""
    return prefix.replace('%', '\\%').replace('_', '\\_')


def upsert_topic(conn, site_id, report_date, title, *, user_id=None, source_s3_key=None,
                 occurred_at=None, category=None, summary=None,
                 action_items=None, safety=None, photos=None,
                 time_range=None, participants=None) -> dict:
    """Insert a topic with its children. NOTE: currently insert-only —
    no ON CONFLICT dedup. Dedup is instead handled by callers running
    delete_topics_for_scope() first to clear the (site_id, report_date, user_id)
    scope before re-inserting (Phase 4a scope-delete idempotency); insert-only
    semantics here are retained by design.

    time_range/participants (migration 0011) are display fields the
    extraction JSON already carries but the Aurora boundary previously
    dropped -- both optional, no writer passes them yet (this task only
    lands the pass-through). participants is bound via Jsonb, same
    convention as every other jsonb column in this codebase (chunks.py,
    findings.py). `source` is NOT a kwarg here: it's a passive provenance
    column that defaults to 'ai' at the DB level (spec §8 Task 5 adds the
    'human' writer later)."""
    cur = conn.cursor(row_factory=dict_row)
    topic = cur.execute(
        f"INSERT INTO topics (site_id, user_id, source_s3_key, report_date, occurred_at, "
        f"category, title, summary, time_range, participants) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_TOPIC_COLS}",
        (site_id, user_id, source_s3_key, report_date, occurred_at, category, title, summary,
         time_range, Jsonb(participants) if participants is not None else None),
    ).fetchone()
    tid = topic["id"]
    for a in (action_items or []):
        conn.execute(
            "INSERT INTO action_items (topic_id, site_id, text, responsible, deadline, "
            "deadline_text, priority, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (tid, site_id, a["text"], a.get("responsible"), a.get("deadline"),
             a.get("deadline_text"), a.get("priority"), a.get("status", "open")),
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
    escaped = _escape_like(source_prefix)
    cur = conn.execute(
        "DELETE FROM topics WHERE source_s3_key LIKE %s ESCAPE '\\'",
        (escaped + '%',),
    )
    return cur.rowcount


def has_topics_for_source_prefix(conn, source_prefix) -> bool:
    """Existence check for the org-api timeline shim (authority-flip Task 4):
    does ANY topic already exist for this (user, date) extraction prefix?
    Used to decide report-verbatim vs. Aurora-rendered per day. Same
    LIKE-wildcard escaping as delete_topics_for_source_prefix (S3 user
    folders contain literal underscores)."""
    escaped = _escape_like(source_prefix)
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT 1 FROM topics WHERE source_s3_key LIKE %s ESCAPE '\\' LIMIT 1",
        (escaped + '%',),
    ).fetchone()
    return row is not None


_TOPIC_COLS_JOINED = (
    "t.id, t.site_id, t.user_id, t.source_s3_key, t.report_date, t.occurred_at, "
    "t.category, t.title, t.summary, t.time_range, t.participants, t.source, t.created_at"
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


def list_topics_for_source_prefix(conn, source_prefix) -> list[dict]:
    """org-api timeline shim read (authority-flip Task 4): all topics whose
    source_s3_key starts with source_prefix (typically
    f"extractions/{user_folder}/{date}/"), so the shim can render the
    daily_report.json shape from extraction-sourced topics for one
    (user, date). Mirrors list_topics_for_date's JOIN + batched-children
    pattern (action_items -- now including deadline_text -- ,
    safety_observations, findings via findings.list_for_topics), PLUS a
    FOURTH batched child, photos (topic_photos) -- the shim needs photos
    per topic and list_topics_for_date never did. Same LIKE-wildcard
    escaping as delete_topics_for_source_prefix/has_topics_for_source_prefix
    (S3 user folders contain literal underscores).

    ORDER BY time_range NULLS LAST, created_at, id gives D3 stable ordering
    (time_range is a free-text display field, not sortable as a real range,
    but groups topics that share one, with created_at/id as tiebreakers for
    topics with no time_range)."""
    escaped = _escape_like(source_prefix)
    topic_rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TOPIC_COLS_JOINED}, "
        f"s.name AS site_name, (u.first_name || ' ' || u.last_name) AS user_name "
        f"FROM topics t "
        f"LEFT JOIN sites s ON s.id = t.site_id "
        f"LEFT JOIN users u ON u.id = t.user_id "
        f"WHERE t.source_s3_key LIKE %s ESCAPE '\\' "
        f"ORDER BY t.time_range NULLS LAST, t.created_at, t.id",
        (escaped + '%',),
    ).fetchall()

    if not topic_rows:
        return []

    topic_ids = [t["id"] for t in topic_rows]
    action_items_by_topic = {}
    for a in conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, text, responsible, deadline, deadline_text, priority, "
        "status, created_at FROM action_items WHERE topic_id = ANY(%s) ORDER BY created_at",
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

    photos_by_topic = {}
    for p in conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, s3_key, caption_text FROM topic_photos "
        "WHERE topic_id = ANY(%s) ORDER BY created_at",
        (topic_ids,),
    ).fetchall():
        photos_by_topic.setdefault(p["topic_id"], []).append(p)

    for t in topic_rows:
        t["action_items"] = action_items_by_topic.get(t["id"], [])
        t["safety_observations"] = safety_by_topic.get(t["id"], [])
        t["findings"] = findings_by_topic.get(t["id"], [])
        t["photos"] = photos_by_topic.get(t["id"], [])

    return topic_rows


def list_extraction_folder_names_for_date(conn, company_id, report_date) -> list[str]:
    """Distinct extraction-sourced folder_name values for one company/date --
    org-api timeline admin-disambiguation (authority-flip Task 4, RETARGET
    override 5's multi-tenant guard). source_s3_key has no company scoping
    of its own (S3 lake paths are folder_name-keyed only, not per-company);
    the users JOIN + company_id filter is what makes this candidate list
    tenant-safe. Read-only -- used only to build the admin
    "available_users" list, never to gate a write. Same LIKE-wildcard
    posture as has_topics_for_source_prefix, but the prefix here is a fixed
    literal ('extractions/'), not caller input, so no _escape_like() call is
    needed -- the '%%' is escaped inline for psycopg's %s paramstyle."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT DISTINCT u.folder_name FROM topics t JOIN users u ON u.id = t.user_id "
        "WHERE t.report_date=%s AND u.company_id=%s "
        "AND t.source_s3_key LIKE 'extractions/%%' AND u.folder_name IS NOT NULL",
        (report_date, company_id),
    ).fetchall()
    return [r["folder_name"] for r in rows]
