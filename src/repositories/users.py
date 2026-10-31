import re
import uuid as _uuid

import folder_key
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_COLS = "id, cognito_sub, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at, archived_at, folder_name, kind, prefs"

# The Team directory (list_company_users / list_all_users) also carries last_seen_at
# (migration 0091). Kept OUT of the shared _COLS on purpose: _COLS is read by
# get_user_by_sub on every org-api request, and the deploy runs sam deploy BEFORE the
# migrate step -- a column the database does not have yet would 500 every request in
# that window (or for good, if the migrate step failed). Here it can only break the
# member list.
_MEMBER_COLS = _COLS + ", last_seen_at"


def upsert_user(conn, cognito_sub, email, company_id=None, first_name=None,
                last_name=None, global_role=None) -> dict:
    """Create or update the app profile keyed by cognito_sub.

    None means "leave the existing value unchanged" for company_id,
    first_name, last_name and global_role; new rows default global_role
    to 'worker'. Callers must never pass client-supplied roles unchecked.
    """
    params = {
        "sub": cognito_sub, "email": email, "company_id": company_id,
        "first": first_name, "last": last_name, "role": global_role,
    }
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO users (cognito_sub, email, company_id, first_name, last_name, global_role) "
        f"VALUES (%(sub)s, %(email)s, %(company_id)s, %(first)s, %(last)s, COALESCE(%(role)s, 'worker')) "
        f"ON CONFLICT (cognito_sub) DO UPDATE SET "
        f"  email=EXCLUDED.email, "
        f"  company_id=COALESCE(%(company_id)s, users.company_id), "
        f"  first_name=COALESCE(%(first)s, users.first_name), "
        f"  last_name=COALESCE(%(last)s, users.last_name), "
        f"  global_role=COALESCE(%(role)s, users.global_role) "
        f"RETURNING {_COLS}",
        params,
    ).fetchone()


def find_live_users_by_email_global(conn, email, limit=2) -> list:
    """LIVE users by exact login email (case-insensitive), across ALL companies, each with
    their home company's name, oldest first, at most `limit` (default 2: the caller only
    needs to know whether the email is unique). Used only to add an existing person from
    another company to a project (project-owned tenancy P1) -- never to search. Archived
    users are not found. More than one row means the email is ambiguous: the caller
    refuses rather than picking one (final review F8)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT users.{', users.'.join(_COLS.split(', '))}, c.name AS company_name "
        f"FROM users LEFT JOIN companies c ON c.id = users.company_id "
        f"WHERE lower(users.email) = lower(%s) AND users.archived_at IS NULL "
        f"ORDER BY users.created_at LIMIT %s", (email, limit)
    ).fetchall()


def get_user_by_email_global(conn, email) -> dict | None:
    """The one live user with this email, or None -- also None when TWO OR MORE share it
    (never pick one: see `find_live_users_by_email_global`)."""
    rows = find_live_users_by_email_global(conn, email)
    return rows[0] if len(rows) == 1 else None


def get_user_by_sub(conn, cognito_sub) -> dict | None:
    """The caller, with their company's voiceprint basis carried alongside.

    The basis is a company fact — settled at induction and in the subcontract — and every
    request already loads the caller, so it rides along in that query rather than costing a
    second round trip on a value that changes about once in a company's lifetime.

    LEFT JOIN, not JOIN: a user whose company row is missing is a broken state worth seeing
    as a user with no basis, not as a user who does not exist. And absent reads as None,
    which is the strict fallback — enrolment then needs the subject's own id, exactly as it
    did before any of this. A caller assembled without this field (every test double) gets
    the same, which is the safe direction for a default nobody chose.
    """
    return conn.cursor(row_factory=dict_row).execute(
        # `users.*` rather than the bare `_COLS`, which is shared with the un-joined
        # queries below. The moment a second table is in scope, an unqualified `id` matches
        # both and Postgres refuses the statement — `AmbiguousColumn`, at PREPARE time, for
        # every caller. Only a real database says so: the connection doubles in the suite
        # record the SQL string and never parse it, so 3286 unit tests passed over it.
        f"SELECT users.{', users.'.join(_COLS.split(', '))}, "
        f"       c.voiceprint_consent_basis "
        f"FROM users LEFT JOIN companies c ON c.id = users.company_id "
        f"WHERE users.cognito_sub=%s", (cognito_sub,)
    ).fetchone()


def get_by_id(conn, user_id) -> dict | None:
    """Look up a user by internal id (e.g. meeting_session.user_id) -> email +
    folder_name. Used by the Tier-0 finalize claim step to address the recorder."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE id=%s", (str(user_id),)
    ).fetchone()


