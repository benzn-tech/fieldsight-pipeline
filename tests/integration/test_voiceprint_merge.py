"""Integration: migration 0078 and the merge / rename / identity SQL, against real Postgres.

Skipped unless TEST_DATABASE_URL is set; rolled back per test (the `db` fixture). A connection
double records statements and parses none of them, so it cannot show that the sample-collision
DELETE really clears the 0070 unique index before the UPDATE, that the proposal DELETE really
matches the UNIQUE key, or that the listing's GROUP BY is accepted by Postgres at all.
Spec: docs/superpowers/specs/2026-10-01-voices-identity-and-merge-design.md.
"""
import pytest

from repositories import voiceprints as vp

pytestmark = pytest.mark.integration

SID = "sid" + "a" * 32


def _vec(i, scale=1.0):
    """A 192-dim literal with one hot component: orthogonal for different `i`."""
    v = [0.0] * 192
    v[i] = scale
    return "[" + ",".join(str(x) for x in v) + "]"


def _company(db):
    return db.execute("INSERT INTO companies (name) VALUES ('Merge Co ' || gen_random_uuid()::text) RETURNING id"
                      ).fetchone()[0]


def _user(db, cid, first, last, folder=None):
    email = f"{first}.{last}@example.com".lower()
    return db.execute(
        "INSERT INTO users (company_id, cognito_sub, first_name, last_name, email, "
        " folder_name) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (cid, "sub-" + email, first, last, email, folder)).fetchone()[0]


def _profile(db, cid, name="Ben Lin", **cols):
    status = cols.pop("status", "confirmed")
    keys = ["company_id", "display_name", "status"] + list(cols)
    vals = [cid, name, status] + list(cols.values())
    return db.execute(
        f"INSERT INTO speaker_voiceprints ({', '.join(keys)}) "
        f"VALUES ({', '.join(['%s'] * len(keys))}) RETURNING id", vals).fetchone()[0]


def _sample(db, cid, pid, key, start, end, vec=None, quarantined=False, source="enrolment"):
    return db.execute(
        "INSERT INTO speaker_voiceprint_samples (company_id, voiceprint_id, embedding, "
        " source, s3_key, window_start_s, window_end_s, quarantined_at) "
        "VALUES (%s, %s, %s::vector, %s, %s, %s, %s, "
        "        CASE WHEN %s THEN now() END) RETURNING id",
        (cid, pid, vec or _vec(0), source, key, start, end, quarantined)).fetchone()[0]


def _count(db, sql, params):
    return db.execute(sql, params).fetchone()[0]


def test_migration_adds_the_three_merge_columns(db):
    cols = {r[0]: r[1] for r in db.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'speaker_voiceprints' AND column_name LIKE 'merged_%'"
    ).fetchall()}
    assert cols == {"merged_into": "uuid", "merged_at": "timestamp with time zone",
                    "merged_by": "uuid"}


def test_sample_collision_is_dropped_and_the_rest_move(db):
    cid = _company(db)
    src, tgt = _profile(db, cid), _profile(db, cid)
    # Same window as the target's, though the floats differ: round() is the unique key.
    _sample(db, cid, tgt, "users/A/x.wav", 10.2, 20.4)
    _sample(db, cid, src, "users/A/x.wav", 10.4, 20.1)      # collides after rounding
    _sample(db, cid, src, "users/A/x.wav", 30.0, 40.0)      # does not
    _sample(db, cid, src, "users/B/y.wav", 10.2, 20.4)      # other file: does not
    res = vp.merge_profiles(db, cid, src, tgt, merged_by=None)
    assert res == {"samplesMoved": 2, "samplesDropped": 1}
    assert _count(db, "SELECT count(*) FROM speaker_voiceprint_samples "
                      "WHERE voiceprint_id = %s", (tgt,)) == 3
    assert _count(db, "SELECT count(*) FROM speaker_voiceprint_samples "
                      "WHERE voiceprint_id = %s", (src,)) == 0


def test_a_quarantined_target_sample_also_blocks_the_window(db):
    """0070's index has no quarantine filter, so the collision delete must not either."""
    cid = _company(db)
    src, tgt = _profile(db, cid), _profile(db, cid)
    _sample(db, cid, tgt, "users/A/x.wav", 1, 11, quarantined=True)
    _sample(db, cid, src, "users/A/x.wav", 1, 11)
    assert vp.merge_profiles(db, cid, src, tgt, None) == {"samplesMoved": 0,
                                                          "samplesDropped": 1}


