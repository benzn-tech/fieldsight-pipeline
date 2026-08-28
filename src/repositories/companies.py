from psycopg.rows import dict_row


def create_company(conn, name, industry=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO companies (name, industry) VALUES (%s, %s) "
        "RETURNING id, name, industry, created_at",
        (name, industry),
    ).fetchone()


def get_company_by_name(conn, name) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, name, industry, created_at FROM companies WHERE name=%s",
        (name,),
    ).fetchone()


def get_company_by_id(conn, company_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, name, industry, created_at, voiceprint_consent_basis "
        "FROM companies WHERE id=%s",
        (company_id,),
    ).fetchone()


def voiceprint_consent_basis(conn, company_id):
    """On what basis this company may hold a voiceprint, or None if it has not settled one.

    A company fact, not a per-request one. On a real site the basis is decided before anybody
    opens the app: the induction tells workers their voice is captured for reports and
    archiving and not for training, and the subcontract says the same. Every correction made
    inside that company inherits it.

    Typing it per request — which is what 0048 did — lets two corrections in one company
    disagree about the basis under which the same person was recorded, and leaves the answer
    to whoever happened to be at the keyboard.

    None means the company has not settled one, and enrolment falls back to the strict rule
    that predates all of this: the subject's own id, or nothing.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT voiceprint_consent_basis FROM companies WHERE id=%s",
        (company_id,)).fetchone()
    return (row or {}).get("voiceprint_consent_basis") or None


def list_companies(conn) -> list[dict]:
    """Every tenant company -- platform_admin cross-company views (Team,
    Sites) use this to label each user/site with its company name."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, name, industry, created_at FROM companies ORDER BY name",
    ).fetchall()
