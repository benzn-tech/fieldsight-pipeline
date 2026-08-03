"""Repository for the programme tables (migration 0027).

Style mirrors src/repositories/programme_suggestions.py: module-level SQL,
conn.cursor(row_factory=dict_row).execute(...).fetchone()/.fetchall().

Two invariants are enforced here rather than in the handler, so no future
caller can bypass them:

  * update_task writes only allow-listed columns. A PATCH body is client
    input; letting it name columns freely would let a caller move a task to
    another programme or flip an imported row to local.
  * delete_local_task carries `origin = 'local'` in its WHERE. Imported rows
    are the file's, and only an import may remove them.
"""
from psycopg.rows import dict_row

_TASK_COLS = (
    "id, programme_id, source_task_id, parent_id, origin, name, wbs_code, "
    "start_date, end_date, duration_days, progress_pct, status, zone, "
    "total_float_days, is_critical, first_seen_version, removed_in_version, "
    "locally_modified, sort_order, created_at, updated_at, updated_by, row_version"
)

_PROGRAMME_COLS = (
    "id, site_id, name, source_format, current_version, baseline_version, "
    "is_primary, status, created_at, updated_at"
)

# Columns a PATCH may address. Deliberately excludes programme_id, parent_id,
# origin, source_task_id, first_seen_version, removed_in_version and
# row_version — those are structural, and are set by import or by create.
_UPDATABLE = frozenset({
    "name", "start_date", "end_date", "duration_days",
    "progress_pct", "status", "zone", "sort_order",
})


def get_primary_programme(conn, site_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_PROGRAMME_COLS} FROM programmes "
        f"WHERE site_id = %s AND status = 'active' AND is_primary "
        f"LIMIT 1",
        (site_id,),
    ).fetchone()


def get_primary_programme_by_id(conn, programme_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_PROGRAMME_COLS} FROM programmes WHERE id = %s",
        (programme_id,),
    ).fetchone()


def create_programme(conn, *, site_id, name, source_format) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO programmes (site_id, name, source_format) "
        f"VALUES (%s,%s,%s) RETURNING {_PROGRAMME_COLS}",
        (site_id, name, source_format),
    ).fetchone()


def record_version(conn, programme_id, *, version_no, filename, mode,
                   imported_by) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO programme_versions "
        "(programme_id, version_no, filename, mode, imported_by) "
        "VALUES (%s,%s,%s,%s,%s) "
        "RETURNING id, programme_id, version_no, filename, mode, "
        "imported_by, imported_at, diff_summary",
        (programme_id, version_no, filename, mode, imported_by),
    ).fetchone()


def list_tasks(conn, programme_id, *, include_removed=False) -> list[dict]:
    where = "programme_id = %s"
    if not include_removed:
        where += " AND removed_in_version IS NULL"
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TASK_COLS} FROM programme_tasks "
        f"WHERE {where} "
        f"ORDER BY sort_order, wbs_code NULLS LAST, created_at",
        (programme_id,),
    ).fetchall()


def get_task(conn, task_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TASK_COLS} FROM programme_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()


def get_task_by_doc_id(conn, programme_id, doc_id) -> dict | None:
    """Resolve a programme.json / suggestion `task_id` back to its row.

    Mirrors programme_snapshot._doc_id, which emits source_task_id for
    imported rows and the UUID string for local ones. The two identifier
    spaces share one text column in programme_progress_suggestions, so this
    has to try both.

    Imported wins the (vanishingly unlikely) collision between a file's
    Activity ID and another row's UUID text: a suggestion is far more likely
    to be carrying the file's identifier. NULLS LAST matters — a local row's
    source_task_id is NULL, so the comparison is NULL, and a plain DESC would
    sort it to the front and beat the exact imported match.
    """
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TASK_COLS} FROM programme_tasks "
        f"WHERE programme_id = %s AND removed_in_version IS NULL "
        f"AND (source_task_id = %s OR id::text = %s) "
        f"ORDER BY (source_task_id = %s) DESC NULLS LAST LIMIT 1",
        (programme_id, doc_id, doc_id, doc_id),
    ).fetchone()


def create_task(conn, *, programme_id, parent_id, name, wbs_code, start_date,
                end_date, duration_days, status, zone, sort_order,
                updated_by) -> dict:
    """Always origin='local'. Only an import mints imported rows."""
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO programme_tasks ("
        f"programme_id, parent_id, origin, name, wbs_code, start_date, end_date, "
        f"duration_days, status, zone, sort_order, updated_by) "
        f"VALUES (%s,%s,'local',%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        f"RETURNING {_TASK_COLS}",
        (programme_id, parent_id, name, wbs_code, start_date, end_date,
         duration_days, status, zone, sort_order, updated_by),
    ).fetchone()