def test_proposal_collision_is_dropped_and_the_rest_repoint(db):
    cid = _company(db)
    src, tgt = _profile(db, cid), _profile(db, cid)
    ins = ("INSERT INTO speaker_name_proposals (company_id, voiceprint_id, session_base, "
           " source_filename, speaker_label, user_folder, session_date) "
           "VALUES (%s, %s, %s, %s, %s, 'F', '2026-09-30')")
    db.execute(ins, (cid, tgt, SID, "f_c0000.json", "spk_0"))
    db.execute(ins, (cid, src, SID, "f_c0000.json", "spk_0"))   # collides with the target's
    db.execute(ins, (cid, src, SID, "f_c0001.json", "spk_0"))   # does not
    vp.merge_profiles(db, cid, src, tgt, None)
    assert _count(db, "SELECT count(*) FROM speaker_name_proposals "
                      "WHERE voiceprint_id = %s", (tgt,)) == 2
    assert _count(db, "SELECT count(*) FROM speaker_name_proposals "
                      "WHERE voiceprint_id = %s", (src,)) == 0


def test_turn_names_follow_the_id_and_take_the_target_name_never_by_name(db):
    cid = _company(db)
    src = _profile(db, cid, "Ben L")
    tgt = _profile(db, cid, "Ben Lin")
    other = _profile(db, cid, "Ben L")           # a different person who shares the name
    ins = ("INSERT INTO speaker_turn_names (company_id, voiceprint_id, session_base, "
           " turn_ref, state, display_name) VALUES (%s, %s, %s, %s, 'confirmed', %s)")
    db.execute(ins, (cid, src, SID, "a@1.0", "Ben L"))
    db.execute(ins, (cid, other, SID, "a@2.0", "Ben L"))
    vp.merge_profiles(db, cid, src, tgt, None)
    rows = dict(db.execute(
        "SELECT turn_ref, voiceprint_id::text || '|' || display_name "
        "FROM speaker_turn_names WHERE company_id = %s", (cid,)).fetchall())
    assert rows["a@1.0"] == f"{tgt}|Ben Lin"
    assert rows["a@2.0"] == f"{other}|Ben L"


def test_site_attendance_is_repointed(db):
    cid = _company(db)
    site = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id",
                      (cid,)).fetchone()[0]
    src, tgt = _profile(db, cid), _profile(db, cid)
    db.execute(
        "INSERT INTO site_attendance (company_id, site_id, attend_date, display_name, "
        " voiceprint_id, source, source_ref) "
        "VALUES (%s, %s, '2026-09-30', 'Ben Lin', %s, 'manual', 'ben')", (cid, site, src))
    vp.merge_profiles(db, cid, src, tgt, None)
    assert db.execute("SELECT voiceprint_id FROM site_attendance WHERE company_id = %s",
                      (cid,)).fetchone()[0] == tgt


def test_target_inherits_link_employer_and_external_ref_when_it_has_none(db):
    cid = _company(db)
    uid = _user(db, cid, "Ben", "Lin")
    src = _profile(db, cid, user_id=uid, linked_on="folder_name",
                   employer_name="ABC Ltd", employer_source="typed",
                   external_ref="sos-1", external_source="signonsite")
    tgt = _profile(db, cid)
    vp.merge_profiles(db, cid, src, tgt, None)     # would raise on the external_ident index
    row = db.execute(
        "SELECT user_id, linked_on, employer_name, employer_source, external_ref, "
        "       external_source FROM speaker_voiceprints WHERE id = %s", (tgt,)).fetchone()
    assert row == (uid, "folder_name", "ABC Ltd", "typed", "sos-1", "signonsite")


def test_target_keeps_what_it_already_has(db):
    cid = _company(db)
    u1, u2 = _user(db, cid, "Ann", "A"), _user(db, cid, "Bob", "B")
    src = _profile(db, cid, user_id=u1, employer_name="Src Ltd", employer_source="typed")
    tgt = _profile(db, cid, user_id=u2, employer_name="Tgt Ltd", employer_source="typed")
    vp.merge_profiles(db, cid, src, tgt, None)
    row = db.execute("SELECT user_id, employer_name FROM speaker_voiceprints WHERE id = %s",
                     (tgt,)).fetchone()
    assert row == (u2, "Tgt Ltd")


