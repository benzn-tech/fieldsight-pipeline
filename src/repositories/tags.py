"""The taxonomy: reading it, extending it, and refusing to break it.

Three scopes live in one table (migration 0065):

    company_id IS NULL, site_id IS NULL   the global base set we ship
    company_id set,      site_id IS NULL   a company's own vocabulary
    company_id set,      site_id set       a child one project added

WHICH ROWS A CALLER MAY SEE IS A DIFFERENT QUESTION FROM WHICH ROWS A CALLER
MAY WRITE, and they are answered in different places on purpose. Visibility is
`list_visible` below. Authority -- who is allowed to call `create`/`update` at
all -- is the route's job, because it depends on the caller's role and this
layer does not have one. What this layer refuses is the part that no role
should ever be able to do: write another company's tag, or write the base set.

Refusing it HERE and not only at the route is deliberate. A repo that will
happily update a global row is one careless route away from someone renaming
'Safety' for every customer, and this repo has shipped an endpoint with no
authorization at all before.
"""
from psycopg.rows import dict_row

_COLS = ("id, company_id, site_id, parent_id, slug, label, is_active, "
         "sort_order, created_at, created_by")


class NotWritable(Exception):
    """This tag exists but is not this caller's to change."""


def list_visible(conn, company_id, site_ids, *, include_inactive=False) -> list[dict]:
    """The taxonomy one caller can see: the base set, their company's tags, and
    the tags of the sites they can reach.

    `site_ids` IS NOT OPTIONAL AND `[]` MEANS NO SITES. The `= ANY(%s)`
    formulation matches nothing for an empty array, which is the answer a
    worker with no memberships must get. The alternative -- skipping the site
    arm when the list is empty -- reads as "no filter" and is exactly the
    empty-list-means-deny-all confusion that has already cost this codebase a
    cross-tenant leak. There is no branch here; the predicate is always the
    same shape.

    Inactive tags are excluded by default so a deactivated word stays out of
    every picker, and INCLUDED on request because otherwise the admin screen
    could never show one to switch it back on.

    Ordered by (sort_order, label) and NOTHING ELSE. The first draft tried to
    group each parent with its children in SQL --
    `ORDER BY COALESCE(parent_id::text, id::text), ...` -- which does group
    them, and then orders the GROUPS by a uuid rendered as text. The twelve
    top-level tags would have come out in an order nobody chose and that
    changes every time the base set is re-seeded into a fresh database. A unit
    test on the client's tree builder is what caught it.

    The client builds the tree (api/tags.js asTree) and sorts each level by
    the same two columns, so the ordering lives in one shape rather than being
    half-expressed here and half there.
    """
    where = ("(company_id IS NULL "
             " OR (company_id = %s AND site_id IS NULL) "
             " OR site_id = ANY(%s::uuid[]))")
    params = [str(company_id) if company_id else None,
              [str(s) for s in (site_ids or [])]]
    if not include_inactive:
        where += " AND is_active"
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM tag WHERE {where} ORDER BY sort_order, label",
        tuple(params),
    ).fetchall()


def get(conn, tag_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM tag WHERE id = %s", (tag_id,)).fetchone()


def create(conn, *, company_id, site_id, parent_id, slug, label,
           created_by=None, sort_order=0) -> dict:
    """Add one tag to a company's (or a site's) vocabulary.

    A SITE TAG CARRIES ITS COMPANY TOO. Without it the row is invisible to the
    company arm of `list_visible` for everyone except the people who can reach
    that site, and the company-wide uniqueness index would not see it either --
    so a project could quietly shadow a company-wide slug and two different
    tags would answer to one name.

    `company_id` is never None here: this function cannot create a global tag.
    The base set is seeded by the migration and is not writable at runtime by
    anyone, which is why there is no parameter for it rather than a guard
    against it.
    """
    if not company_id:
        raise NotWritable("a tag must belong to a company; the base set is not writable")
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO tag (company_id, site_id, parent_id, slug, label, sort_order, created_by) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
        (str(company_id), str(site_id) if site_id else None,
         str(parent_id) if parent_id else None, slug, label, sort_order,
         str(created_by) if created_by else None),
    ).fetchone()


def update(conn, tag_id, *, company_id, label=None, is_active=None,
           sort_order=None) -> dict | None:
    """Change a tag's LABEL, its activation or its order. Never its slug.

    A slug is what an assignment row and a prompt both name. Renaming one would
    orphan every row that used it, silently -- the rows stay, pointing at a
    word that no longer means what they were tagged with. Labels are the part
    people actually want to change, and changing a label breaks nothing.

    Deactivating is an UPDATE. There is no delete: a tag with assignments
    cannot be removed without deciding what happens to them, and a tag without
    assignments costs nothing to leave in place switched off.

    Raises NotWritable for the global base set and for another company's tags.
    Returns None when the id does not exist, so a caller can tell "not yours"
    (403) from "not there" (404) -- one answer for both would tell a stranger
    which ids are real.
    """
    row = get(conn, tag_id)
    if row is None:
        return None
    if row["company_id"] is None:
        raise NotWritable("the base set is shared by every customer and is read-only")
    if str(row["company_id"]) != str(company_id):
        raise NotWritable("that tag belongs to another company")

    sets, params = [], []
    if label is not None:
        sets.append("label = %s")
        params.append(label)
    if is_active is not None:
        sets.append("is_active = %s")
        params.append(bool(is_active))
    if sort_order is not None:
        sets.append("sort_order = %s")
        params.append(int(sort_order))
    if not sets:
        # An UPDATE with an empty SET list is a syntax error, and a PATCH body
        # carrying only keys we do not accept produces exactly that. Returning
        # the unchanged row is the honest answer: nothing was asked for.
        return row
    params.append(tag_id)
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE tag SET {', '.join(sets)} WHERE id = %s RETURNING {_COLS}",
        tuple(params),
    ).fetchone()


def ids_for_slugs(conn, company_id, slugs) -> dict:
    """{slug: tag_id} for the slugs this company can actually use.

    A COMPANY'S OWN ROW WINS OVER THE GLOBAL ONE of the same slug. That is what
    shadowing is for: a company that renames 'Walls' creates its own row with
    the same slug, and everything written afterwards must point at theirs, or
    their rename would be visible in the picker and invisible on the data.

    Site-level tags are NOT resolved here. This is called by the writer that
    persists an extraction's labels, and that labelling is done outside the VPC
    from the base set alone (taxonomy_base) -- it cannot emit a site slug, so
    accepting one would be resolving a word nothing can produce.

    A slug with no row is simply ABSENT from the result. The caller drops it
    rather than inventing a tag: a label the vocabulary does not have must not
    become a row that looks deliberate.
    """
    wanted = sorted({s for s in (slugs or []) if s})
    if not wanted:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT slug, id, company_id FROM tag "
        "WHERE site_id IS NULL AND slug = ANY(%s) "
        "AND (company_id IS NULL OR company_id = %s)",
        (wanted, str(company_id) if company_id else None),
    ).fetchall()
    out = {}
    for r in rows:
        # The company's row overwrites the global one; the global one never
        # overwrites the company's, whatever order the rows arrive in.
        if r["company_id"] is not None or r["slug"] not in out:
            out[r["slug"]] = r["id"]
    return out


def children_of(conn, tag_id) -> list[dict]:
    """Used before deactivating a parent, so the caller can say what else it
    is about to hide rather than discovering it afterwards."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM tag WHERE parent_id = %s ORDER BY sort_order, label",
        (tag_id,)).fetchall()
