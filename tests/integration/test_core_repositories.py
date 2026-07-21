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

    s = sites.create_site(db, co["id"], "North Wharf", location="Auckland",
                          address="12 Queen St")
    assert sites.get_site(db, s["id"])["name"] == "North Wharf"
    assert sites.get_site(db, s["id"])["address"] == "12 Queen St"
    assert [x["id"] for x in sites.list_company_sites(db, co["id"])] == [s["id"]]
    assert [x["address"] for x in sites.list_company_sites(db, co["id"])] == ["12 Queen St"]


def test_site_coordinates_create_and_update(db):
    co = companies.create_company(db, "GeoCo")
    s = sites.create_site(db, co["id"], "Depot", address="1 Colombo St",
                          latitude=-43.5321, longitude=172.6362)
    assert s["latitude"] == -43.5321 and s["longitude"] == 172.6362
    got = sites.get_site(db, s["id"])
    assert got["latitude"] == -43.5321 and got["longitude"] == 172.6362

    # update_site: None leaves a column unchanged (COALESCE), a value overwrites
    upd = sites.update_site(db, s["id"], co["id"], latitude=-41.2865, longitude=174.7762)
    assert upd["latitude"] == -41.2865 and upd["longitude"] == 174.7762
    assert upd["address"] == "1 Colombo St"  # untouched column preserved


def test_upsert_user_partial_update_preserves_role_and_company(db):
    co = companies.create_company(db, "Acme")
    u1 = users.upsert_user(db, "sub-pm", "pm@acme.com", company_id=co["id"], global_role="pm")
    assert u1["global_role"] == "pm"
    # login-sync style call: only sub + email — must not demote or detach
    u2 = users.upsert_user(db, "sub-pm", "pm@acme.com")
    assert u2["global_role"] == "pm", "partial upsert must not demote role"
    assert u2["company_id"] == co["id"], "partial upsert must not clear company"


@pytest.mark.integration
def test_list_company_users_scoped_to_company(db):
    c1 = companies.create_company(db, "ListCo A")
    c2 = companies.create_company(db, "ListCo B")
    u1 = users.upsert_user(db, "sub-lc-1", "a@x.nz", company_id=c1["id"])
    users.upsert_user(db, "sub-lc-2", "b@x.nz", company_id=c2["id"])
    rows = users.list_company_users(db, c1["id"])
    subs = [r["cognito_sub"] for r in rows]
    assert "sub-lc-1" in subs and "sub-lc-2" not in subs


@pytest.mark.integration
def test_set_global_role_explicit_and_company_guarded(db):
    c1 = companies.create_company(db, "RoleCo A")
    c2 = companies.create_company(db, "RoleCo B")
    users.upsert_user(db, "sub-rl-1", "r@x.nz", company_id=c1["id"], global_role="worker")
    # explicit set works within the company
    row = users.set_global_role(db, "sub-rl-1", c1["id"], "pm")
    assert row["global_role"] == "pm"
    # cross-company set returns None and does not change the row
    assert users.set_global_role(db, "sub-rl-1", c2["id"], "admin") is None
    assert users.get_user_by_sub(db, "sub-rl-1")["global_role"] == "pm"


@pytest.mark.integration
def test_update_profile_none_preserving(db):
    c1 = companies.create_company(db, "ProfCo")
    users.upsert_user(db, "sub-pf-1", "p@x.nz", company_id=c1["id"],
                first_name="Old", last_name="Name")
    row = users.update_profile(db, "sub-pf-1", first_name="New",
                         avatar_s3_key="org-assets/avatars/sub-pf-1/a.jpg")
    assert row["first_name"] == "New"
    assert row["last_name"] == "Name"  # None = unchanged
    assert row["avatar_s3_key"] == "org-assets/avatars/sub-pf-1/a.jpg"
    assert users.update_profile(db, "sub-does-not-exist", first_name="X") is None


def test_topic_work_class_roundtrip(db):
    import repositories.topics as topics
    from repositories import companies, sites
    co = companies.create_company(db, "WC-Co")
    s = sites.create_site(db, co["id"], "WC-Site")
    row = topics.upsert_topic(
        db, s["id"], "2026-07-21", "Lunch chat",
        work_class="non_work", work_confidence=0.91, is_mixed=True)
    assert row["work_class"] == "non_work"
    assert abs(row["work_confidence"] - 0.91) < 1e-6
    assert row["is_mixed"] is True
    got = topics.list_site_topics(db, s["id"], "2026-07-21")[0]
    assert got["work_class"] == "non_work" and got["is_mixed"] is True
