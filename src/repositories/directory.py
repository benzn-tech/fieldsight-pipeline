"""The three reads behind config/directory.json. Live rows only: archived
users and sites, and memberships archived or sitting on an archived site, are
excluded here, so the document never has to know about archiving."""
from psycopg.rows import dict_row


def live_users(conn):
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, folder_name, first_name, last_name, global_role, company_id "
        "FROM users WHERE archived_at IS NULL AND folder_name IS NOT NULL "
        "ORDER BY folder_name").fetchall()


def live_sites(conn):
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, company_id, name, location, client, slug "
        "FROM sites WHERE archived_at IS NULL ORDER BY company_id, created_at").fetchall()


def live_memberships(conn):
    # The company invariant (user and site in the same company) is enforced
    # here as everywhere else: a mis-tenanted row must not put a person on
    # another company's project in a file every report reads. External
    # memberships (project-owned tenancy P1) are deliberately NOT published
    # here: the pipeline's people/site maps are home-company only, or the
    # weekly/monthly rollups would mix companies (final review F2).
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT m.user_id, m.site_id FROM memberships m "
        "JOIN users u ON u.id = m.user_id JOIN sites s ON s.id = m.site_id "
        "WHERE m.archived_at IS NULL AND u.archived_at IS NULL "
        "AND s.archived_at IS NULL AND u.company_id = s.company_id AND NOT m.external").fetchall()


def recent_activity(conn, days):
    """Topics per (user, site) over the last `days` days: how a person with
    several sites is told to have a primary one. Deleted/hidden topics are not
    distinguished -- this only ranks sites against each other."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT user_id, site_id, count(*) AS n, "
        "max(coalesce(occurred_at, report_date::timestamptz)) AS latest "
        "FROM topics WHERE user_id IS NOT NULL "
        "AND report_date >= current_date - %s::int GROUP BY user_id, site_id",
        (int(days),)).fetchall()