def get_by_folder_name(conn, company_id, folder_name) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE company_id=%s AND folder_name=%s",
        (company_id, folder_name),
    ).fetchone()


def get_by_folder_name_global(conn, folder_name) -> dict | None:
    """Cross-company folder lookup for the shared-lake pipeline (0012 makes
    folder_name globally unique, so at most one row)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE folder_name=%s",
        (folder_name,),
    ).fetchone()


def upsert_field_only_user(conn, company_id, folder_name, first_name,
                           last_name, global_role) -> dict:
    """Enroll (or refresh) a non-login directory entry -- a field worker who
    only ever appears as a device/report folder, never signs in via Cognito.
    cognito_sub is NULL (NULL never collides in a UNIQUE index, so this
    can't reuse upsert_user's ON CONFLICT (cognito_sub) path); conflicts are
    keyed on (company_id, folder_name) instead. email has a NOT NULL
    constraint -- field_only rows get ''.

    `folder_name` must already be a folder key (folder_key.folder_key); the
    database CHECK (0081) would refuse anything else, so refuse it here first
    with a message that says why."""
    if not folder_key.KEY_RE.fullmatch(folder_name or ""):
        raise ValueError(f"folder_name {folder_name!r} is not a folder key "
                         "(mint it with folder_key.folder_key)")
    params = {
        "company_id": company_id, "folder_name": folder_name,
        "first": first_name, "last": last_name, "role": global_role,
    }
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO users (company_id, email, first_name, last_name, global_role, folder_name, kind, cognito_sub) "
        f"VALUES (%(company_id)s, '', %(first)s, %(last)s, %(role)s, %(folder_name)s, 'field_only', NULL) "
        f"ON CONFLICT (company_id, folder_name) DO UPDATE SET "
        f"  first_name=EXCLUDED.first_name, "
        f"  last_name=EXCLUDED.last_name, "
        f"  global_role=EXCLUDED.global_role "
        f"RETURNING {_COLS}",
        params,
    ).fetchone()


def set_folder_name(conn, cognito_sub, folder_name) -> None:
    """Backfill folder_name onto an existing login user (enrollment path for
    the other 4 of the 8 directory people, who do have Cognito accounts)."""
    conn.cursor().execute(
        "UPDATE users SET folder_name=%s WHERE cognito_sub=%s",
        (folder_name, cognito_sub),
    )
    return None


def touch_last_seen(conn, cognito_sub) -> bool:
    """Stamp users.last_seen_at = now() for a signed-in caller, at most once per 5 minutes.

    The throttle is in the WHERE clause, so a caller polling /me does not write a row on
    every request. FOR UPDATE SKIP LOCKED: if another transaction holds the row, skip the
    stamp rather than wait -- /me must never queue behind it. True when a row was written."""
    cur = conn.execute(
        "UPDATE users SET last_seen_at = now() WHERE id = ("
        " SELECT id FROM users WHERE cognito_sub = %s"
        " AND (last_seen_at IS NULL OR last_seen_at < now() - interval '5 minutes')"
        " FOR UPDATE SKIP LOCKED)",
        (cognito_sub,),
    )
    return cur.rowcount == 1


def list_company_users(conn, company_id, include_archived=False) -> list[dict]:
    guard = "" if include_archived else "AND archived_at IS NULL "
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_MEMBER_COLS} FROM users WHERE company_id=%s {guard}ORDER BY created_at",
        (company_id,),
    ).fetchall()


def folder_names_for_user_ids(conn, user_ids, company_id=None) -> set:
    """Recording folder names for a set of user ids -- the org report-history
    route's scope resolver (S-1 companion, 2026-07-23 security plan).

    Returns a SET of non-empty folder_name strings; users with no folder
    mapping simply contribute nothing (they own no reports/{date}/{folder}/
    objects either). Empty in -> empty out, no round-trip.

    company_id, when given, pins the lookup to that company -- belt and
    braces on top of the already company-scoped id sets visible_scope
    hands us, so a mis-tenanted membership row can never widen the
    result. Pass None only for the cross-company (platform_admin) path."""
    if not user_ids:
        return set()
    guard = "AND company_id = %s " if company_id else ""
    params = [list(str(u) for u in user_ids)]
    if company_id:
        params.append(company_id)
    rows = conn.execute(
        "SELECT folder_name FROM users WHERE id = ANY(%s::uuid[]) "
        f"AND archived_at IS NULL {guard}"
        "AND folder_name IS NOT NULL AND folder_name <> ''",
        tuple(params),
    ).fetchall()
    return {r[0] for r in rows}


def list_all_users(conn, include_archived=False) -> list[dict]:
    """Cross-company user directory -- platform_admin (is_cross_company) only.
    Mirrors list_company_users WITHOUT the company_id filter so the platform
    operator's Team directory spans every tenant. Each row still carries
    company_id (in _COLS) so callers can group / label by company."""
    guard = "" if include_archived else "WHERE archived_at IS NULL "
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_MEMBER_COLS} FROM users {guard}ORDER BY company_id, created_at",
    ).fetchall()


def list_company_logins_unenrolled(conn, company_id) -> list[dict]:
    """Logins (cognito_sub IS NOT NULL) in this company that never got a
    folder_name -- the bulk-backfill route's input set. Excludes field_only
    directory rows (cognito_sub IS NULL; those are enrolled separately via
    upsert_field_only_user) and archived accounts."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, cognito_sub, first_name, last_name FROM users "
        "WHERE company_id=%s AND folder_name IS NULL AND cognito_sub IS NOT NULL "
        "AND archived_at IS NULL",
        (company_id,),
    ).fetchall()


