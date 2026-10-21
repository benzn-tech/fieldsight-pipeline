"""Ask tools people SQL (spec 2026-10-15-ask-retrieval-tools §3.1 item 4), against a real PostgreSQL.

Skipped without TEST_DATABASE_URL. A SKIP IS NOT A PASS — read the CI run.
"""
import pytest
from repositories import companies, sites, topics, users, recordings

pytestmark = pytest.mark.integration


def _user(db, co, folder, first, last=None):
    u = users.upsert_user(db, f"sub-{folder}", f"{folder}@x.nz", company_id=co["id"],
                          first_name=first, last_name=last)
    db.execute("UPDATE users SET folder_name=%s WHERE id=%s", (folder, u["id"]))
    return u


@pytest.fixture
def w(db):
    a = companies.create_company(db, "A")
    b = companies.create_company(db, "B")
    sa = sites.create_site(db, a["id"], "SA")
    sb = sites.create_site(db, b["id"], "SB")
    mike1 = _user(db, a, "Mike_Smith", "Mike", "Smith")
    mike2 = _user(db, a, "Mike_Tane", "Mike", "Tane")
    bmike = _user(db, b, "Mike_B", "Mike", "Brown")
    for u, s in ((mike1, sa), (mike2, sa), (bmike, sb)):
        topics.upsert_topic(db, s["id"], "2026-10-14", "x", user_id=u["id"],
                            source_s3_key="extractions/2026-10-14/x/a.json")
    return {"a": a, "b": b, "sa": sa, "sb": sb, "mike1": mike1, "mike2": mike2, "bmike": bmike}


def test_two_mikes_both_returned_other_company_never(db, w):
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "mike")
    assert sorted(p["name"] for p in got) == ["Mike Smith", "Mike Tane"]


def test_result_shape(db, w):
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "Mike Smith")
    assert got == [{"person_ref": str(w["mike1"]["id"]), "name": "Mike Smith",
                    "folder_name": "Mike_Smith"}]


def test_folder_name_matches_case_insensitive(db, w):
    for q in ("mike_tane", "MIKE TANE", "  Mike_Tane "):
        got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, q)
        assert [p["name"] for p in got] == ["Mike Tane"], q


def test_match_is_exact_not_substring(db, w):
    for q in ("Mi", "ike", "Mike S", "Smith"):
        assert users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, q) == [], q


def test_graded_author_set_narrows(db, w):
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]],
                                    {str(w["mike1"]["id"])}, "mike")
    assert [p["name"] for p in got] == ["Mike Smith"]


def test_person_alias_resolves(db, w):
    db.execute("INSERT INTO name_aliases (company_id, wrong_term, right_term, kind) "
               "VALUES (%s, 'Mikey', 'Mike Tane', 'person')", (w["a"]["id"],))
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "mikey")
    assert [p["name"] for p in got] == ["Mike Tane"]


def test_alias_of_other_company_does_not_resolve(db, w):
    # Company B's glossary says Mikey = Mike Tane; company A's caller must not see that.
    db.execute("INSERT INTO name_aliases (company_id, wrong_term, right_term, kind) "
               "VALUES (%s, 'Mikey', 'Mike Tane', 'person')", (w["b"]["id"],))
    assert users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "mikey") == []


def test_alias_retired_or_non_person_does_not_resolve(db, w):
    db.execute("INSERT INTO name_aliases (company_id, wrong_term, right_term, kind, status) "
               "VALUES (%s, 'Mikey', 'Mike Tane', 'person', 'retired'), "
               "(%s, 'Mikey', 'Mike Smith', 'product', 'active')",
               (w["a"]["id"], w["a"]["id"]))
    assert users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "mikey") == []


def test_alias_scoped_to_unreachable_site_does_not_resolve(db, w):
    other = sites.create_site(db, w["a"]["id"], "SA2")
    db.execute("INSERT INTO name_aliases (company_id, site_id, wrong_term, right_term, kind) "
               "VALUES (%s, %s, 'Mikey', 'Mike Tane', 'person')", (w["a"]["id"], other["id"]))
    assert users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "mikey") == []


def test_external_author_with_footprint_is_found(db, w):
    # A company-B person who recorded on company A's site: findable by A's caller,
    # because the user lookup is by id across companies, not company-pinned.
    ext = _user(db, w["b"], "Rangi_Ext", "Rangi", "Parata")
    topics.upsert_topic(db, w["sa"]["id"], "2026-10-14", "y", user_id=ext["id"],
                        source_s3_key="extractions/2026-10-14/y/a.json")
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "rangi")
    assert [p["person_ref"] for p in got] == [str(ext["id"])]
    assert users.person_in_reach(db, [w["sa"]["id"]], None, str(ext["id"]))


def test_other_company_person_without_footprint_never_found(db, w):
    assert users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "Mike Brown") == []
    assert users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "Mike_B") == []


def test_archived_user_with_footprint_is_still_found(db, w):
    # Mirrors visibility: scope/author_footprint never filter on users.archived_at,
    # so an archived person's history stays readable and they stay findable.
    db.execute("UPDATE users SET archived_at=now() WHERE id=%s", (w["mike2"]["id"],))
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "Mike Tane")
    assert [p["name"] for p in got] == ["Mike Tane"]
    assert users.person_in_reach(db, [w["sa"]["id"]], None, str(w["mike2"]["id"]))


def test_empty_inputs_return_nothing(db, w):
    assert users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "  ") == []
    assert users.find_visible_people(db, w["a"]["id"], [], None, "mike") == []
    assert not users.person_in_reach(db, [w["sa"]["id"]], None, None)
    assert not users.person_in_reach(db, [w["sa"]["id"]], None, "")


