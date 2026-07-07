import json
import uuid

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

    calls = {"users": [], "sites": [], "memberships": [],
             "folder_names": [], "field_only": []}
    monkeypatch.setattr(seed, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(seed, "load_mapping", lambda: MAPPING)
    monkeypatch.setattr(seed, "list_cognito_users", lambda: COGNITO_USERS)
    monkeypatch.setattr(seed.companies, "get_company_by_name", lambda c, n: None)
    monkeypatch.setattr(seed.companies, "create_company",
                        lambda c, n: {"id": uuid.UUID("dc2eafa9-1260-4bd9-8d65-862f47dacb3c"), "name": n})
    monkeypatch.setattr(seed.sites, "get_company_site_by_name", lambda c, cid, n: None)
    monkeypatch.setattr(seed.sites, "create_site",
                        lambda c, cid, name, **kw: (calls["sites"].append(name)
                                                    or {"id": "s-" + name[:6], "name": name}))
    monkeypatch.setattr(seed.sites, "set_slug",
                        lambda c, sid, slug: {"id": sid, "slug": slug})
    monkeypatch.setattr(seed.users, "upsert_user",
                        lambda c, sub, email, **kw: (calls["users"].append((sub, kw.get("global_role")))
                                                     or {"id": "u-" + sub, "cognito_sub": sub}))
    monkeypatch.setattr(seed.users, "set_folder_name",
                        lambda c, sub, folder_name: calls["folder_names"].append((sub, folder_name)))
    monkeypatch.setattr(seed.users, "upsert_field_only_user",
                        lambda c, cid, folder_name, first_name, last_name, global_role:
                            (calls["field_only"].append(folder_name)
                             or {"id": "fo-" + folder_name}))
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
    assert isinstance(out["company"]["id"], str)  # UUID coerced to str
    json.dumps(out)  # returned dict must be JSON-serializable (Lambda marshals it)
    # folder_name backfilled for every login user, from their Cognito name
    assert ("sub-admin", "Ben_Lin") in calls["folder_names"]
    assert ("sub-jt", "Jarley_Trainor") in calls["folder_names"]
    # Jarley Trainor is already a Cognito login -> not re-enrolled as field_only
    assert calls["field_only"] == []
    assert out["sites_backfilled"] == 1
    assert out["login_folder_set"] == 2
    assert out["field_only_enrolled"] == 0


# ---------------------------------------------------------------------------
# Task 2: slug backfill / folder_name / field_only enrollment (second pass)
# ---------------------------------------------------------------------------

FULL_MAPPING = {
    "sites": {
        "sb1108-ellesmere": {"name": "SB1108 Ellesmere College",
                              "location": "Christchurch",
                              "client": "Ministry of Education"},
        "mpi": {"name": "MPI", "location": "Auckland",
                "client": "Ministry for Primary Industries"},
        "sb1131-northbrook-wanaka": {"name": "SB1131 - Northbrook Wanaka",
                                      "location": "Wanaka", "client": "Northbrook"},
    },
    "mapping": {
        "Benl1": {"name": "Jarley Trainor", "role": "site_manager",
                  "sites": ["sb1108-ellesmere"]},
        "Benl2": {"name": "MPI1", "role": "worker", "sites": ["mpi"]},
        "Benl3": {"name": "David Barillaro", "role": "site_manager",
                  "sites": ["sb1108-ellesmere"]},
        "Benl4": {"name": "MPI2", "role": "worker", "sites": ["mpi"]},
        "Benl5": {"name": "James Lamb", "role": "site_manager",
                  "sites": ["sb1131-northbrook-wanaka"]},
        "Benl6": {"name": "Jack Gibson", "role": "site_manager",
                  "sites": ["sb1131-northbrook-wanaka"]},
    },
}

FULL_COGNITO_USERS = [
    {"Attributes": [{"Name": "sub", "Value": "sub-jt"},
                    {"Name": "email", "Value": "jt@example.com"},
                    {"Name": "name", "Value": "Jarley Trainor"}]},
    {"Attributes": [{"Name": "sub", "Value": "sub-db"},
                    {"Name": "email", "Value": "db@example.com"},
                    {"Name": "name", "Value": "David Barillaro"}]},
]


class FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _install_fake_repo(monkeypatch, mapping, cognito_users):
    """Stateful fake repo layer simulating ON CONFLICT / UPDATE idempotency
    (unlike the simpler append-only mocks above), so lambda_handler can be
    invoked more than once to exercise re-run behaviour. Returns (calls,
    state) dicts for assertions."""
    calls = {"create_site": [], "set_slug": [], "upsert_user": [],
             "set_folder_name": [], "upsert_field_only_user": [],
             "ensure_membership": []}
    state = {"sites_by_name": {}, "sites_by_id": {}, "users_by_sub": {},
             "users_by_folder": {}, "memberships": {}}

    monkeypatch.setattr(seed, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(seed, "load_mapping", lambda: mapping)
    monkeypatch.setattr(seed, "list_cognito_users", lambda: cognito_users)
    monkeypatch.setattr(seed.companies, "get_company_by_name", lambda c, n: None)
    monkeypatch.setattr(seed.companies, "create_company",
                        lambda c, n: {"id": "company-1", "name": n})

    def fake_get_site_by_name(c, cid, name):
        return state["sites_by_name"].get(name)

    def fake_create_site(c, cid, name, **kw):
        calls["create_site"].append((name, kw.get("slug")))
        site = {"id": f"site-{len(state['sites_by_id']) + 1}", "name": name,
                "slug": kw.get("slug")}
        state["sites_by_name"][name] = site
        state["sites_by_id"][site["id"]] = site
        return site

    def fake_set_slug(c, site_id, slug):
        calls["set_slug"].append((site_id, slug))
        site = state["sites_by_id"][site_id]
        site["slug"] = slug
        return site

    monkeypatch.setattr(seed.sites, "get_company_site_by_name", fake_get_site_by_name)
    monkeypatch.setattr(seed.sites, "create_site", fake_create_site)
    monkeypatch.setattr(seed.sites, "set_slug", fake_set_slug)

    def fake_upsert_user(c, sub, email, **kw):
        calls["upsert_user"].append((sub, kw.get("global_role")))
        user = state["users_by_sub"].get(sub) or {"id": f"user-{len(state['users_by_sub']) + 1}"}
        user.update({"cognito_sub": sub, "email": email, **kw})
        state["users_by_sub"][sub] = user
        return user

    def fake_set_folder_name(c, sub, folder_name):
        calls["set_folder_name"].append((sub, folder_name))
        state["users_by_sub"][sub]["folder_name"] = folder_name
        return None

    def fake_upsert_field_only_user(c, company_id, folder_name, first_name,
                                     last_name, global_role):
        calls["upsert_field_only_user"].append((folder_name, global_role))
        user = state["users_by_folder"].get(folder_name) or \
            {"id": f"field-{len(state['users_by_folder']) + 1}"}
        user.update({"folder_name": folder_name, "kind": "field_only",
                     "first_name": first_name, "last_name": last_name,
                     "global_role": global_role})
        state["users_by_folder"][folder_name] = user
        return user

    monkeypatch.setattr(seed.users, "upsert_user", fake_upsert_user)
    monkeypatch.setattr(seed.users, "set_folder_name", fake_set_folder_name)
    monkeypatch.setattr(seed.users, "upsert_field_only_user", fake_upsert_field_only_user)

    def fake_ensure_membership(c, user_id, site_id, role):
        calls["ensure_membership"].append((user_id, site_id, role))
        key = (user_id, site_id)
        m = state["memberships"].get(key) or {"id": f"m-{len(state['memberships']) + 1}"}
        m.update({"user_id": user_id, "site_id": site_id, "role": role})
        state["memberships"][key] = m
        return m

    monkeypatch.setattr(seed.memberships, "ensure_membership", fake_ensure_membership)

    return calls, state


def test_backfills_slug_on_existing_site(monkeypatch):
    calls, state = _install_fake_repo(monkeypatch, FULL_MAPPING, [])
    existing = {"id": "site-existing", "name": "MPI", "slug": None}
    state["sites_by_name"]["MPI"] = existing
    state["sites_by_id"]["site-existing"] = existing

    seed.lambda_handler({"company_name": "TestCo"}, None)

    assert ("site-existing", "mpi") in calls["set_slug"]
    assert all(name != "MPI" for name, _ in calls["create_site"])
    assert existing["slug"] == "mpi"


def test_sets_folder_name_on_login_user(monkeypatch):
    calls, state = _install_fake_repo(monkeypatch, FULL_MAPPING, FULL_COGNITO_USERS)

    seed.lambda_handler({"company_name": "TestCo"}, None)

    assert ("sub-jt", "Jarley_Trainor") in calls["set_folder_name"]
    assert ("sub-db", "David_Barillaro") in calls["set_folder_name"]


def test_enrolls_field_only_for_non_cognito_mapping_name(monkeypatch):
    calls, state = _install_fake_repo(monkeypatch, FULL_MAPPING, FULL_COGNITO_USERS)

    seed.lambda_handler({"company_name": "TestCo"}, None)

    enrolled = {name for name, role in calls["upsert_field_only_user"]}
    assert enrolled == {"MPI1", "MPI2", "James_Lamb", "Jack_Gibson"}
    assert ("James_Lamb", "site_manager") in calls["upsert_field_only_user"]

    james = state["users_by_folder"]["James_Lamb"]
    wanaka_site = state["sites_by_name"]["SB1131 - Northbrook Wanaka"]
    m = state["memberships"][(james["id"], wanaka_site["id"])]
    assert m["role"] == "site_manager"


def test_field_only_skipped_when_name_is_cognito_user(monkeypatch):
    calls, state = _install_fake_repo(monkeypatch, FULL_MAPPING, FULL_COGNITO_USERS)

    seed.lambda_handler({"company_name": "TestCo"}, None)

    enrolled = {name for name, role in calls["upsert_field_only_user"]}
    assert "Jarley_Trainor" not in enrolled
    assert "David_Barillaro" not in enrolled
    assert "David_Barillaro" not in state["users_by_folder"]


def test_idempotent_rerun(monkeypatch):
    calls, state = _install_fake_repo(monkeypatch, FULL_MAPPING, FULL_COGNITO_USERS)

    seed.lambda_handler({"company_name": "TestCo"}, None)
    n_sites_after_first = len(state["sites_by_id"])
    n_users_after_first = len(state["users_by_sub"])
    n_field_only_after_first = len(state["users_by_folder"])
    n_memberships_after_first = len(state["memberships"])

    seed.lambda_handler({"company_name": "TestCo"}, None)

    # no new rows created on the second run -- ON CONFLICT / UPDATE paths only
    assert len(state["sites_by_id"]) == n_sites_after_first == 3
    assert len(state["users_by_sub"]) == n_users_after_first
    assert len(state["users_by_folder"]) == n_field_only_after_first == 4
    assert len(state["memberships"]) == n_memberships_after_first
    assert len(calls["create_site"]) == 3  # create_site never called again on rerun


def test_summary_counts(monkeypatch):
    calls, state = _install_fake_repo(monkeypatch, FULL_MAPPING, FULL_COGNITO_USERS)

    out = seed.lambda_handler({"company_name": "TestCo"}, None)

    assert out["sites_backfilled"] == 3
    assert out["login_folder_set"] == 2
    assert out["field_only_enrolled"] == 4
    json.dumps(out)