def set_global_role(conn, cognito_sub, company_id, global_role) -> dict | None:
    """Explicit role SET (admin action). Company-guarded: refuses to touch a
    row outside the caller's company (cross-tenant write = returns None)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET global_role=%s "
        f"WHERE cognito_sub=%s AND company_id=%s RETURNING {_COLS}",
        (global_role, cognito_sub, company_id),
    ).fetchone()


def update_profile(conn, cognito_sub, first_name=None, last_name=None,
                   avatar_s3_key=None) -> dict | None:
    """Self-service profile update. None = leave unchanged (same semantics
    as upsert_user). Role/company are NOT touchable here by design."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET "
        f"  first_name=COALESCE(%(first)s, first_name), "
        f"  last_name=COALESCE(%(last)s, last_name), "
        f"  avatar_s3_key=COALESCE(%(avatar)s, avatar_s3_key) "
        f"WHERE cognito_sub=%(sub)s RETURNING {_COLS}",
        {"sub": cognito_sub, "first": first_name, "last": last_name,
         "avatar": avatar_s3_key},
    ).fetchone()


def merge_prefs(conn, cognito_sub, prefs: dict) -> dict | None:
    """Shallow-merge UI preferences (migration 0028).

    `prefs || %s` rather than an assignment: a surface saving its own key
    must not clobber preferences belonging to a surface it never read. Nested
    objects are replaced wholesale, which is fine — every key here is a flat
    UI choice.
    """
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET prefs = prefs || %s WHERE cognito_sub = %s "
        f"RETURNING {_COLS}",
        (Jsonb(prefs or {}), cognito_sub),
    ).fetchone()


def archive_user(conn, cognito_sub, company_id) -> dict | None:
    """Soft-delete a user (company-guarded) and cascade-archive their
    memberships. Cognito login is NOT touched (design). Returns None if not
    found / wrong company / already archived."""
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute(
        f"UPDATE users SET archived_at=now() "
        f"WHERE cognito_sub=%s AND company_id=%s AND archived_at IS NULL RETURNING {_COLS}",
        (cognito_sub, company_id),
    ).fetchone()
    if row is None:
        return None
    cur.execute(
        "UPDATE memberships SET archived_at=now() "
        "WHERE user_id=%s AND archived_at IS NULL", (row["id"],))
    return row


def unarchive_user(conn, cognito_sub, company_id) -> dict | None:
    """Restore an archived user (company-guarded) and the memberships archive_user closed
    WITH them -- those whose archived_at equals the user's (archive_user stamps both with
    the one now() of its transaction). A membership closed earlier on its own, or by a
    site archive, carries a different stamp and stays closed (spec 2026-11-01 company
    offboarding §8). Returns None if not found / wrong company / not archived.

    The user's archived_at is read (and the row locked) BEFORE it is cleared; the three
    statements run in the caller's one transaction."""
    cur = conn.cursor(row_factory=dict_row)
    found = cur.execute(
        "SELECT id, archived_at FROM users "
        "WHERE cognito_sub=%s AND company_id=%s AND archived_at IS NOT NULL FOR UPDATE",
        (cognito_sub, company_id),
    ).fetchone()
    if found is None:
        return None
    cur.execute(
        "UPDATE memberships SET archived_at=NULL WHERE user_id=%s AND archived_at=%s",
        (found["id"], found["archived_at"]))
    return cur.execute(
        f"UPDATE users SET archived_at=NULL WHERE id=%s RETURNING {_COLS}",
        (found["id"],),
    ).fetchone()


