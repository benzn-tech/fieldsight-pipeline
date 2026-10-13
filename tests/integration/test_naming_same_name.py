"""Integration: the same-name lookup, `find_existing_profile` parity and `force_new`, in real SQL.

Skipped unless TEST_DATABASE_URL is set; rolled back per test (the `db` fixture). A connection
double records statements and parses none of them, so it cannot show that the whitespace /
case match really matches, that `last_heard` reads the right table, or that the extracted
lookup picks the same row `upsert_profile` reuses.
Spec: docs/superpowers/specs/2026-10-01-naming-asks-which-same-name-person.md.
"""
import datetime

import pytest

from repositories import voiceprints as vp

pytestmark = pytest.mark.integration

SID = "sid" + "c" * 32
UTC = datetime.timezone.utc


def _vec():
    return "[" + ",".join(["0.1"] * 192) + "]"


def _company(db, name="Same Name Co"):
    return db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                      (name,)).fetchone()[0]


def _user(db, cid, first, last, folder=None):
    email = f"{first}.{last}.{folder or ''}@example.com".lower()
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


def _sample(db, cid, pid, key):
    db.execute("INSERT INTO speaker_voiceprint_samples (company_id, voiceprint_id, embedding, "
               " source, s3_key, window_start_s, window_end_s) "
               "VALUES (%s, %s, %s::vector, 'enrolment', %s, 1, 11)",
               (cid, pid, _vec(), key))


def _turn(db, cid, pid, ref, created=None):
    return db.execute(
        "INSERT INTO speaker_turn_names (company_id, voiceprint_id, session_base, turn_ref, "
        " state, display_name, created_at) "
        "VALUES (%s, %s, %s, %s, 'confirmed', 'x', COALESCE(%s, now()))",
        (cid, pid, SID, ref, created))


def _ids(rows):
    return [r["id"] for r in rows]


# ---- same_name_profiles ----------------------------------------------------------------

def test_match_ignores_case_and_whitespace_but_not_other_names(db):
    cid = _company(db)
    a = _profile(db, cid, "Ben Lin")
    b = _profile(db, cid, "  ben   LIN ")
    _profile(db, cid, "Ben Lind")
    _profile(db, cid, "Benlin")
    assert _ids(vp.same_name_profiles(db, cid, "BEN lin")) == [a, b]
    assert _ids(vp.same_name_profiles(db, cid, " Ben\tLin\n")) == [a, b]


def test_withdrawn_and_other_companies_are_excluded(db):
    cid, other = _company(db), _company(db, "Other Co")
    live = _profile(db, cid, "Ben Lin")
    _profile(db, cid, "Ben Lin", status="withdrawn")
    _profile(db, other, "Ben Lin")
    assert _ids(vp.same_name_profiles(db, cid, "Ben Lin")) == [live]


def test_a_blank_name_matches_nothing(db):
    cid = _company(db)
    _profile(db, cid, "Ben Lin")
    assert vp.same_name_profiles(db, cid, "   ") == []


def test_last_heard_is_the_newest_turn_name_company_scoped(db):
    cid, other = _company(db), _company(db, "Other Co")
    heard, silent = _profile(db, cid), _profile(db, cid)
    old = datetime.datetime(2026, 9, 1, tzinfo=UTC)
    new = datetime.datetime(2026, 9, 30, tzinfo=UTC)
    _turn(db, cid, heard, "a@1.0", old)
    _turn(db, cid, heard, "a@2.0", new)
    rows = {r["id"]: r for r in vp.same_name_profiles(db, cid, "Ben Lin")}
    assert rows[heard]["last_heard"] == new
    assert rows[silent]["last_heard"] is None
    # A turn filed under another company for the same voiceprint id is not counted.
    _turn(db, other, silent, "z@1.0", new)
    rows = {r["id"]: r for r in vp.same_name_profiles(db, cid, "Ben Lin")}
    assert rows[silent]["last_heard"] is None


def test_identity_fields_match_the_listing(db):
    cid = _company(db)
    uid = _user(db, cid, "Ben", "Lin", "Ben_Lin")
    pid = _profile(db, cid, "Ben Lin", user_id=uid, asserted_by=uid, employer_name="ABC Ltd",
                    employer_source="typed")
    _sample(db, cid, pid, "users/Ben_UCPK/a.wav")
    row = vp.same_name_profiles(db, cid, "Ben Lin")[0]
    listed = next(r for r in vp.list_profiles(db, cid) if r["id"] == pid)
    for k in ("linked_name", "linked_email", "heard_on", "first_named_by",
              "first_named_at", "employer_name"):
        assert row[k] == listed[k], k
    assert row["heard_on"] == ["Ben_UCPK"] and row["linked_name"] == "Ben Lin"


# ---- find_existing_profile parity with upsert_profile ----------------------------------

def _upsert(db, cid, name, **kw):
    return vp.upsert_profile(db, cid, display_name=name, consent_given=False,
                             consent_basis="attestation", **kw)["id"]


