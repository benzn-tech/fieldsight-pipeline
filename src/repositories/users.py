from psycopg.rows import dict_row

_COLS = "id, cognito_sub, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at, archived_at, folder_name, kind"


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


def get_user_by_sub(conn, cognito_sub) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE cognito_sub=%s", (cognito_sub,)
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
    constraint -- field_only rows get ''."""
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


def list_company_users(conn, company_id, include_archived=False) -> list[dict]:
    guard = "" if include_archived else "AND archived_at IS NULL "
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE company_id=%s {guard}ORDER BY created_at",
        (company_id,),
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
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET archived_at=NULL "
        f"WHERE cognito_sub=%s AND company_id=%s AND archived_at IS NOT NULL RETURNING {_COLS}",
        (cognito_sub, company_id),
    ).fetchone()


def clear_avatar(conn, cognito_sub) -> dict | None:
    """Explicit avatar removal (update_profile's COALESCE can't set NULL)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE users SET avatar_s3_key=NULL WHERE cognito_sub=%s RETURNING {_COLS}",
        (cognito_sub,),
    ).fetchone()
