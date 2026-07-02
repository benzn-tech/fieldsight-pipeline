from psycopg.rows import dict_row


def create_company(conn, name, industry=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO companies (name, industry) VALUES (%s, %s) "
        "RETURNING id, name, industry, created_at",
        (name, industry),
    ).fetchone()