def _count(db, cid):
    return db.execute("SELECT count(*) FROM speaker_voiceprints WHERE company_id = %s",
                      (cid,)).fetchone()[0]


def test_lookup_agrees_with_upsert_on_each_key(db):
    cid = _company(db)
    asserter = _user(db, cid, "Ada", "Lovelace", "Ada")
    person = _user(db, cid, "Ben", "Lin", "Ben_Lin")
    linked = _profile(db, cid, "Ben Lin", user_id=person)
    voucher = _profile(db, cid, "Sam Yu", asserted_by=asserter)
    ext = _profile(db, cid, "Zed", external_source="sos", external_ref="42")

    cases = [
        dict(name="Ben Lin", user_id=str(person), anchor=str(asserter), want=linked),
        dict(name="Sam Yu", user_id=None, anchor=str(asserter), want=voucher),
        dict(name="Sam Yu", user_id=None, anchor=str(person), want=None),   # other voucher
        dict(name="Zed", user_id=None, anchor=str(asserter), ext=("sos", "42"), want=ext),
    ]
    for c in cases:
        ext_src, ext_ref = c.get("ext", (None, None))
        found = vp.find_existing_profile(db, cid, c["name"], c["user_id"], c["anchor"],
                                         ext_ref, ext_src)
        assert (found["id"] if found else None) == c["want"], c
        before = _count(db, cid)
        got = _upsert(db, cid, c["name"], user_id=c["user_id"], asserted_by=c["anchor"],
                      external_ref=ext_ref, external_source=ext_src)
        if c["want"] is None:
            assert got not in (linked, voucher, ext) and _count(db, cid) == before + 1
        else:
            assert got == c["want"] and _count(db, cid) == before


def test_lookup_adopts_only_an_empty_unlinked_same_name_profile_for_a_user(db):
    cid = _company(db)
    asserter = _user(db, cid, "Ada", "Lovelace", "Ada")
    person = _user(db, cid, "Sam", "Yu", "Sam_Yu")
    empty = _profile(db, cid, "Sam Yu")
    found = vp.find_existing_profile(db, cid, "Sam Yu", str(person), str(asserter))
    assert found["id"] == empty
    _sample(db, cid, empty, "users/X/a.wav")
    assert vp.find_existing_profile(db, cid, "Sam Yu", str(person), str(asserter)) is None


def test_lookup_accepts_a_cursor_as_well_as_a_connection(db):
    from psycopg.rows import dict_row
    cid = _company(db)
    asserter = _user(db, cid, "Ada", "Lovelace", "Ada")
    pid = _profile(db, cid, "Sam Yu", asserted_by=asserter)
    cur = db.cursor(row_factory=dict_row)
    assert vp.find_existing_profile(cur, cid, "Sam Yu", None, str(asserter))["id"] == pid


# ---- force_new -------------------------------------------------------------------------

def test_force_new_inserts_even_when_the_lookup_would_have_matched(db):
    cid = _company(db)
    asserter = _user(db, cid, "Ada", "Lovelace", "Ada")
    existing = _profile(db, cid, "Sam Yu", asserted_by=asserter)
    assert _upsert(db, cid, "Sam Yu", asserted_by=str(asserter)) == existing
    created = _upsert(db, cid, "Sam Yu", asserted_by=str(asserter), force_new=True)
    assert created != existing
    assert _ids(vp.same_name_profiles(db, cid, "Sam Yu")) == [existing, created]


# ---- the helpers the correction handler uses -------------------------------------------

def test_get_consented_live_profile(db):
    cid, other = _company(db), _company(db, "Other Co")
    when = datetime.datetime(2026, 9, 1, tzinfo=UTC)
    ok = _profile(db, cid, consent_at=when)
    unconsented = _profile(db, cid)
    gone = _profile(db, cid, status="withdrawn", consent_at=when)
    assert vp.get_consented_live_profile(db, cid, ok)["id"] == ok
    assert vp.get_consented_live_profile(db, cid, unconsented) is None
    assert vp.get_consented_live_profile(db, cid, gone) is None
    assert vp.get_consented_live_profile(db, other, ok) is None


def test_user_has_live_profile(db):
    cid = _company(db)
    uid = _user(db, cid, "Ben", "Lin", "Ben_Lin")
    assert vp.user_has_live_profile(db, cid, str(uid)) is False
    pid = _profile(db, cid, user_id=uid)
    assert vp.user_has_live_profile(db, cid, str(uid)) is True
    db.execute("UPDATE speaker_voiceprints SET status = 'withdrawn' WHERE id = %s", (pid,))
    assert vp.user_has_live_profile(db, cid, str(uid)) is False


def test_set_employer_stamps_who_and_when(db):
    cid = _company(db)
    uid = _user(db, cid, "Ada", "Lovelace", "Ada")
    pid = _profile(db, cid)
    vp.set_employer(db, cid, pid, "ABC Ltd", "typed", str(uid))
    row = db.execute("SELECT employer_name, employer_source, employer_set_by, "
                     "employer_set_at IS NOT NULL FROM speaker_voiceprints WHERE id = %s",
                     (pid,)).fetchone()
    assert row == ("ABC Ltd", "typed", uid, True)


