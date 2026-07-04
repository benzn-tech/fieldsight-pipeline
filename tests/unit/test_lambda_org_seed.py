import pytest

seed = pytest.importorskip("lambda_org_seed", reason="requires psycopg (installed in CI)")


COGNITO_USERS = [
    {"Attributes": [{"Name": "sub", "Value": "sub-admin"},
                    {"Name": "email", "Value": "benl.tech@outlook.com"},
                    {"Name": "name", "Value": "Ben Lin"}]},
    {"Attributes": [{"Name": "sub", "Value": "sub-jt"},
                    {"Name": "email", "Value": "benlin.chch+jt@gmail.com"},
                    {"Name": "name", "Value": "Jarley Trainor"}]},
]

MAPPING = {
    "sites": {"sb1108-ellesmere": {"name": "SB1108 Ellesmere College",
                                   "location": "Christchurch",
                                   "client": "Ministry of Education"}},
    "mapping": {"Benl1": {"name": "Jarley Trainor", "role": "site_manager",
                          "sites": ["sb1108-ellesmere"]}},
}


def test_resolve_role_admin_override_beats_mapping():
    by_name = seed.mapping_by_name(MAPPING)
    assert seed.resolve_role("benl.tech@outlook.com", "Ben Lin",
                             {"benl.tech@outlook.com"}, by_name) == "admin"
    assert seed.resolve_role("benlin.chch+jt@gmail.com", "Jarley Trainor",
                             set(), by_name) == "site_manager"
    assert seed.resolve_role("x@x.nz", "Nobody Known", set(), by_name) == "worker"


def test_split_name():
    assert seed.split_name("Jarley Trainor") == ("Jarley", "Trainor")
    assert seed.split_name("MPI1") == ("MPI1", None)
    assert seed.split_name("") == (None, None)


def test_handler_seeds_company_users_sites_memberships(monkeypatch):
    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls = {"users": [], "sites": [], "memberships": []}
    monkeypatch.setattr(seed, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(seed, "load_mapping", lambda: MAPPING)
    monkeypatch.setattr(seed, "list_cognito_users", lambda: COGNITO_USERS)
    monkeypatch.setattr(seed.companies, "get_company_by_name", lambda c, n: None)
    monkeypatch.setattr(seed.companies, "create_company",
                        lambda c, n: {"id": "c-1", "name": n})
    monkeypatch.setattr(seed.sites, "get_company_site_by_name", lambda c, cid, n: None)
    monkeypatch.setattr(seed.sites, "create_site",
                        lambda c, cid, name, **kw: (calls["sites"].append(name)
                                                    or {"id": "s-" + name[:6], "name": name}))
    monkeypatch.setattr(seed.users, "upsert_user",
                        lambda c, sub, email, **kw: (calls["users"].append((sub, kw.get("global_role")))
                                                     or {"id": "u-" + sub, "cognito_sub": sub}))
    monkeypatch.setattr(seed.memberships, "ensure_membership",
                        lambda c, uid, sid, role: (calls["memberships"].append((uid, sid, role))
                                                   or {"id": "m-1"}))

    out = seed.lambda_handler({"company_name": "TestCo"}, None)
    assert out["company"]["name"] == "TestCo"
    assert ("sub-admin", "admin") in calls["users"]
    assert ("sub-jt", "site_manager") in calls["users"]
    assert calls["sites"] == ["SB1108 Ellesmere College"]
    assert calls["memberships"] == [("u-sub-jt", "s-SB1108", "site_manager")]
    assert out["users"] == 2 and out["sites"] == 1 and out["memberships"] == 1
