"""Report templates, their versions, and which one the schedule uses. Migration 0062.

Read and written only by org-api. Repositories never commit -- the caller owns
the transaction.

TWO RULES LIVE HERE RATHER THAN IN THE ROUTES, because a route is one caller
and a rule with one caller today has two tomorrow:

  * A version is never rewritten. `add_version` allocates the next number
    inside the same statement that inserts it, so two writers racing produce
    versions 4 and 5, never one clobbered 4. A restore is `add_version` with an
    older body -- there is no path that moves `current_version` backwards.

  * Visibility is a WHERE clause, not a filter applied afterwards. A personal
    template belongs to one person and is invisible to everyone else in the
    company INCLUDING gm/admin; that is enforced in SQL so a caller cannot
    forget it. `list_visible` takes the caller's own id for exactly this
    reason and has no "show me everything" mode.

WHAT THIS MODULE DOES NOT DO: decide who may write. That is the routes' job,
because the answer depends on the caller's global_role and on cross-company
reach, and both belong with the request. This module will happily archive a
template the caller had no business archiving.
"""
import json

from psycopg.rows import dict_row

_TEMPLATE_COLS = (
    "id, company_id, scope, owner_user_id, slug, name, description, "
    "report_type, current_version, archived_at, created_by, created_at, updated_at"
)

# How many sections the current version has, as a correlated subquery rather
# than a second round trip per row. The Library shows "N sections" on every
# row, and fetching each row's body to count them would be one query per
# template for a number.
_SECTION_COUNT = (
    "(SELECT jsonb_array_length(v.body->'sections') "
    " FROM report_template_versions v "
    " WHERE v.template_id = report_templates.id "
    "   AND v.version = report_templates.current_version) AS section_count"
)


def _decoded(row, key="body"):
    """psycopg hands jsonb back as str on some paths and dict on others."""
    if row is not None and isinstance(row.get(key), str):
        row[key] = json.loads(row[key])
    return row


# ---- reading ---------------------------------------------------------------

def list_visible(conn, company_id, user_id, scope=None):
    """Every template this caller may see: the company's org templates plus
    their OWN personal ones. Never anybody else's personal ones.

    `scope` narrows to one side; omitting it returns the union, which is what
    the Library's "All" tab shows.
    """
    where = ["company_id = %s", "archived_at IS NULL",
             "(scope = 'org' OR owner_user_id = %s)"]
    args = [str(company_id), str(user_id)]
    if scope in ("org", "personal"):
        where.append("scope = %s")
        args.append(scope)
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TEMPLATE_COLS}, {_SECTION_COUNT} FROM report_templates "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY scope, name",
        tuple(args),
    ).fetchall()


def get_visible(conn, company_id, user_id, template_id):
    """One template, or None -- where None covers 'no such row', 'archived' and
    'somebody else's personal one' alike.

    Deliberately one answer for all three: telling a caller that a template
    exists but is not theirs discloses that a colleague has a template by that
    id, which is precisely what personal scope promises it will not do.
    """
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TEMPLATE_COLS} FROM report_templates "
        "WHERE id = %s AND company_id = %s AND archived_at IS NULL "
        "AND (scope = 'org' OR owner_user_id = %s)",
        (str(template_id), str(company_id), str(user_id)),
    ).fetchone()


def list_versions(conn, template_id):
    """Newest first. The caller must have established visibility already."""
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, template_id, version, body, change_note, created_by, created_at "
        "FROM report_template_versions WHERE template_id = %s ORDER BY version DESC",
        (str(template_id),),
    ).fetchall()
    return [_decoded(r) for r in rows]


def get_version(conn, template_id, version):
    """A specific version's row, or None.

    `version=None` means the template's current one. A template whose
    current_version is 0 has no body yet, and this returns None for it rather
    than inventing an empty one -- 'not written yet' and 'written empty' are
    different states and a report generated from the first is a bug.
    """
    if version is None:
        row = conn.cursor(row_factory=dict_row).execute(
            "SELECT v.id, v.template_id, v.version, v.body, v.change_note, "
            "       v.created_by, v.created_at "
            "FROM report_template_versions v "
            "JOIN report_templates t ON t.id = v.template_id "
            "     AND t.current_version = v.version "
            "WHERE v.template_id = %s",
            (str(template_id),),
        ).fetchone()
    else:
        row = conn.cursor(row_factory=dict_row).execute(
            "SELECT id, template_id, version, body, change_note, created_by, created_at "
            "FROM report_template_versions WHERE template_id = %s AND version = %s",
            (str(template_id), int(version)),
        ).fetchone()
    return _decoded(row)


# ---- writing ---------------------------------------------------------------

def create(conn, company_id, scope, owner_user_id, slug, name, description,
           report_type, created_by):
    """A template with no body yet (current_version = 0).

    Creating and writing the first body are two statements on purpose: the row
    has to exist before a version can reference it, and a create that failed
    halfway through would otherwise leave a template whose current_version
    names a version that was never inserted.
    """
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO report_templates "
        "(company_id, scope, owner_user_id, slug, name, description, report_type, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        f"RETURNING {_TEMPLATE_COLS}",
        (str(company_id), scope, str(owner_user_id) if owner_user_id else None,
         slug, name, description or "", report_type, str(created_by)),
    ).fetchone()


