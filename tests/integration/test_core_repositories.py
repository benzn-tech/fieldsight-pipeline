import pytest

pytestmark = pytest.mark.integration


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_core_tables_exist_with_key_columns(db):
    assert {"id", "name", "industry", "created_at"} <= _columns(db, "companies")
    assert {"id", "cognito_sub", "company_id", "email", "global_role"} <= _columns(db, "users")
    assert {"id", "company_id", "name", "location", "icon_s3_key"} <= _columns(db, "sites")
    assert {"id", "user_id", "site_id", "role"} <= _columns(db, "memberships")


def test_membership_unique_user_site(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role) "
        "VALUES ('sub1', %s, 'a@x.com', 'worker') RETURNING id", (cid,)).fetchone()[0]
    sid = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id", (cid,)).fetchone()[0]
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'worker')", (uid, sid))
    with pytest.raises(Exception):
        db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'pm')", (uid, sid))


from repositories import companies, users, sites


def test_company_user_site_roundtrip(db):
    co = companies.create_company(db, "Acme", industry="construction")
    assert co["name"] == "Acme" and co["id"]

    u1 = users.upsert_user(db, "sub-9", "a@acme.com", company_id=co["id"], global_role="pm")
    u2 = users.upsert_user(db, "sub-9", "a@acme.com", company_id=co["id"], first_name="Ann")
    assert u1["id"] == u2["id"], "upsert by cognito_sub must not create a duplicate"
    assert users.get_user_by_sub(db, "sub-9")["first_name"] == "Ann"

    s = sites.create_site(db, co["id"], "North Wharf", location="Auckland")
    assert sites.get_site(db, s["id"])["name"] == "North Wharf"
    assert [x["id"] for x in sites.list_company_sites(db, co["id"])] == [s["id"]]


def test_upsert_user_partial_update_preserves_role_and_company(db):
    co = companies.create_company(db, "Acme")
    u1 = users.upsert_user(db, "sub-pm", "pm@acme.com", company_id=co["id"], global_role="pm")
    assert u1["global_role"] == "pm"
    # login-sync style call: only sub + email — must not demote or detach
    u2 = users.upsert_user(db, "sub-pm", "pm@acme.com")
    assert u2["global_role"] == "pm", "partial upsert must not demote role"
    assert u2["company_id"] == co["id"], "partial upsert must not clear company"
