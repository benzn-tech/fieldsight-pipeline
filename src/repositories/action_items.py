import uuid

import psycopg
from psycopg.rows import dict_row

from deleted_predicates import visible_topics_predicate
from tenancy_status import not_migrated

# Whitelist of columns PATCH /api/org/action-items/{id} may set. text/site_id/
# topic_id/created_at are intentionally NOT here -- editing a task must never
# re-home it to another site (ACL bypass) or rewrite its body.
_EDITABLE = ("priority", "status", "deadline", "deadline_text", "responsible")
_RET = ("id, topic_id, site_id, text, responsible, deadline, deadline_text, "
        "priority, status, created_at, updated_at, updated_by")

# Display name of the last writer, for the "Checked by <name> · <time>" caption.
# updated_by holds a COGNITO SUB (migration 0017: `updated_by text`), not a
# users.id, so the join is on users.cognito_sub -- NOT users.id.
# NULLIF(TRIM(CONCAT_WS(' ', ...))) is the exact NULL/blank-safe form already
# used by content_edits.list_content_edits and topics.*: CONCAT_WS skips NULL
# parts, so an account with no last_name yields "Ada" (never "Ada "), and an
# account with neither name collapses to NULL instead of "". Do not hand-roll
# `first || ' ' || last` -- that yields NULL for a missing surname.
_UPDATED_BY_NAME = ("NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '') "
                    "AS updated_by_name")


def get_action_item(conn, action_item_id) -> dict | None:
    """One action item joined to its site's company_id (the tenant guard the
    handler checks against caller.company_id). Returns None if not found or
    action_item_id is not a valid UUID -- malformed id == missing (404
    semantics), same posture as observations.get_observation.

    Also carries topic_user_id: who RECORDED the topic this task came from.
    _is_assignee needs it to decide whether an UNASSIGNED task is the caller's
    own work. LEFT JOIN, because the topic row is not guaranteed to outlive the
    action item -- a missing topic yields NULL, which that predicate treats as
    "not mine" (fail-closed)."""
    try:
        row = conn.cursor(row_factory=dict_row).execute(
            "SELECT a.id, a.topic_id, a.site_id, a.text, a.responsible, a.deadline, "
            "a.deadline_text, a.priority, a.status, a.created_at, a.updated_at, "
            "a.updated_by, a.stable_id, s.company_id, t.user_id AS topic_user_id, "
            # The parent topic's provenance ('agent' for an action pack's topic, D2).
            "t.source AS topic_source "
            "FROM action_items a JOIN sites s ON s.id = a.site_id "
            "LEFT JOIN topics t ON t.id = a.topic_id WHERE a.id=%s",
            (action_item_id,),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None
    if row is not None:
        # ADDITIVE (action packs spec §14 D1): the per-site task number, None when the item
        # has none, the table is not migrated yet, or the lookup failed -- never fatal, and
        # its own SAVEPOINT (task_nos_for_item_ids), so the caller's transaction is intact.
        try:
            row["task_no"] = task_nos_for_item_ids(conn, [row["id"]]).get(str(row["id"]))
        except Exception:  # noqa: BLE001
            row["task_no"] = None
    return row


def list_for_carry_forward(conn, topic_ids, site_id) -> list[dict]:
    """Action items on a set of topics -- new OR retired -- shaped for Track B Task 4's
    carry_forward.match: `id`, `text`, `stable_id` plus a computed `human_touched`.
    Batched with ANY(%s), same pattern as findings.list_for_topics; scoped to `site_id`
    (Ruling R9) so a stray topic id can never pull another tenant's -- or another site's --
    rows into the match pool.

    human_touched = updated_by IS NOT NULL OR status <> 'open' -- either half of a person's
    footprint on the row: a status flip with no updated_by (a pre-0017 row, or a write that
    predates the audit column) must still count, and an updated_by with status still 'open'
    (priority/deadline edited without closing it) must too. For a freshly-inserted row
    (the "new" pool) this is always False -- exactly the answer wanted, since nothing has
    touched it yet.
    """
    if not topic_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, text, stable_id, status, priority, deadline, "
        "deadline_text, responsible, updated_by, updated_at, audience, item_id, "
        "(updated_by IS NOT NULL OR status <> 'open') AS human_touched "
        "FROM action_items WHERE topic_id = ANY(%s) AND site_id = %s",
        (list(topic_ids), site_id),
    ).fetchall()


