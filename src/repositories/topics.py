from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from repositories import findings

_TOPIC_COLS = ("id, site_id, user_id, source_s3_key, report_date, occurred_at, "
               "category, title, summary, time_range, participants, source, created_at, "
               "work_class, work_confidence, is_mixed")

# Phase F (D8 retirement, spec §8): severity -> risk_level, for reshaping
# safety-domain findings into the legacy safety_observations row shape.
# Mirrors lambda_org_api._SEV_TO_RISK / lambda_extract_session._SEV_TO_RISK
# (kept as a small local copy -- importing from lambda_org_api would be
# circular, since it imports this module).
_SEV_TO_RISK = {"major": "high", "minor": "medium", "none": "low"}


def _findings_as_safety_rows(topic_findings):
    """Reshape one topic's safety-domain findings into the legacy
    safety_observations row shape ({id, topic_id, observation, risk_level,
    location, status, created_at}) so existing consumers of the
    safety_observations child slot keep working unchanged while the
    underlying source of truth becomes `findings` (Phase F / D8 retirement,
    spec §8: a corrected finding.observation must reach this slot
    immediately, not the unlinked legacy dual-write copy). location has no
    findings equivalent -- always None (findings has no location column).
    All access is defensive .get() -- findings dicts vary by call site."""
    return [{
        "id": f.get("id"), "topic_id": f.get("topic_id"), "observation": f.get("observation"),
        "risk_level": _SEV_TO_RISK.get(f.get("severity"), "medium"),
        "location": None, "status": f.get("status"), "created_at": f.get("created_at"),
    } for f in topic_findings if f.get("domain") == "safety"]


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
                 time_range=None, participants=None,
                 work_class=None, work_confidence=None, is_mixed=False) -> dict:
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
        f"category, title, summary, time_range, participants, "
        f"work_class, work_confidence, is_mixed) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_TOPIC_COLS}",
        (site_id, user_id, source_s3_key, report_date, occurred_at, category, title, summary,
         time_range, Jsonb(participants) if participants is not None else None,
         work_class, work_confidence, is_mixed),
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


def list_contributor_folders_for_site_date(conn, site_id, report_date) -> list[str]:
    """Distinct S3 recording-folder names whose topics are attributed to this
    (site_id, report_date) -- the UNION source for the site-aggregated timeline
    fan-out.

    A topic's folder is the recorder folder encoded in source_s3_key
    ('extractions/{folder}/{date}/...' or 'reports/{date}/{folder}/...'), NOT
    user_id: G5b attribution resolves the AUTHOR/site, but the /timeline read is
    served per S3 FOLDER. A non-member recorder (e.g. an admin whose recording
    was site-tagged via recordings.site_id) has topics attributed here yet is
    absent from memberships, so a members-only fan-out drops them -- exactly the
    aggregation gap this closes. Members ∪ these folders = the correct fan-out.

    Literal '%' in the LIKE patterns is doubled ('%%') because the query carries
    positional params (psycopg %-style)."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT DISTINCT CASE "
        "  WHEN source_s3_key LIKE 'extractions/%%' THEN split_part(source_s3_key, '/', 2) "
        "  WHEN source_s3_key LIKE 'reports/%%'     THEN split_part(source_s3_key, '/', 3) "
        "END AS folder "
        "FROM topics WHERE site_id=%s AND report_date=%s",
        (site_id, report_date),
    ).fetchall()
    return sorted({r["folder"] for r in rows if r["folder"]})


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
    "t.category, t.title, t.summary, t.time_range, t.participants, t.source, t.created_at, "
    "t.work_class, t.work_confidence, t.is_mixed"
)


def list_topics_for_date(conn, site_ids, report_date, *, author_ids=None) -> list[dict]:
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

    Phase F (D8 retirement, spec §8): the `safety_observations` child slot
    itself is now SOURCED from safety-domain findings first (via
    _findings_as_safety_rows, reusing the already-fetched findings -- no
    extra query), falling back to the raw safety_observations rows for a
    topic ONLY when it has zero safety-domain findings (pre-#46 legacy
    extractions that predate the findings table). This makes the /live-items
    dashboard feed -- which the frontend reads `topic.safety_observations`
    off of directly, with no findings-aware merge of its own -- reflect a
    finding.observation correction immediately instead of the unlinked
    dual-write copy. The safety_observations query itself is kept (not
    removed) precisely to preserve that legacy-topic fallback.

    author_ids (visibility spec §3.1 user_scope, Phase 3 graded roles)
    optionally restricts results to topics whose t.user_id is in the
    caller's resolved allow-set; None (default) = no per-author filter,
    today's behavior unchanged. A topic with a NULL user_id (unattributed
    report row) is deliberately EXCLUDED when a filter is active --
    fail-closed, no unattributed row leaks into a SELF/SELF+WORKERS feed.

    Empty site_ids -> [] without a round-trip (mirrors sites.list_sites_by_ids)."""
    if not site_ids:
        return []

    where = "WHERE t.site_id = ANY(%s) AND t.report_date=%s"
    params = [list(site_ids), report_date]
    if author_ids is not None:
        where += " AND t.user_id = ANY(%s::uuid[])"
        params.append(list(author_ids))

    topic_rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TOPIC_COLS_JOINED}, "
        f"s.name AS site_name, (u.first_name || ' ' || u.last_name) AS user_name "
        f"FROM topics t "
        f"LEFT JOIN sites s ON s.id = t.site_id "
        f"LEFT JOIN users u ON u.id = t.user_id "
        f"{where} "
        f"ORDER BY t.occurred_at NULLS LAST, t.created_at",
        tuple(params),
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
        t_findings = findings_by_topic.get(t["id"], [])
        # Phase F / D8 retirement (spec §8): findings-first, legacy-fallback.
        t["safety_observations"] = (_findings_as_safety_rows(t_findings)
                                    or safety_by_topic.get(t["id"], []))
        t["findings"] = t_findings
        t["is_live"] = bool(t["source_s3_key"]) and t["source_s3_key"].startswith("extractions/")

    return topic_rows


