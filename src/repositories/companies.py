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


def list_companies(conn) -> list[dict]:
    """Every tenant company -- platform_admin cross-company views (Team,
    Sites) use this to label each user/site with its company name."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, name, industry, created_at FROM companies ORDER BY name",
    ).fetchall()


VOICEPRINT_BASES = ("notice", "attestation", "confirmed")


def set_voiceprint_consent_basis(conn, company_id, basis):
    """On what basis this company may hold a voiceprint. Returns the row it changed.

    A short closed list, not free text. The value decides whether biometric data may be
    created at all, and a typo that lands outside the list would read as "settled" to the
    endpoint while meaning nothing — the shape where a guard passes on a value nobody
    intended. `None` clears it, which is how a company withdraws the basis and returns
    enrolment to the strict rule.

    No audit row is written here and that is a gap, not a decision: this is a change to the
    grounds on which a company may hold biometric data, and it should be attributable. It is
    left to the endpoint for now because that is where the caller's identity is, and recorded
    here so it is not mistaken for something that was considered and dismissed.
    """
    if basis is not None and basis not in VOICEPRINT_BASES:
        raise ValueError(
            f"unknown voiceprint consent basis {basis!r}; one of {VOICEPRINT_BASES} or None")
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE companies SET voiceprint_consent_basis=%s WHERE id=%s "
        "RETURNING id, name, voiceprint_consent_basis",
        (basis, str(company_id)),
    ).fetchone()