def carry_identity(conn, new_id, old_row) -> None:
    """Carry `old_row`'s stable identity onto the new action item `new_id` that replaced it
    (Track B Task 4). Always moves stable_id/carried_from; when the old row was
    human-touched, also moves the fields a person can set on it (PATCH's own _EDITABLE, plus
    the updated_by/updated_at audit pair and audience) so a tick, a reassignment or a
    deadline edit survives the re-extraction that reworded the row's text around it. A row
    nobody touched carries only its identity -- its status/priority/etc stay whatever the
    fresh extraction just wrote, because there is nothing human to preserve."""
    if old_row.get("human_touched"):
        conn.execute(
            "UPDATE action_items SET stable_id=%s, carried_from=%s, status=%s, priority=%s, "
            "deadline=%s, deadline_text=%s, responsible=%s, updated_by=%s, updated_at=%s, "
            "audience=%s WHERE id=%s",
            (old_row["stable_id"], old_row["id"], old_row["status"], old_row["priority"],
             old_row["deadline"], old_row["deadline_text"], old_row["responsible"],
             old_row["updated_by"], old_row["updated_at"], old_row["audience"], new_id),
        )
    else:
        conn.execute(
            "UPDATE action_items SET stable_id=%s, carried_from=%s WHERE id=%s",
            (old_row["stable_id"], old_row["id"], new_id),
        )