def test_person_in_reach_rejects_other_company_ref(db, w):
    assert users.person_in_reach(db, [w["sa"]["id"]], None, str(w["mike1"]["id"]))
    assert not users.person_in_reach(db, [w["sa"]["id"]], None, str(w["bmike"]["id"]))
    assert not users.person_in_reach(db, [w["sa"]["id"]], {str(w["mike2"]["id"])},
                                     str(w["mike1"]["id"]))


def test_candidates_include_live_membership_only_people(db, w):
    lone = _user(db, w["a"], "New_Hire", "New", "Hire")
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s, %s, 'worker')",
               (lone["id"], w["sa"]["id"]))
    assert str(lone["id"]) in recordings.author_candidates_in_sites(db, [w["sa"]["id"]])


def test_candidates_exclude_archived_membership(db, w):
    gone = _user(db, w["a"], "Old_Hand", "Old", "Hand")
    db.execute("INSERT INTO memberships (user_id, site_id, role, archived_at) "
               "VALUES (%s, %s, 'worker', now())", (gone["id"], w["sa"]["id"]))
    assert str(gone["id"]) not in recordings.author_candidates_in_sites(db, [w["sa"]["id"]])


# ---- fix round 1 -------------------------------------------------------------------

def _rec(db, co, user, site, key):
    return recordings.insert_pending(db, co["id"], user["id"], site["id"], "audio",
                                     f"users/x/audio/2026-10-14/{key}.wav", key,
                                     "2026-10-14T01:00:00Z")


def test_recording_only_author_is_a_candidate_and_findable(db, w):
    rec_only = _user(db, w["a"], "Tama_Rec", "Tama", "Rec")
    _rec(db, w["a"], rec_only, w["sa"], "tama-1")
    assert str(rec_only["id"]) in recordings.author_candidates_in_sites(db, [w["sa"]["id"]])
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "tama")
    assert [p["person_ref"] for p in got] == [str(rec_only["id"])]
    assert users.person_in_reach(db, [w["sa"]["id"]], None, str(rec_only["id"]))


def test_null_user_topic_is_not_a_candidate_and_does_not_crash(db, w):
    topics.upsert_topic(db, w["sa"]["id"], "2026-10-14", "orphan", user_id=None,
                        source_s3_key="reports/2026-10-14/x/daily_report.json")
    cands = recordings.author_candidates_in_sites(db, [w["sa"]["id"]])
    assert "None" not in cands and None not in cands
    assert cands == {str(w["mike1"]["id"]), str(w["mike2"]["id"])}
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "mike")
    assert sorted(p["name"] for p in got) == ["Mike Smith", "Mike Tane"]


def test_recordings_user_id_cannot_be_null(db, w):
    # The recordings arm's `user_id IS NOT NULL` is defence in depth: the schema
    # (0009) already forbids a NULL author, so no such row can exist to test against.
    import psycopg
    with pytest.raises(psycopg.errors.NotNullViolation):
        with db.transaction():
            db.execute("INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, "
                       "client_uuid, started_at) VALUES (%s, NULL, %s, 'audio', 'k', 'c', now())",
                       (w["a"]["id"], w["sa"]["id"]))


def test_person_in_reach_malformed_ref_is_false_without_db(db, w):
    class _NoDb:
        def __getattr__(self, name):
            raise AssertionError(f"DB touched: {name}")
    for ref in ("not-a-uuid", "None", "123", "'; drop table users; --", None, "", 42):
        assert users.person_in_reach(_NoDb(), [w["sa"]["id"]], None, ref) is False, ref


def test_person_in_reach_accepts_canonicalisable_ref(db, w):
    ref = str(w["mike1"]["id"])
    assert users.person_in_reach(db, [w["sa"]["id"]], None, ref.upper())
    assert users.person_in_reach(db, [w["sa"]["id"]], {ref}, "{" + ref + "}")
    assert users.person_in_reach(db, [w["sa"]["id"]], None, w["mike1"]["id"])


def test_stored_double_space_name_matches(db, w):
    mj = _user(db, w["a"], "Mary_Jane", "Mary  Jane", "Ruru")
    topics.upsert_topic(db, w["sa"]["id"], "2026-10-14", "z", user_id=mj["id"],
                        source_s3_key="extractions/2026-10-14/z/a.json")
    for q in ("mary jane", "Mary  Jane", "mary jane ruru", "MARY_JANE"):
        got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, q)
        assert [p["person_ref"] for p in got] == [str(mj["id"])], q


def test_double_underscore_folder_matches(db, w):
    ab = _user(db, w["a"], "Aroha__Brown", "Aroha", None)
    topics.upsert_topic(db, w["sa"]["id"], "2026-10-14", "z", user_id=ab["id"],
                        source_s3_key="extractions/2026-10-14/z/b.json")
    for q in ("aroha brown", "Aroha__Brown", "aroha_brown", "aroha  brown"):
        got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, q)
        assert [p["person_ref"] for p in got] == [str(ab["id"])], q


def test_alias_terms_normalised_like_names(db, w):
    db.execute("INSERT INTO name_aliases (company_id, wrong_term, right_term, kind) "
               "VALUES (%s, ' Big__Mike ', 'mike_  tane', 'person')", (w["a"]["id"],))
    for q in ("big mike", "Big  Mike", "big_mike"):
        got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, q)
        assert [p["name"] for p in got] == ["Mike Tane"], q
