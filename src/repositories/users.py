from psycopg.rows import dict_row

_COLS = "id, cognito_sub, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at"


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