def update_task(conn, task_id, *, fields: dict, row_version: int,
                updated_by) -> dict | None:
    """Optimistic-locked partial update.

    Returns the updated row, or None when the WHERE matched nothing — either
    the task is gone or another writer moved it first. The caller turns None
    into a 409; it must never be treated as success.
    """
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"non-updatable programme task columns: {sorted(bad)}")
    if not fields:
        raise ValueError("no fields to update")

    sets = [f"{col} = %s" for col in fields]
    params = list(fields.values())

    # An edit to an imported row is surfaced in the next import's diff rather
    # than being silently overwritten. Local rows are ours already, so the
    # flag would mean nothing there.
    sets.append("locally_modified = (origin = 'imported')")
    sets.append("updated_by = %s")
    params.append(updated_by)
    sets.append("updated_at = now()")
    sets.append("row_version = row_version + 1")

    params.extend([task_id, row_version])
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE programme_tasks SET {', '.join(sets)} "
        f"WHERE id = %s AND row_version = %s "
        f"RETURNING {_TASK_COLS}",
        tuple(params),
    ).fetchone()


def delete_local_task(conn, task_id) -> bool:
    """Hard-delete, local rows only. Imported rows are soft-deleted by import
    reconciliation and are never removed through this path."""
    row = conn.cursor(row_factory=dict_row).execute(
        "DELETE FROM programme_tasks WHERE id = %s AND origin = 'local' "
        "RETURNING id",
        (task_id,),
    ).fetchone()
    return row is not None