def clear_avatar(conn, cognito_sub) -> dict | None:
    """Explicit avatar removal (update_profile's COALESCE can't set NULL)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET avatar_s3_key=NULL WHERE cognito_sub=%s RETURNING {_COLS}",
        (cognito_sub,),
    ).fetchone()

_UNSAFE_IN_FOLDER = re.compile(r'[<>:"/\\|?*\s]')


def safe_folder_name(raw):
    """The folder spelling of a person's name, in ONE place.

    org-api built this inline in three separate spots, which was survivable while it was only
    ever used to WRITE. Reading is what makes a second copy dangerous: a resolver that
    compares a typed name against `folder_name` without applying the same substitution misses
    every multi-word name, because "Neil Blunden" is stored as "Neil_Blunden" — and it misses
    them silently, resolving to nobody and looking like "that person is not in the directory".
    """
    return _UNSAFE_IN_FOLDER.sub("_", (raw or "").strip())


def resolve_display_name(conn, company_id, name):
    """Which person in this company that name refers to, or an honest refusal.

    Returns `(row, matched_on)` or `(None, reason)` where reason is `ambiguous` or
    `not-in-directory`.

    **Ambiguity refuses rather than guesses.** Attaching a voiceprint to the wrong identity
    files one person's voice under another's name, and the site filter then hides it from the
    site they are on while offering it on one they are not — a wrong answer nobody can see. A
    profile with no identity still works by display name; a profile with the wrong one does
    not fail anywhere.

    `concat_ws`, not `first_name || ' ' || last_name`: in Postgres `'Ben' || ' ' || NULL` is
    NULL and `NULL = 'ben'` is never true, so the second rule would match nobody for every
    row with no surname — and rows with no surname are ordinary here (the `Ben_UCPK_` folder,
    trailing underscore and all, comes from exactly that).
    """
    wanted = (name or "").strip()
    if not wanted or not company_id:
        return None, "not-in-directory"
    cur = conn.cursor(row_factory=dict_row)
    folder = safe_folder_name(wanted)
    rows = cur.execute(
        f"SELECT {_COLS} FROM users "
        "WHERE company_id = %s AND archived_at IS NULL AND lower(folder_name) = lower(%s)",
        (company_id, folder)).fetchall()
    if len(rows) == 1:
        return rows[0], "folder_name"
    if len(rows) > 1:
        return None, "ambiguous"

    rows = cur.execute(
        f"SELECT {_COLS} FROM users "
        "WHERE company_id = %s AND archived_at IS NULL "
        "  AND lower(concat_ws(' ', first_name, last_name)) = lower(%s)",
        (company_id, wanted)).fetchall()
    if len(rows) == 1:
        return rows[0], "full_name"
    if len(rows) > 1:
        return None, "ambiguous"

    rows = cur.execute(
        f"SELECT {_COLS} FROM users "
        "WHERE company_id = %s AND archived_at IS NULL AND lower(first_name) = lower(%s)",
        (company_id, wanted)).fetchall()
    if len(rows) == 1:
        return rows[0], "first_name"
    if len(rows) > 1:
        return None, "ambiguous"
    return None, "not-in-directory"


def first_officer_or_member(conn, company_id):
    """The person to record as author for something the company owns, or None.

    Anything written on a company's behalf still needs a real person on it:
    report_templates.created_by is NOT NULL and references users(id). An
    officer is preferred because the things seeded this way are org-scoped, and
    org-scoped things are the officers' to manage -- but any live member is
    better than failing, and a company with nobody in it returns None rather
    than raising, because there is also nobody there to see the result.

    ORDER BY on a boolean, not a role list in Python: the tie-break has to be
    deterministic or re-running a seed names a different author each time, and
    an author that moves is an audit trail that says nothing.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"SELECT {_COLS} FROM users "
            "WHERE company_id = %s AND archived_at IS NULL "
            "ORDER BY (global_role IN ('gm', 'admin')) DESC, created_at ASC, id ASC "
            "LIMIT 1",
            (company_id,)).fetchall()
    return rows[0] if rows else None


def homes_for_folders(conn, folders) -> dict:
    """{folder_name: {id, company_id}} (one query; folder_name is globally unique)."""
    folders = sorted({f for f in (folders or []) if f})
    if not folders:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT folder_name, id, company_id FROM users WHERE folder_name = ANY(%s)",
        (folders,)).fetchall()
    return {r["folder_name"]: {"id": r["id"], "company_id": r["company_id"]} for r in rows}