def update_action_item_fields(conn, action_item_id, fields, updated_by) -> dict | None:
    """Whitelisted partial update + last-writer audit (spec §3.6). Only keys
    in _EDITABLE are written, in the caller's own dict order (fields is
    already handler-validated -- this just filters, it doesn't reorder);
    updated_at=now(), updated_by=caller sub. Empty editable set -> None
    without a round-trip. None on missing / malformed uuid (mirrors
    observations.set_status).

    The UPDATE is wrapped in a CTE so the RETURNING row can be LEFT JOINed to
    users for `updated_by_name` -- the resolved display name of the last writer
    -- in ONE round-trip. updated_by alone is a cognito sub, which no UI can
    render; the caption needs a person. NULL when the sub matches no users row
    (e.g. a pre-0017 row, or a login that was never provisioned)."""
    cols = [c for c in fields if c in _EDITABLE]
    if not cols:
        return None
    set_sql = ", ".join(f"{c}=%s" for c in cols) + ", updated_at=now(), updated_by=%s"
    params = [fields[c] for c in cols] + [updated_by, action_item_id]
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"WITH upd AS ("
            f"UPDATE action_items SET {set_sql} WHERE id=%s RETURNING {_RET}"
            f") SELECT upd.*, {_UPDATED_BY_NAME} FROM upd "
            f"LEFT JOIN users u ON u.cognito_sub = upd.updated_by",
            params,
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def insert_item(conn, topic_id, site_id, text, *, responsible=None, deadline=None,
                priority=None) -> dict:
    """One new open action item under `topic_id` (action-pack create_item, spec §5.1).
    `deadline_text` mirrors `deadline`, as PATCH writes it. Returns id, stable_id (the
    database's) and the written fields. No updated_by: the create is audited by the caller's
    content_edits 'created' row, not as a later human edit."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO action_items (topic_id, site_id, text, responsible, deadline, "
        "deadline_text, priority, status) VALUES (%s,%s,%s,%s,%s,%s,%s,'open') "
        "RETURNING id, stable_id, topic_id, site_id, text, responsible, deadline, priority, "
        "status",
        (topic_id, site_id, text, responsible, deadline, deadline, priority),
    ).fetchone()


def _as_uuid(value):
    """`value` as a uuid.UUID, or None when it is not one (None, '', '69', 'abc')."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def get_live_by_stable_id(conn, stable_id, *, for_update=False) -> dict | None:
    """The one LIVE action item addressed by `stable_id` (action packs, spec §14 S12) --
    mirrors topic_questions.get_live_by_stable_id: joined to its topic for
    `visible_topics_predicate` (a superseded pass of the same source key shares this
    stable_id through carry_forward's carried_from chain, and only the live topic's row is
    the one to act on; a deleted topic or recording hides it too) and to `sites` for
    `company_id`, the tenant guard callers check.

    `ORDER BY created_at DESC LIMIT 1`: two rows can share a stable_id for one commit's width
    in a concurrent-pass race; the newest wins, as supersede_topics_for_source already does.

    Also carries the topic's `source_s3_key` (the writer's advisory-lock key, which apply
    takes before writing) and `topic_user_id` (who recorded it, for the assignee check).

    None on missing OR a malformed id. Unlike topic_questions', the malformed case is caught
    in Python before any SQL runs, so it never rolls back: this is called inside the apply
    transaction, where a `conn.rollback()` would discard the caller's own work.

    for_update=True locks the action_items row (FOR UPDATE OF a) until the caller's
    transaction ends: apply's final read, so a concurrent PATCH of the same row either waits
    for apply or has committed before the read (and its content_edits row is then seen)."""
    sid = _as_uuid(stable_id)
    if sid is None:
        return None
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT a.id, a.topic_id, a.site_id, a.stable_id, a.carried_from, a.text, "
        f"a.responsible, a.deadline, a.deadline_text, a.priority, a.status, a.audience, "
        f"a.item_id, a.created_at, a.updated_at, a.updated_by, "
        f"s.company_id, t.source_s3_key, t.user_id AS topic_user_id "
        f"FROM action_items a "
        f"JOIN topics t ON t.id = a.topic_id "
        f"JOIN sites s ON s.id = a.site_id "
        f"WHERE a.stable_id = %s AND {visible_topics_predicate('t')} "
        f"ORDER BY a.created_at DESC LIMIT 1" + (" FOR UPDATE OF a" if for_update else ""),
        (sid,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Task numbers (migration 0093, spec §14 D1): "#69" on the Tasks page, per site,
# tied to stable_id, assigned once and never reused.
#
# LOCK ORDER. The per-site lock below is always the LAST advisory lock a transaction takes:
# the writers take their source lock (hashtext(extraction_key)) and photo_rebind's day lock
# first and number their items at the very end of the pass; apply (action packs Task 7)
# takes the source lock, then this one. Nothing takes another lock after it, so no cycle.
# ---------------------------------------------------------------------------
_SITE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('task_no:' || %s::text))"

# "Not migrated yet" (42P01 undefined_table / 42703 undefined_column -- sam deploy runs before
# migrate) is tenancy_status.not_migrated, the one shared definition.


def lock_site_task_numbers(conn, site_id) -> None:
    """Take the per-site task-number lock (transaction-scoped; re-entrant in one session)."""
    conn.execute(_SITE_LOCK_SQL, (str(site_id),))


def assign_task_no(conn, stable_id, site_id) -> int:
    """The task number of `stable_id`, assigning the site's next one if it has none.

    Runs in the CALLER's open transaction: the number becomes durable when the caller
    commits, and the site lock is held until then, so a second writer on the same site
    waits and then reads this one's max. Idempotent -- an existing stable_id returns its
    number, whatever site asked. The counter is max(task_no)+1 over the ledger, which nothing
    deletes from, so a deleted item's number is never handed out again."""
    lock_site_task_numbers(conn, site_id)
    row = conn.execute("SELECT task_no FROM action_task_numbers WHERE stable_id = %s",
                       (stable_id,)).fetchone()
    if row is not None:
        return row[0]
    return conn.execute(
        "INSERT INTO action_task_numbers (stable_id, site_id, task_no) "
        "SELECT %s, %s, COALESCE(max(task_no), 0) + 1 FROM action_task_numbers "
        "WHERE site_id = %s RETURNING task_no",
        (stable_id, site_id, site_id),
    ).fetchone()[0]