def count_local_tasks(conn, programme_id) -> int:
    """How many rows under this programme are ours rather than the client's.

    replace_all_tasks discards them, which is correct for a replace and
    catastrophic for a zone split or an AI breakdown. The caller uses this to
    refuse rather than to warn: an assumption written in a docstring and not
    enforced is what made this reachable in the first place.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT count(*) AS n FROM programme_tasks "
        "WHERE programme_id = %s AND origin = 'local' "
        "AND removed_in_version IS NULL",
        (programme_id,),
    ).fetchone()
    return int((row or {}).get("n") or 0)


def replace_all_tasks(conn, programme_id, *, parents, leaves, version_no,
                      updated_by) -> int:
    """Whole-document save that PRESERVES local rows.

    Imported rows are the client's and are rebuilt from the payload. Local
    rows -- zone splits, AI breakdowns, anything allocated to a person -- are
    ours, and a save must not destroy them: that path silently converted them
    to imported and let the next import remove them as departed
    (fieldsight-ui#186).

    What makes this possible is that local rows already carry identity in the
    payload: programme_snapshot._doc_id emits their UUID as `task_id`, so a
    payload leaf can be matched back to the row it came from without a
    source_task_id.

    Five steps, in order:
      1. remember each live local row and the source_task_id of its parent --
         the only durable link across the rebuild, since imported rows are
         re-minted with new UUIDs
      2. delete IMPORTED rows only
      3. re-insert imported rows, building source_task_id -> new uuid
      4. update each surviving local row from the payload and re-point its
         parent through that map
      5. delete local rows the payload no longer contains -- the user removed
         them in the UI, and silently keeping them would resurrect work
         somebody deleted

    Returns the number of task rows written.
    """
    # 1. Local rows, and what their parent was called in the FILE. Reading the
    #    parent's source_task_id rather than its uuid is the whole trick: the
    #    uuid does not survive step 2, the file's id does.
    locals_before = conn.cursor(row_factory=dict_row).execute(
        "SELECT t.id, p.source_task_id AS parent_source "
        "FROM programme_tasks t "
        "LEFT JOIN programme_tasks p ON p.id = t.parent_id "
        "WHERE t.programme_id = %s AND t.origin = 'local' "
        "AND t.removed_in_version IS NULL",
        (programme_id,),
    ).fetchall()
    local_ids = {str(r["id"]) for r in locals_before}

    # 2a. DETACH the local rows first. programme_tasks.parent_id is
    #     `REFERENCES programme_tasks(id) ON DELETE CASCADE` (migration 0027),
    #     so deleting an imported parent takes its local children with it --
    #     scoping the DELETE to origin='imported' is not enough on its own.
    #
    #     This was caught only by running the rewrite against real PostgreSQL;
    #     the unit tests pass either way, because the connection double does
    #     not enforce foreign keys. Step 1 has already recorded where each of
    #     these rows belongs, so cutting the link is safe.
    if local_ids:
        conn.cursor().execute(
            "UPDATE programme_tasks SET parent_id = NULL "
            "WHERE programme_id = %s AND origin = 'local' "
            "AND removed_in_version IS NULL",
            (programme_id,))

    # 2b. Imported only. Local rows now survive the rebuild in place, keeping
    #     their uuid -- which is what assignees and recorded progress hang off.
    conn.cursor().execute(
        "DELETE FROM programme_tasks WHERE programme_id = %s "
        "AND origin = 'imported'",
        (programme_id,))

    # Groups first: the file expresses parentage with its own ids, and the
    # rows have to be linked by our uuids, so a group must exist before any
    # leaf can reference it.
    by_source = {}
    order = 0
    for p in parents or []:
        row = conn.cursor(row_factory=dict_row).execute(
            "INSERT INTO programme_tasks ("
            "programme_id, source_task_id, parent_id, origin, name, wbs_code, "
            "first_seen_version, sort_order, updated_by) "
            "VALUES (%s,%s,NULL,'imported',%s,%s,%s,%s,%s) RETURNING id",
            (programme_id, p["task_id"], p.get("name") or p["task_id"],
             p.get("wbs"), version_no, order, updated_by),
        ).fetchone()
        by_source[p["task_id"]] = row["id"]
        order += 1

    first_group_id = next(iter(by_source.values()), None)

    for t in leaves or []:
        # A payload leaf whose task_id is a known local uuid is one of OUR
        # rows coming back. Re-inserting it here would mint a duplicate and
        # stamp it 'imported' -- the exact bug this rewrite exists to remove.
        if str(t.get("task_id")) in local_ids:
            continue
        conn.cursor().execute(
            "INSERT INTO programme_tasks ("
            "programme_id, source_task_id, parent_id, origin, name, wbs_code, "
            "start_date, end_date, duration_days, progress_pct, status, "
            "first_seen_version, sort_order, updated_by) "
            "VALUES (%s,%s,%s,'imported',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (programme_id, t["task_id"], by_source.get(t.get("parent_id")),
             t.get("name") or t["task_id"], t.get("wbs"),
             t.get("start"), t.get("end"), t.get("duration_days"),
             t.get("progress_pct") or 0, t.get("status") or "not_started",
             version_no, order, updated_by),
        )
        order += 1

    # 4 & 5. Local rows: update from the payload, or delete if the payload
    #        dropped them.
    payload_by_id = {str(t.get("task_id")): t for t in (leaves or [])}
    kept = 0
    for row in locals_before:
        rid = str(row["id"])
        payload = payload_by_id.get(rid)
        if payload is None:
            # Removed in the UI. Hard delete: local rows are ours, and
            # soft-deletion is reserved for imported rows an import dropped.
            conn.cursor().execute(
                "DELETE FROM programme_tasks WHERE id = %s AND origin = 'local'",
                (row["id"],))
            continue

        # An orphan: the parent was removed from the file in this same save,
        # so no new uuid was minted for it. Re-parent to the first group
        # rather than orphaning -- this work is allocated to a person and must
        # not vanish because the client reorganised their WBS.
        new_parent = by_source.get(row["parent_source"]) or first_group_id

        # The payload is the user's INTENT; the database is only the source of
        # identity. Ignoring the payload here would be safe for identity and
        # wrong for content -- an edit to a zone's dates would vanish on save.
        conn.cursor().execute(
            "UPDATE programme_tasks SET parent_id = %s, name = %s, "
            "wbs_code = %s, start_date = %s, end_date = %s, "
            "duration_days = %s, progress_pct = %s, status = %s, "
            "updated_by = %s, updated_at = now(), "
            "row_version = row_version + 1 "
            "WHERE id = %s AND origin = 'local'",
            (new_parent, payload.get("name") or rid, payload.get("wbs"),
             payload.get("start"), payload.get("end"),
             payload.get("duration_days"), payload.get("progress_pct") or 0,
             payload.get("status") or "not_started", updated_by, row["id"]))
        kept += 1

    conn.cursor().execute(
        "UPDATE programmes SET current_version = %s, updated_at = now() "
        "WHERE id = %s",
        (version_no, programme_id))

    imported_written = len(parents or []) + len(
        [t for t in (leaves or []) if str(t.get("task_id")) not in local_ids])
    return imported_written + kept


def list_assignees(conn, task_ids) -> dict:
    """{task_id: [assignee, ...]}. Empty input returns {} without querying —
    building `IN ()` is a syntax error, and dropping the filter instead would
    return every assignee in the database."""
    if not task_ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT task_id, assignee FROM programme_task_assignees "
        "WHERE task_id = ANY(%s) ORDER BY assignee",
        (list(task_ids),),
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(str(r["task_id"]), []).append(r["assignee"])
    return out


def set_assignees(conn, task_id, assignees) -> None:
    conn.cursor().execute(
        "DELETE FROM programme_task_assignees WHERE task_id = %s", (task_id,))
    for a in assignees or []:
        conn.cursor().execute(
            "INSERT INTO programme_task_assignees (task_id, assignee) "
            "VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (task_id, a))