def test_source_is_marked_merged_and_withdrawn(db):
    cid = _company(db)
    caller = _user(db, cid, "Cal", "Ler")
    src, tgt = _profile(db, cid), _profile(db, cid)
    vp.merge_profiles(db, cid, src, tgt, merged_by=caller)
    row = db.execute("SELECT status, merged_into, merged_at, merged_by "
                     "FROM speaker_voiceprints WHERE id = %s", (src,)).fetchone()
    assert row[0] == "withdrawn" and row[1] == tgt and row[2] is not None and row[3] == caller
    assert db.execute("SELECT status FROM speaker_voiceprints WHERE id = %s",
                      (tgt,)).fetchone()[0] == "confirmed"


def test_merge_is_company_scoped(db):
    c1, c2 = _company(db), _company(db)
    src, tgt = _profile(db, c1), _profile(db, c2)
    with pytest.raises(vp.MergeRefused):
        vp.merge_profiles(db, c1, src, tgt, None)
    with pytest.raises(vp.MergeRefused):
        vp.merge_profiles(db, c1, src, src, None)


def test_merging_a_withdrawn_profile_is_refused(db):
    cid = _company(db)
    src, tgt = _profile(db, cid, status="withdrawn"), _profile(db, cid)
    with pytest.raises(vp.MergeRefused):
        vp.merge_profiles(db, cid, src, tgt, None)


def test_merge_check_verdicts_on_real_vectors(db):
    cid = _company(db)
    a, b, c, empty = (_profile(db, cid) for _ in range(4))
    _sample(db, cid, a, "users/A/1.wav", 0, 10, _vec(0))
    _sample(db, cid, b, "users/B/1.wav", 0, 10, _vec(0, 2.0))       # same direction
    _sample(db, cid, c, "users/C/1.wav", 0, 10, _vec(5))            # orthogonal
    _sample(db, cid, c, "users/C/2.wav", 0, 10, _vec(0), quarantined=True)  # ignored
    assert vp.merge_check(db, cid, a, b)["verdict"] == "alike"
    assert vp.merge_check(db, cid, a, c)["verdict"] == "different"
    assert vp.merge_check(db, cid, a, empty)["verdict"] == "unsure"


def test_rename_updates_the_profile_and_its_turns_by_id_only(db):
    cid = _company(db)
    pid, other = _profile(db, cid, "Ben Lin"), _profile(db, cid, "Ben Lin")
    ins = ("INSERT INTO speaker_turn_names (company_id, voiceprint_id, session_base, "
           " turn_ref, state, display_name) VALUES (%s, %s, %s, %s, 'confirmed', 'Ben Lin')")
    db.execute(ins, (cid, pid, SID, "a@1.0"))
    db.execute(ins, (cid, other, SID, "a@2.0"))
    assert vp.rename_profile(db, cid, pid, "Ben Lin (Cassidy)") is True
    names = dict(db.execute("SELECT turn_ref, display_name FROM speaker_turn_names "
                            "WHERE company_id = %s", (cid,)).fetchall())
    assert names == {"a@1.0": "Ben Lin (Cassidy)", "a@2.0": "Ben Lin"}
    assert vp.rename_profile(db, _company(db), pid, "X") is False   # other company


def test_list_profiles_returns_identity_fields_and_merged_into(db):
    cid = _company(db)
    owner = _user(db, cid, "Ann", "Asserter")
    acct = _user(db, cid, "Ben", "Lin", folder="Ben_Lin_test2")
    linked = _profile(db, cid, user_id=acct, asserted_by=owner,
                      employer_name="ABC Ltd", employer_source="typed")
    plain = _profile(db, cid, "Ben Lin")
    _sample(db, cid, linked, "users/Ben_UCPK2/a.wav", 0, 10)
    _sample(db, cid, linked, "users/Ben_UCPK2/a.wav", 20, 30)
    _sample(db, cid, linked, "users/Ben_Lin_test2/b.wav", 0, 10)
    _sample(db, cid, linked, "users/Quarantined_Dev/c.wav", 0, 10, quarantined=True)
    vp.merge_profiles(db, cid, plain, linked, None)

    rows = {r["id"]: r for r in vp.list_profiles(db, cid)}
    r = rows[linked]
    assert (r["linked_name"], r["linked_email"]) == ("Ben Lin", "ben.lin@example.com")
    assert r["heard_on"] == ["Ben_UCPK2", "Ben_Lin_test2"]        # most samples first,
    assert "Quarantined_Dev" not in r["heard_on"]                 # live samples only
    assert r["first_named_by"] == "Ann Asserter" and r["first_named_at"] is not None
    assert r["employer_name"] == "ABC Ltd" and r["merged_into"] is None
    m = rows[plain]
    assert m["merged_into"] == linked and m["merged_into_name"] == "Ben Lin"
    assert m["status"] == "withdrawn" and m["linked_name"] is None and m["heard_on"] == []
