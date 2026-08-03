"""Applies a reconciliation plan, and owns version history, rollback and
baseline selection.

Spec: fieldsight-ui/docs/superpowers/specs/2026-08-02-programme-foundation-design.md §6.5

Everything here runs inside the request transaction (lambda_org_api wraps each
request in `with get_connection() as conn:`), so a plan is applied whole or not
at all. A half-applied reconciliation would be worse than a failed one: the
programme would disagree with the file in a way nobody could see.

Nothing in this module deletes a task row. Departures are stamped with
`removed_in_version`, because allocations, recorded progress and local
breakdown subtrees hang off rows the file may drop.
"""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def apply_plan(conn, programme_id, plan, *, version_no, updated_by) -> dict:
    """Returns {inserted, updated, removed} — what was actually written."""
    inserted = updated = removed = 0

    # Groups first. The file expresses parentage with its own ids while the
    # rows link by our uuids, so a leaf written before its group would have
    # nothing to point at.
    src_to_uuid = {}
    for row in plan["insert"]:
        if row.get("parent_source_id") is not None:
            continue
        new = conn.cursor(row_factory=dict_row).execute(
            "INSERT INTO programme_tasks ("
            "programme_id, source_task_id, parent_id, origin, name, wbs_code, "
            "start_date, end_date, duration_days, first_seen_version, updated_by) "
            "VALUES (%s,%s,NULL,'imported',%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (programme_id, row["source_task_id"], row["name"], row.get("wbs_code"),
             row.get("start_date"), row.get("end_date"), row.get("duration_days"),
             row["first_seen_version"], updated_by),
        ).fetchone()
        src_to_uuid[row["source_task_id"]] = new["id"]
        inserted += 1

    for row in plan["insert"]:
        if row.get("parent_source_id") is None:
            continue
        parent_uuid = src_to_uuid.get(row["parent_source_id"])
        if parent_uuid is None:
            # The parent already existed from an earlier version.
            got = conn.cursor(row_factory=dict_row).execute(
                "SELECT id FROM programme_tasks "
                "WHERE programme_id = %s AND source_task_id = %s",
                (programme_id, row["parent_source_id"]),
            ).fetchone()
            parent_uuid = got["id"] if got else None
        conn.cursor().execute(
            "INSERT INTO programme_tasks ("
            "programme_id, source_task_id, parent_id, origin, name, wbs_code, "
            "start_date, end_date, duration_days, first_seen_version, updated_by) "
            "VALUES (%s,%s,%s,'imported',%s,%s,%s,%s,%s,%s,%s)",
            (programme_id, row["source_task_id"], parent_uuid, row["name"],
             row.get("wbs_code"), row.get("start_date"), row.get("end_date"),
             row.get("duration_days"), row["first_seen_version"], updated_by),
        )
        inserted += 1

    for row in plan["update"]:
        fields = row["fields"]
        if not fields:
            continue
        sets = [f"{c} = %s" for c in fields]
        params = list(fields.values())
        # The import is the authority for these columns, so the local-edit
        # flag clears: the file and the row agree again. Leaving it set would
        # warn about an overwrite on every future import.
        sets.append("locally_modified = false")
        sets.append("updated_at = now()")
        sets.append("row_version = row_version + 1")
        params.append(row["id"])
        conn.cursor().execute(
            f"UPDATE programme_tasks SET {', '.join(sets)} WHERE id = %s",
            tuple(params))
        updated += 1

    for row in plan["remove"]:
        # Soft. See the module docstring.
        conn.cursor().execute(
            "UPDATE programme_tasks SET removed_in_version = %s, "
            "updated_at = now(), row_version = row_version + 1 WHERE id = %s",
            (row["removed_in_version"], row["id"]))
        removed += 1

    return {"inserted": inserted, "updated": updated, "removed": removed}


