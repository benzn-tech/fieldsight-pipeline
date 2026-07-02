from psycopg.rows import dict_row

_COLS = "id, cognito_sub, company_id, email, first_name, last_name, avatar_s3_key, global_role, created_at"


def upsert_user(conn, cognito_sub, email, company_id=None, first_name=None,
                last_name=None, global_role="worker") -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO users (cognito_sub, email, company_id, first_name, last_name, global_role) "
        f"VALUES (%s, %s, %s, %s, %s, %s) "
        f"ON CONFLICT (cognito_sub) DO UPDATE SET "
        f"  email=EXCLUDED.email, company_id=EXCLUDED.company_id, "
        f"  first_name=COALESCE(EXCLUDED.first_name, users.first_name), "
        f"  last_name=COALESCE(EXCLUDED.last_name, users.last_name), "
        f"  global_role=EXCLUDED.global_role "
        f"RETURNING {_COLS}",
        (cognito_sub, email, company_id, first_name, last_name, global_role),
    ).fetchone()


def get_user_by_sub(conn, cognito_sub) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM users WHERE cognito_sub=%s", (cognito_sub,)
    ).fetchone()