def list_report_dates(conn, site_ids, since_date, *, author_ids=None) -> list:
    """Distinct report_date values (ascending) for a caller-computed ACL
    site-id set, on or after since_date. Backs org-api GET /api/org/dates —
    the membership-scoped replacement for legacy get_dates' S3 folder scan,
    which had no ?site access check and leaked cross-user/cross-company
    report-dates (visibility spec §1.1 dots leak). site_ids is the SAME
    kind of caller-scoped list list_topics_for_date takes (ALL company
    sites for admin/gm, else memberships.accessible_site_ids); the ::uuid[]
    cast accepts the str ids _allowed_site_ids/_resolve_site_param hand back.

    author_ids (visibility spec §3.1, Phase 3 graded roles) optionally
    restricts to dates carrying at least one topic authored by an id in the
    caller's allow-set; None (default) = no per-author filter, today's
    behavior unchanged. Same fail-closed NULL-user_id exclusion as
    list_topics_for_date when a filter is active.

    Empty site_ids -> [] without a round-trip (mirrors list_topics_for_date)."""
    if not site_ids:
        return []
    where = "WHERE site_id = ANY(%s::uuid[]) AND report_date >= %s"
    params = [list(site_ids), since_date]
    if author_ids is not None:
        where += " AND user_id = ANY(%s::uuid[])"
        params.append(list(author_ids))
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT DISTINCT report_date FROM topics "
        f"{where} "
        f"ORDER BY report_date",
        tuple(params),
    ).fetchall()
    return [r["report_date"] for r in rows]


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
    topics with no time_range).

    Phase F (D8 retirement, spec §8): the safety_observations child slot
    itself is findings-first with a per-topic legacy fallback, same
    _findings_as_safety_rows rule as list_topics_for_date -- see that
    docstring. render_report_shape (the shim's own consumer) already layers
    a findings-vs-safety_observations preference of its own on top of this;
    this fix makes that layering redundant-but-harmless rather than
    load-bearing (NOTE: get_topic_full, below, is a separate single-topic
    read used only by the reindex builder and is intentionally NOT changed
    here -- out of this task's scoped call sites)."""
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
        t_findings = findings_by_topic.get(t["id"], [])
        # Phase F / D8 retirement (spec §8): findings-first, legacy-fallback.
        t["safety_observations"] = (_findings_as_safety_rows(t_findings)
                                    or safety_by_topic.get(t["id"], []))
        t["findings"] = t_findings
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


def get_topic_full(conn, topic_id) -> dict | None:
    """One topic row (joined site_name/user_name) plus its action_items /
    safety_observations / findings / photos children, shaped EXACTLY like a
    list_topics_for_source_prefix element so render_report_shape can consume
    [row]. Used by the per-topic reindex builder (reindex.enqueue_topic_
    reindex). Returns None if the id is missing/malformed."""
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TOPIC_COLS_JOINED}, "
        f"s.name AS site_name, (u.first_name || ' ' || u.last_name) AS user_name "
        f"FROM topics t "
        f"LEFT JOIN sites s ON s.id = t.site_id "
        f"LEFT JOIN users u ON u.id = t.user_id "
        f"WHERE t.id=%s",
        (topic_id,),
    ).fetchall()
    if not rows:
        return None
    t = rows[0]
    tids = [t["id"]]
    t["action_items"] = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, text, responsible, deadline, deadline_text, "
        "priority, status, created_at FROM action_items WHERE topic_id = ANY(%s) "
        "ORDER BY created_at", (tids,)).fetchall()
    t["safety_observations"] = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, observation, risk_level, location, status, "
        "created_at FROM safety_observations WHERE topic_id = ANY(%s) "
        "ORDER BY created_at", (tids,)).fetchall()
    t["findings"] = findings.list_for_topics(conn, tids)
    t["photos"] = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, s3_key, caption_text FROM topic_photos "
        "WHERE topic_id = ANY(%s) ORDER BY created_at", (tids,)).fetchall()
    return t