def apply_rename(conn, existing_id, new_source_task_id) -> bool:
    """Repair for a planner changing an Activity ID.

    One column. The row's allocations, recorded progress and local subtree are
    untouched — which is the entire reason identity and matching keys live in
    separate columns.

    Refuses a local row: migration 0027's CHECK ties `origin='local'` to a NULL
    source id, so giving one a file identity would make the row
    unrepresentable.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "UPDATE programme_tasks SET source_task_id = %s, updated_at = now() "
        "WHERE id = %s AND origin = 'imported' RETURNING id",
        (new_source_task_id, existing_id),
    ).fetchone()
    return row is not None


def list_versions(conn, programme_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, programme_id, version_no, filename, mode, imported_by, "
        "imported_at, diff_summary FROM programme_versions "
        "WHERE programme_id = %s ORDER BY version_no DESC",
        (programme_id,),
    ).fetchall()


def _iso(v):
    if v is None:
        return None
    return v if isinstance(v, str) else v.isoformat()


def build_task_snapshot(tasks) -> list:
    """The dated task set as it stands, for storing on the version row.

    programme_tasks.start_date/end_date are overwritten IN PLACE by every
    import, and first_seen_version / removed_in_version record only which
    tasks EXISTED at version N. Without this, lateness against a baseline
    cannot be computed at all unless the baseline is the current version —
    the one case where the answer is always zero, which is why the gap stayed
    invisible until Project 2 needed the number.

    Excludes three kinds of row, each for its own reason:
      soft-removed — not part of what this import agreed to
      local        — ours, not the client's; a breakdown subtask running past
                     the contract end would silently inflate the baseline
      undated      — cannot contribute to a finish date

    Compact keys because this is stored per version and read whole:
      i = source_task_id, s = start, e = end, d = duration_days
    """
    out = []
    for t in tasks or []:
        if t.get("removed_in_version") is not None:
            continue
        if t.get("origin") != "imported":
            continue
        start, end = _iso(t.get("start_date")), _iso(t.get("end_date"))
        if not start or not end:
            continue
        out.append({"i": t.get("source_task_id"), "s": start, "e": end,
                    "d": t.get("duration_days")})
    return out


def get_version_tasks(conn, programme_id, version_no):
    """The stored snapshot for one version, or None when there is no such
    version. None and [] are different answers — "no such version" versus
    "that version had no dated tasks" — and the caller renders them
    differently."""
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT task_snapshot FROM programme_versions "
        "WHERE programme_id = %s AND version_no = %s",
        (programme_id, version_no),
    ).fetchone()
    return None if row is None else (row.get("task_snapshot") or [])


def record_version(conn, programme_id, *, version_no, filename, mode,
                   imported_by, diff_summary, task_snapshot=None) -> dict:
    """`task_snapshot` is optional because restore_version writes a version
    row for the state it is leaving and has no task set of its own to
    snapshot — it must not be forced to invent one."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO programme_versions "
        "(programme_id, version_no, filename, mode, imported_by, diff_summary, "
        " task_snapshot) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "RETURNING id, programme_id, version_no, filename, mode, imported_by, "
        "imported_at, diff_summary",
        (programme_id, version_no, filename, mode, imported_by,
         Jsonb(diff_summary or {}), Jsonb(task_snapshot or [])),
    ).fetchone()


def restore_version(conn, programme_id, version_no, *, restored_by) -> dict:
    """Roll the task set back to how it stood at `version_no`.

    A version row is recorded for the state being LEFT, so the rollback is
    itself reversible. Without that, an accidental restore destroys the thing
    version history exists to protect.

    The rollback works by flipping `removed_in_version`, never by deleting or
    reinserting, so no history is lost in either direction: rows that had not
    arrived yet go back to removed, rows removed after the target come back.
    """
    target = conn.cursor(row_factory=dict_row).execute(
        "SELECT version_no FROM programme_versions "
        "WHERE programme_id = %s AND version_no = %s",
        (programme_id, version_no),
    ).fetchone()
    if target is None:
        raise ValueError(f"no such version: {version_no}")

    conn.cursor().execute(
        "UPDATE programme_tasks SET removed_in_version = %s "
        "WHERE programme_id = %s AND origin = 'imported' "
        "AND first_seen_version > %s AND removed_in_version IS NULL",
        (version_no, programme_id, version_no))
    conn.cursor().execute(
        "UPDATE programme_tasks SET removed_in_version = NULL "
        "WHERE programme_id = %s AND origin = 'imported' "
        "AND removed_in_version > %s",
        (programme_id, version_no))

    cur = conn.cursor(row_factory=dict_row).execute(
        "SELECT current_version FROM programmes WHERE id = %s",
        (programme_id,)).fetchone()
    next_version = ((cur or {}).get("current_version") or version_no) + 1

    conn.cursor().execute(
        "UPDATE programmes SET current_version = %s, updated_at = now() "
        "WHERE id = %s", (next_version, programme_id))

    return record_version(
        conn, programme_id, version_no=next_version, filename=None,
        mode="replace", imported_by=restored_by,
        diff_summary={"restored_from": version_no})


def set_baseline(conn, programme_id, version_no) -> dict:
    """The contractually approved revision, which is often not the first
    import. Lateness is measured against it, and measuring against the wrong
    one is worse than not measuring at all."""
    exists = conn.cursor(row_factory=dict_row).execute(
        "SELECT version_no FROM programme_versions "
        "WHERE programme_id = %s AND version_no = %s",
        (programme_id, version_no),
    ).fetchone()
    if exists is None:
        raise ValueError(f"no such version: {version_no}")
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE programmes SET baseline_version = %s, updated_at = now() "
        "WHERE id = %s RETURNING id, baseline_version",
        (version_no, programme_id),
    ).fetchone()