def assign_task_nos_for_topics(conn, topic_ids, site_id) -> int | None:
    """Number every action item on `topic_ids` (this pass's new topics, AFTER carry-forward
    has moved carried stable_ids onto them) whose stable_id has no number yet. Returns how
    many were numbered, or None when migration 0093 is not applied yet.

    Order: insertion order -- created_at, then the topic's position in `topic_ids`, then the
    physical row order (a pass inserts all its rows in one transaction, so created_at ties).

    Savepoint-guarded: this runs inside the existing extraction/report write, which must
    keep working unchanged before migrate (42P01 -> None, nothing written, the caller's
    transaction still usable). Any other error is raised -- after the savepoint has rolled
    back -- for the caller to log and degrade on."""
    if not topic_ids:
        return 0
    ids = list(topic_ids)
    try:
        with conn.transaction():
            rows = conn.execute(
                "SELECT a.stable_id FROM action_items a "
                "WHERE a.topic_id = ANY(%s) AND a.site_id = %s AND NOT EXISTS "
                "(SELECT 1 FROM action_task_numbers n WHERE n.stable_id = a.stable_id) "
                "ORDER BY a.created_at, array_position(%s::uuid[], a.topic_id), a.ctid",
                (ids, site_id, ids),
            ).fetchall()
            pending = list(dict.fromkeys(r[0] for r in rows))
            for stable_id in pending:
                assign_task_no(conn, stable_id, site_id)
            return len(pending)
    except psycopg.Error as exc:
        if not_migrated(exc):
            return None
        raise


def task_nos_for_item_ids(conn, action_item_ids) -> dict:
    """{str(action_items.id): task_no} for the numbered ones among `action_item_ids`, in one
    query -- keyed by the row id every payload already carries, so the existing heavy reads
    stay untouched. Savepoint-guarded; {} when migration 0093 is not applied yet. Any other
    error is raised (after the savepoint rolled back) for the caller to degrade on."""
    ids = [str(i) for i in action_item_ids if i]
    if not ids:
        return {}
    try:
        with conn.transaction():
            rows = conn.execute(
                "SELECT a.id, n.task_no FROM action_items a "
                "JOIN action_task_numbers n ON n.stable_id = a.stable_id "
                "WHERE a.id = ANY(%s::uuid[])",
                (ids,),
            ).fetchall()
    except psycopg.Error as exc:
        if not_migrated(exc):
            return {}
        raise
    return {str(r[0]): r[1] for r in rows}


def _as_task_no(value):
    """A positive int task number from an int or a digit string ("69"); None for anything
    else -- bool (True is an int in Python), float, "6.9", "abc", "-1", 0, > int4."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        no = value
    elif isinstance(value, str) and value.strip().isdecimal():
        no = int(value.strip())
    else:
        return None
    # task_no is a Postgres integer: a larger value can name no row, and would raise.
    return no if 0 < no <= 2147483647 else None


def get_live_by_task_no(conn, site_id, task_no) -> dict | None:
    """The live action item numbered `task_no` on `site_id` ("#69" in an instruction):
    the ledger's stable_id, then get_live_by_stable_id. None when the number is unknown,
    its item is no longer live, or either argument is malformed."""
    sid = _as_uuid(site_id)
    no = _as_task_no(task_no)
    if sid is None or no is None:
        return None
    row = conn.execute(
        "SELECT stable_id FROM action_task_numbers WHERE site_id = %s AND task_no = %s",
        (sid, no),
    ).fetchone()
    if row is None:
        return None
    item = get_live_by_stable_id(conn, row[0])
    if item is None or str(item["site_id"]) != str(sid):
        return None
    return item