def _candidates(conn, site_ids, author_ids) -> set:
    """Ask find_people candidate set (spec 2026-10-15 §3.1.4): footprint on
    `site_ids`, narrowed by the graded `author_ids` when that is not None."""
    from repositories import recordings
    cands = recordings.author_candidates_in_sites(conn, site_ids)
    if author_ids is not None:
        cands &= {str(a) for a in author_ids}
    return cands


def _canonical_uuid(ref) -> str | None:
    """`ref` as a canonical lowercase UUID string, or None when it is not one.
    Accepts a UUID, or a string in any form `uuid.UUID` parses (upper case,
    braces, no hyphens)."""
    if isinstance(ref, _uuid.UUID):
        return str(ref)
    if not isinstance(ref, str) or not ref.strip():
        return None
    try:
        return str(_uuid.UUID(ref.strip()))
    except ValueError:
        return None


def person_in_reach(conn, site_ids, author_ids, user_id) -> bool:
    """Re-validated on EVERY tool call (spec §3.1.4): a person_ref is never
    trusted because find_people minted it.

    Order matters. A ref that is not a UUID is False with no DB call, so a
    garbage or forged ref looks exactly like an out-of-reach one and can never
    raise a cast error (§3.3: out-of-ACL looks like no match). Then the graded
    author set. Then the footprint boolean, which stops at the first match
    rather than building the whole candidate set."""
    uid = _canonical_uuid(user_id)
    if uid is None:
        return False
    if author_ids is not None and uid not in {str(a) for a in author_ids}:
        return False
    from repositories import recordings
    return recordings.author_footprint_in_sites(conn, uid, site_ids)


def _norm_name(s) -> str:
    """One normaliser for both sides of every name comparison: `_` reads as a
    space, whitespace runs collapse to one space, case folds. Applied to the
    query, the stored first/last/folder names and both alias terms."""
    return " ".join((s or "").replace("_", " ").split()).lower()


def _name_keys(row) -> set:
    first = _norm_name(row["first_name"])
    full = _norm_name(f"{row['first_name'] or ''} {row['last_name'] or ''}")
    folder = _norm_name(row["folder_name"])
    return {k for k in (first, full, folder) if k}


def find_visible_people(conn, company_id, site_ids, author_ids, name) -> list[dict]:
    """People the caller may ask about whose name matches `name` (spec §3.1.4).

    Candidates are the users with a footprint on the caller's sites (topics,
    recordings, live memberships), intersected with `author_ids`. The user rows
    are then read by id with NO company pin, so an external author from another
    company who worked on these sites is found, while anyone without a footprint
    never enters the set. Archived users are not filtered: visibility (scope,
    author_footprint_in_sites, topic reads) never looks at users.archived_at, so
    a departed person's history stays readable and they stay findable.

    Matching is exact on `_norm_name` keys (case-insensitive, `_` = space,
    whitespace collapsed): first name, full name, folder name, or an active
    `kind='person'` alias of the CALLER's company (company-wide or on one of
    `site_ids`) whose `wrong_term` is the query and whose `right_term` is one of
    a candidate's keys. Never substring: "Mi" is not "Mike". Ambiguity is
    returned, not resolved -- two Mikes are two entries, sorted by name.
    """
    q = _norm_name(name)
    if not q or not company_id:
        return []
    cands = _candidates(conn, site_ids, author_ids)
    if not cands:
        return []
    # Normalised in Python, not SQL, so both alias terms go through exactly the
    # same function as the query and the names. One company's person glossary.
    alias_targets = {
        _norm_name(right) for wrong, right in conn.execute(
            "SELECT wrong_term, right_term FROM name_aliases "
            "WHERE company_id=%s AND status='active' AND kind='person' "
            "AND (site_id IS NULL OR site_id = ANY(%s::uuid[]))",
            (company_id, [str(s) for s in site_ids])).fetchall()
        if _norm_name(wrong) == q}
    alias_targets.discard("")
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, first_name, last_name, folder_name FROM users "
        "WHERE id = ANY(%s::uuid[])", (sorted(cands),)).fetchall()
    out = []
    for r in rows:
        keys = _name_keys(r)
        if q in keys or keys & alias_targets:
            full = " ".join(p.strip() for p in (r["first_name"], r["last_name"]) if p and p.strip())
            out.append({"person_ref": str(r["id"]), "name": full or r["folder_name"],
                        "folder_name": r["folder_name"]})
    return sorted(out, key=lambda p: ((p["name"] or "").lower(), p["person_ref"]))
