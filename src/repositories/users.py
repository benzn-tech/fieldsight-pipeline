from psycopg.rows import dict_row

_COLS = "id, cognito_sub, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at, archived_at"


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


def list_company_users(conn, company_id, include_archived=False) -> list[dict]:
    guard = "" if include_archived else "AND archived_at IS NULL "
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE company_id=%s {guard}ORDER BY created_at",
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