def add_version(conn, template_id, body, change_note, created_by):
    """Append a version and point the template at it. Returns the new row.

    THE NUMBER IS ALLOCATED IN SQL, not read-then-incremented in Python, so
    the read and the write cannot be separated by anything.

    That does NOT make it race-free, and the difference matters to the caller.
    Two people saving at the same instant can both compute the same next
    number; the unique index on (template_id, version) then rejects one of
    them. That is the intended outcome -- the alternative is one version
    silently overwriting the other -- but it means a UniqueViolation here is a
    CONCURRENT EDIT, not a bug, and the route must answer 409 and let the
    person retry rather than 500. Nothing in this module retries on their
    behalf: a retry would re-apply an edit made against a body that has since
    changed, which is the same lost update wearing a different hat.

    The aggregate has no GROUP BY, so it yields exactly one row even when the
    template has no versions yet -- coalesce then makes that first one v1.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO report_template_versions "
        "(template_id, version, body, change_note, created_by) "
        "SELECT %s, coalesce(max(version), 0) + 1, %s::jsonb, %s, %s "
        "FROM report_template_versions WHERE template_id = %s "
        "RETURNING id, template_id, version, body, change_note, created_by, created_at",
        (str(template_id), json.dumps(body), change_note, str(created_by), str(template_id)),
    ).fetchone()
    conn.execute(
        "UPDATE report_templates SET current_version = %s, updated_at = now() WHERE id = %s",
        (row["version"], str(template_id)),
    )
    return _decoded(row)


def update_meta(conn, template_id, name=None, description=None):
    """Rename or re-describe. The BODY is never touched here -- changing what a
    template says is a new version, and routing both through one function is
    how an edit ends up with no version behind it."""
    sets, args = [], []
    if name is not None:
        sets.append("name = %s")
        args.append(name)
    if description is not None:
        sets.append("description = %s")
        args.append(description)
    if not sets:
        return get_any(conn, template_id)
    sets.append("updated_at = now()")
    args.append(str(template_id))
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE report_templates SET {', '.join(sets)} WHERE id = %s "
        f"RETURNING {_TEMPLATE_COLS}",
        tuple(args),
    ).fetchone()


def get_any(conn, template_id):
    """No visibility filter. For callers that have already established it, and
    for the archive path, which must be able to see an archived row."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TEMPLATE_COLS} FROM report_templates WHERE id = %s",
        (str(template_id),),
    ).fetchone()


def archive(conn, template_id):
    """Soft delete. Versions are kept -- a deleted template is still the
    template that wrote last month's reports.

    Already-archived rows are left alone rather than having archived_at moved,
    so the timestamp keeps saying when it was actually withdrawn.
    """
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE report_templates SET archived_at = now(), updated_at = now() "
        f"WHERE id = %s AND archived_at IS NULL RETURNING {_TEMPLATE_COLS}",
        (str(template_id),),
    ).fetchone()


def copy_to_personal(conn, template_id, owner_user_id, slug, name, created_by):
    """Copy a template the caller can see into their own library, body and all.

    A COPY, not a reference: the point of copying an org template is to change
    it, and a personal template that moved whenever the company's did would be
    the opposite of that. Only the current version is copied -- the original's
    history belongs to the original.

    Returns None when the source has no body yet; there is nothing to copy and
    a personal template silently created empty would read as a failed copy.
    """
    src = get_any(conn, template_id)
    if src is None:
        return None
    version = get_version(conn, template_id, None)
    if version is None:
        return None
    new = create(conn, src["company_id"], "personal", owner_user_id, slug, name,
                 src["description"], src["report_type"], created_by)
    add_version(conn, new["id"], version["body"],
                "Copied from %s v%s" % (src["slug"], version["version"]), created_by)
    return get_any(conn, new["id"])


# ---- bindings --------------------------------------------------------------

def list_bindings(conn, company_id):
    """Which template each scheduled report uses. Readable by anyone in the
    company: people are entitled to know what format their nightly report is
    written to, even though only gm/admin may change it."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT b.company_id, b.report_type, b.template_id, b.pinned_version, "
        "       b.set_by, b.set_at, t.name AS template_name, t.slug AS template_slug, "
        "       t.current_version, t.archived_at "
        "FROM report_template_bindings b "
        "JOIN report_templates t ON t.id = b.template_id "
        "WHERE b.company_id = %s ORDER BY b.report_type",
        (str(company_id),),
    ).fetchall()


def set_binding(conn, company_id, report_type, template_id, pinned_version, set_by):
    """Point a scheduled report at a template. gm/admin only -- enforced above."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO report_template_bindings "
        "(company_id, report_type, template_id, pinned_version, set_by) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (company_id, report_type) DO UPDATE SET "
        "template_id = EXCLUDED.template_id, pinned_version = EXCLUDED.pinned_version, "
        "set_by = EXCLUDED.set_by, set_at = now() "
        "RETURNING company_id, report_type, template_id, pinned_version, set_by, set_at",
        (str(company_id), report_type, str(template_id),
         int(pinned_version) if pinned_version is not None else None, str(set_by)),
    ).fetchone()


def is_bound(conn, template_id):
    """Whether any schedule points at this template.

    Asked BEFORE archiving. The foreign key is ON DELETE RESTRICT so the
    database would refuse a hard delete, but archiving is an UPDATE and no
    constraint can catch it -- an archived-but-bound template would leave the
    schedule pointing at something the Library no longer shows, which is the
    silent fallback-to-default this design exists to prevent.
    """
    return conn.execute(
        "SELECT count(*) FROM report_template_bindings WHERE template_id = %s",
        (str(template_id),),
    ).fetchone()[0] > 0