# ---- normalised names (spec 2026-10-01-name-normalise-and-did-you-mean) -----------------

def test_upsert_reuses_a_case_and_spacing_variant_in_the_name_and_voucher_branch(db):
    """(name, whoever vouched) branch: "ben  lin" is "Ben Lin", never a second person."""
    cid = _company(db)
    asserter = _user(db, cid, "Ada", "Lovelace", "Ada")
    existing = _profile(db, cid, "Ben Lin", asserted_by=asserter)
    before = _count(db, cid)
    got = _upsert(db, cid, "  ben   LIN ", asserted_by=str(asserter))
    assert got == existing and _count(db, cid) == before
    # A different voucher still does not adopt it (the anchor part of the rule is intact).
    other = _user(db, cid, "Bo", "Peep", "Bo")
    assert _upsert(db, cid, "ben lin", asserted_by=str(other)) != existing


def test_upsert_reuses_a_variant_in_the_empty_unlinked_branch(db):
    """Resolved-user branch: an EMPTY unlinked profile with a variant spelling is adopted."""
    cid = _company(db)
    asserter = _user(db, cid, "Ada", "Lovelace", "Ada")
    person = _user(db, cid, "Ben", "Lin", "Ben_Lin")
    empty = _profile(db, cid, "Ben Lin")
    before = _count(db, cid)
    got = _upsert(db, cid, "ben  lin", user_id=str(person), asserted_by=str(asserter),
                  linked_by=str(asserter), linked_on="full_name")
    assert got == empty and _count(db, cid) == before
    # ...and the one with samples is still not adopted, whatever the spelling.
    _sample(db, cid, empty, "users/X/a.wav")
    assert _upsert(db, cid, "BEN LIN", user_id=str(_user(db, cid, "Ben", "Lin", "B2")),
                   asserted_by=str(asserter)) != empty


def test_similar_name_profiles_returns_near_but_not_equal_live_company_profiles(db):
    cid, other = _company(db), _company(db, "Other Co")
    near = _profile(db, cid, "Sam Wu")
    _profile(db, cid, "Sam Yu")                    # equal to the query: a same-name match
    _profile(db, cid, " sam  YU ")                 # equal after normalising
    _profile(db, cid, "Bob Lee")                   # too far
    _profile(db, cid, "Sam Wu", status="withdrawn")
    _profile(db, other, "Sam Wu")
    _profile(db, cid, "Sam Wang")                  # 3 edits from "sam yu"
    rows = vp.similar_name_profiles(db, cid, "Sam Yu")
    assert _ids(rows) == [near]
    assert {"last_heard", "linked_name", "heard_on", "first_named_by"} <= set(rows[0])
    # Exclusions: anything already listed as a same-name match is left out.
    assert vp.similar_name_profiles(db, cid, "Sam Yu", exclude_ids=[near]) == []
    assert vp.similar_name_profiles(db, cid, "   ") == []


def _call_same_name(db, monkeypatch, cid, caller_id, name):
    org = pytest.importorskip("lambda_org_api", reason="requires psycopg")
    import json
    caller = {"id": str(caller_id), "company_id": str(cid)}
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, c, user, what="media": ("Ada", None))
    monkeypatch.setattr(org, "_may_correct_speakers", lambda conn, c, f: True)
    monkeypatch.setattr(org, "_same_company_as_folder", lambda conn, c, f, w: None)
    resp = org.same_name_voiceprints(db, caller, {"queryStringParameters": {"name": name}})
    assert resp["statusCode"] == 200, resp
    return json.loads(resp["body"])


def test_same_name_endpoint_does_not_ask_for_a_case_variant_the_lookup_would_use(
        db, monkeypatch):
    cid = _company(db)
    asserter = _user(db, cid, "Ada", "Lovelace", "Ada")
    pid = _profile(db, cid, "Ben Lin", asserted_by=asserter)
    body = _call_same_name(db, monkeypatch, cid, asserter, "ben  LIN")
    assert [p["id"] for p in body["profiles"]] == [str(pid)]
    assert body["wouldUse"] == str(pid) and body["ask"] is False
    assert body["similar"] == []


def test_same_name_endpoint_suggests_a_near_spelling_without_asking(db, monkeypatch):
    cid = _company(db)
    asserter = _user(db, cid, "Ada", "Lovelace", "Ada")
    near = _profile(db, cid, "Sam Wu", asserted_by=asserter)
    body = _call_same_name(db, monkeypatch, cid, asserter, "Sam Yu")
    assert body["profiles"] == [] and body["ask"] is False and body["wouldUse"] is None
    assert [p["id"] for p in body["similar"]] == [str(near)]
    assert body["similar"][0]["displayName"] == "Sam Wu"
