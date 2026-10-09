"""Naming asks "which same-name person?" instead of silently making another.

Spec: docs/superpowers/specs/2026-10-01-naming-asks-which-same-name-person.md.

Two halves, both driven through the real handlers with only the repository calls replaced:

* `GET /voiceprints/same-name` -- routing, who may ask, and the `ask` rule.
* `POST .../speaker-corrections` -- the two new mutually exclusive body fields, and that the
  retry / proposal-accept delegations now name the profile they concern.

The SQL (the case/whitespace match, `last_heard`, lookup parity) is in
tests/integration/test_naming_same_name.py: a connection double parses none of it.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO = "11111111-1111-1111-1111-111111111111"
A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NEW = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
USER = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SESSION = "ben_2026-08-12_16-50-28_sid" + "b" * 32
SRC = "ben_2026-08-12_16-52-24_sidbbbb_c0004_off0.0_to114.0_srcwav.json"

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": CO, "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "site_manager", "created_at": "2026-09-30",
          "voiceprint_consent_basis": "attestation"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def _event(method, path, body=None, qs=None):
    return {"httpMethod": method, "path": "/api/org" + path, "queryStringParameters": qs,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


def _json(resp):
    return json.loads(resp["body"])


def _row(pid, name="Ben Lin", **kw):
    r = {"id": pid, "display_name": name, "user_id": None, "linked_name": None,
         "linked_email": None, "heard_on": [], "first_named_at": "2026-08-28",
         "first_named_by": "Ben_UCPK2", "employer_name": None, "last_heard": None}
    r.update(kw)
    return r


# ---------------------------------------------------------------- GET /voiceprints/same-name

@pytest.fixture
def same(monkeypatch):
    st = {"rows": [], "similar": [], "would": None, "person": None, "find_args": [], "may": True,
          "folder": ("Ada_L", None)}
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what="media": st["folder"])
    monkeypatch.setattr(org, "_may_correct_speakers", lambda conn, caller, folder: st["may"])
    monkeypatch.setattr(org, "_same_company_as_folder", lambda conn, c, f, w: None)
    monkeypatch.setattr(org.voiceprints, "same_name_profiles",
                        lambda conn, co, name: st["rows"])
    monkeypatch.setattr(org.voiceprints, "similar_name_profiles",
                        lambda conn, co, name, exclude_ids=(): st["similar"])
    monkeypatch.setattr(org.users, "resolve_display_name",
                        lambda conn, co, name: (st["person"], "full_name")
                        if st["person"] else (None, "not-in-directory"))

    def find(conn, co, name, user_id=None, anchor=None, *a, **k):
        st["find_args"].append((co, name, user_id, anchor))
        return {"id": st["would"]} if st["would"] else None

    monkeypatch.setattr(org.voiceprints, "find_existing_profile", find)
    return st


def _ask(qs=None):
    return org.lambda_handler(_event("GET", "/voiceprints/same-name",
                                     qs=qs if qs is not None else {"name": "Ben Lin"}), None)


def test_the_route_is_reachable_and_not_taken_by_an_id_pattern(same):
    resp = _ask()
    assert resp["statusCode"] == 200, resp
    assert _json(resp) == {"profiles": [], "similar": [], "wouldUse": None, "ask": False}


@pytest.mark.parametrize("rows,would,ask", [
    ([], None, False),                      # nobody of that name: a new one, no question
    ([A], A, False),                        # one, and the lookup uses it: certain
    ([A], None, True),                      # one the lookup would NOT use: a duplicate is coming
    ([A], B, True),                         # lookup would use some other profile
    ([A, B], A, True),                      # two: always ask, even if the lookup picks one
    ([A, B], None, True),
    ([], B, False),                         # nothing same-named; the lookup's pick is a
                                            # differently named linked profile, no ambiguity
])
def test_ask_only_when_the_answer_is_not_already_certain(same, rows, would, ask):
    same["rows"] = [_row(r) for r in rows]
    same["would"] = would
    body = _json(_ask())
    assert body["ask"] is ask
    assert body["wouldUse"] == would
    assert [p["id"] for p in body["profiles"]] == rows


def test_the_lookup_gets_the_callers_id_as_anchor_and_the_directory_user(same):
    same["person"] = {"id": USER}
    _ask({"name": "Ben Lin", "user": "Ada_L"})
    assert same["find_args"] == [(CO, "Ben Lin", USER, "u-1")]


def test_a_name_with_no_directory_match_looks_up_by_name_and_voucher(same):
    _ask()
    assert same["find_args"] == [(CO, "Ben Lin", None, "u-1")]


def test_each_profile_carries_the_identity_fields_and_last_heard(same):
    import datetime
    same["rows"] = [_row(A, user_id=USER, linked_name="Ben Lin", linked_email="b@x.nz",
                         heard_on=["Ben_UCPK"], employer_name="ABC Ltd",
                         last_heard=datetime.datetime(2026, 9, 30, 4, 0))]
    p = _json(_ask())["profiles"][0]
    assert p == {
        "id": A, "displayName": "Ben Lin",
        "linkedAccount": {"name": "Ben Lin", "email": "b@x.nz"},
        "heardOn": ["Ben_UCPK"], "firstNamed": {"at": "2026-08-28", "by": "Ben_UCPK2"},
        "employer": "ABC Ltd", "lastHeard": "2026-09-30T04:00:00"}


def test_similar_profiles_carry_the_same_fields_and_do_not_change_ask(same):
    same["similar"] = [_row(B, name="Ben Linn", employer_name="XYZ")]
    body = _json(_ask())
    assert body["ask"] is False and body["profiles"] == []
    assert body["similar"] == [{
        "id": B, "displayName": "Ben Linn", "linkedAccount": None, "heardOn": [],
        "firstNamed": {"at": "2026-08-28", "by": "Ben_UCPK2"}, "employer": "XYZ",
        "lastHeard": None}]


def test_a_caller_who_may_not_name_a_speaker_is_refused(same):
    same["may"] = False
    assert _ask()["statusCode"] == 403


def test_the_owner_of_the_folder_may_ask(same):
    """`_may_correct_speakers` is the gate speaker-corrections uses, so a worker on their own
    recording passes it (the role list is not the only way in)."""
    same["may"] = True
    assert _ask({"name": "Ben Lin", "user": "Ada_L"})["statusCode"] == 200


def test_the_folder_resolution_error_is_returned_as_is(same):
    same["folder"] = (None, org.error("forbidden", 403))
    assert _ask({"name": "Ben Lin", "user": "Other"})["statusCode"] == 403


def test_a_missing_name_is_a_400(same):
    assert _ask({"name": "  "})["statusCode"] == 400


def test_it_is_a_404_when_identity_is_off(same, monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    assert _ask()["statusCode"] == 404


# ---------------------------------------------------------------- POST speaker-corrections

@pytest.fixture
def post(monkeypatch):
    st = {"upserts": [], "employer": [], "queued": [], "consented": {A},
          "has_live": False, "person": None, "profile_name": {}, "upsert_id": NEW}

    class S3:
        def put_object(self, **kw):
            st["queued"].append(json.loads(kw["Body"]))
            return {}

    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "ENROL_ON_CORRECTION", True)
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what="media": (user or "Ben", None))
    monkeypatch.setattr(org, "_may_correct_speakers", lambda conn, caller, folder: True)
    monkeypatch.setattr(org, "_same_company_as_folder", lambda conn, c, f, w: None)
    monkeypatch.setattr(org, "_session_turns", lambda conn, f, d, s, **k: [])
    monkeypatch.setattr(org, "s3", lambda: S3())
    monkeypatch.setattr(org.users, "resolve_display_name",
                        lambda conn, co, name: (st["person"], "full_name")
                        if st["person"] else (None, "not-in-directory"))

    def upsert(conn, co, **kw):
        st["upserts"].append(kw)
        return {"id": st["upsert_id"]}

    monkeypatch.setattr(org.voiceprints, "upsert_profile", upsert)
    monkeypatch.setattr(org.voiceprints, "get_consented_live_profile",
                        lambda conn, co, vid: {"id": vid} if vid in st["consented"] else None)
    monkeypatch.setattr(org.voiceprints, "set_employer",
                        lambda conn, co, vid, name, source, by:
                        st["employer"].append((vid, name, source, by)))
    monkeypatch.setattr(org.voiceprints, "user_has_live_profile",
                        lambda conn, co, uid: st["has_live"])
    monkeypatch.setattr(org.voiceprints, "get_profile",
                        lambda conn, co, vid: {"id": vid, "employer_name": None,
                                               "employer_source": None,
                                               "display_name": st["profile_name"].get(vid)})
    return st


def _correct(**extra):
    body = {"user": "Ben", "display_name": "Ben Lin", "source_filename": SRC,
            "start_sec": 3.16, "end_sec": 112.66}
    body.update(extra)
    return org.speaker_corrections(FakeConn(), dict(CALLER), SESSION,
                                   {"body": json.dumps(body)})


def test_without_either_field_the_correction_is_unchanged(post):
    res = _correct()
    assert res["statusCode"] == 202
    assert len(post["upserts"]) == 1
    assert post["upserts"][0]["force_new"] is False


def test_both_fields_together_are_a_400_and_write_nothing(post):
    res = _correct(voiceprint_id=A, new_person=True)
    assert res["statusCode"] == 400
    assert post["upserts"] == [] and post["queued"] == []


def test_voiceprint_id_uses_that_profile_and_skips_the_lookup(post):
    post["person"] = {"id": USER}
    res = _correct(voiceprint_id=A)
    assert res["statusCode"] == 202, res
    assert post["upserts"] == []
    assert post["queued"][0]["enrol"] == {"voiceprint_id": A}
    # No account link, and the response says so rather than claiming one.
    assert _json(res)["linkedTo"] is None


@pytest.mark.parametrize("vid", [B, "not-a-uuid"])
def test_voiceprint_id_must_be_a_live_consented_profile_of_this_company(post, vid):
    res = _correct(voiceprint_id=vid)
    assert res["statusCode"] == 400
    assert post["queued"] == [] and post["upserts"] == []


def test_voiceprint_id_still_updates_the_employer_when_one_is_sent(post):
    res = _correct(voiceprint_id=A, employer_name="ABC Ltd", employer_source="typed")
    assert res["statusCode"] == 202
    assert post["employer"] == [(A, "ABC Ltd", "typed", "u-1")]


def test_voiceprint_id_without_an_employer_leaves_the_stored_one_alone(post):
    _correct(voiceprint_id=A)
    assert post["employer"] == []


def test_voiceprint_id_is_ignored_when_enrolment_is_off(post, monkeypatch):
    """No profile is touched on that path, so there is nothing to choose between; the
    propagation half is the same either way."""
    monkeypatch.setattr(org, "ENROL_ON_CORRECTION", False)
    res = _correct(voiceprint_id=B)          # not even a valid profile: still not an error
    assert res["statusCode"] == 202
    assert post["queued"][0]["enrol"] is None and post["upserts"] == []


def test_new_person_forces_an_insert(post):
    res = _correct(new_person=True, display_name="Ben Lin (Auckland)")
    assert res["statusCode"] == 202
    assert post["upserts"][0]["force_new"] is True
    assert post["queued"][0]["enrol"] == {"voiceprint_id": NEW}


def test_new_person_links_the_directory_user_only_if_they_have_no_live_profile(post):
    post["person"] = {"id": USER}
    _correct(new_person=True)
    assert post["upserts"][-1]["user_id"] == USER

    post["has_live"] = True
    res = _correct(new_person=True)
    kw = post["upserts"][-1]
    assert kw["user_id"] is None and kw["linked_by"] is None and kw["linked_on"] is None
    assert _json(res)["linkedTo"] is None


# ---------------------------------------------------------------- the two internal delegations

def test_the_proposal_accept_passes_the_known_profile(monkeypatch):
    forwarded = []
    monkeypatch.setattr(org.voiceprints, "get_profile",
                        lambda conn, co, vid: {"id": vid, "display_name": "Ben Lin"})
    monkeypatch.setattr(org, "_session_turns", lambda conn, f, d, sb, **k: [
        {"source_filename": SRC, "speaker_label": "spk_0", "start_sec": 9.0, "end_sec": 31.0}])
    monkeypatch.setattr(org, "speaker_corrections",
                        lambda conn, caller, sid, event: forwarded.append(
                            json.loads(event["body"])) or {"statusCode": 202, "body": "{}"})
    row = {"voiceprint_id": A, "source_filename": SRC, "speaker_label": "spk_0",
           "user_folder": "Ben_UCPK", "session_date": "2026-08-13", "session_base": SESSION}
    org._apply_confirmed_proposal(FakeConn(), dict(CALLER), CO, row, {})
    assert forwarded and forwarded[0]["voiceprint_id"] == A
    assert forwarded[0]["display_name"] == "Ben Lin"


# ---------------------------------------------------------------- one spelling per person

def _artifact_name(post):
    return post["queued"][-1]["correction"]["display_name"]


def test_a_lookup_hit_sends_the_profiles_own_name_not_the_typed_one(post):
    post["profile_name"][NEW] = "Ben Lin"      # upsert_profile returned the existing row
    res = _correct(display_name="ben   lin")
    assert res["statusCode"] == 202
    assert _artifact_name(post) == "Ben Lin"
    assert _json(res)["displayName"] == "Ben Lin"


def test_a_chosen_voiceprint_id_sends_that_profiles_name(post):
    post["profile_name"][A] = "Ben Lin"
    res = _correct(display_name="BEN LIN", voiceprint_id=A)
    assert _artifact_name(post) == "Ben Lin"
    assert _json(res)["displayName"] == "Ben Lin"


def test_new_person_keeps_the_typed_name_with_whitespace_collapsed(post):
    post["profile_name"][NEW] = "Ben Lin (Auckland)"     # what the INSERT stored
    res = _correct(display_name="  Ben   Lin  (Auckland) ", new_person=True)
    assert post["upserts"][0]["display_name"] == "Ben Lin (Auckland)"
    assert _artifact_name(post) == "Ben Lin (Auckland)"
    assert _json(res)["displayName"] == "Ben Lin (Auckland)"


def test_the_typed_name_is_the_fallback_when_enrolment_is_off(post, monkeypatch):
    monkeypatch.setattr(org, "ENROL_ON_CORRECTION", False)
    _correct(display_name="ben  lin")
    assert _artifact_name(post) == "ben lin"


# ---------------------------------------------------------------- the name helpers

from repositories import voiceprints as vp  # noqa: E402


@pytest.mark.parametrize("raw,key", [
    ("Ben Lin", "ben lin"), ("  ben   LIN ", "ben lin"), ("Ben\tLin\n", "ben lin"),
    ("", ""), (None, ""), ("BEN", "ben")])
def test_name_key_collapses_case_and_whitespace(raw, key):
    assert vp.name_key(raw) == key


def test_collapse_name_keeps_case():
    assert vp.collapse_name("  Ben   Lin ") == "Ben Lin"


@pytest.mark.parametrize("a,b,d", [
    ("abc", "abc", 0), ("abc", "abd", 1), ("abc", "ab", 1), ("", "abc", 3),
    ("kitten", "sitting", 3), ("ben lin", "benn lin", 1)])
def test_edit_distance(a, b, d):
    assert vp.edit_distance(a, b) == d
    assert vp.edit_distance(b, a) == d


def test_edit_distance_limit_stops_early_but_stays_above_the_limit():
    assert vp.edit_distance("abcdef", "uvwxyz", limit=2) > 2


@pytest.mark.parametrize("a,b,similar", [
    ("Sam Yu", "Sam Wu", True),                 # short name, 1 edit
    ("Sam Yu", "Sam Wang", False),              # short name, 3 edits
    ("Ben Lin", "Ben Lin", False),              # equal: a same-name match, not "similar"
    ("Ben Lin", "ben  LIN", False),             # equal after normalising
    ("Ben Lin", "Bob Lee", False),
    ("Ben Lin", "Benn Lin", True),              # 7 chars: threshold 2, 1 edit
    ("Ben Lin", "Ben Linn", True),
    ("Ben Lin", "Ben Lynn", True),              # 2 edits, longer than 6
    ("Sam Yu", "Sam Wuu", False),               # 6 chars -> only 1 edit allowed, this is 2
    ("Ben Lin", "", False), ("", "Ben Lin", False)])
def test_is_similar_name(a, b, similar):
    assert vp.is_similar_name(a, b) is similar


def test_the_check_sees_the_name_exactly_as_the_save_will(same, monkeypatch):
    """`speaker_corrections` collapses inner whitespace before resolving the name; the check
    must hand the resolver and the lookup the same string, or it asks about a duplicate the
    save would never make (TEST 2026-10-01: "  ben   LIN " asked while "Ben Lin" did not)."""
    seen = []
    same["find_args"].clear()
    monkeypatch.setattr(org.users, "resolve_display_name",
                        lambda conn, co, name: (seen.append(name), (None, "x"))[1])
    _ask({"name": "  ben   LIN "})
    assert seen == ["ben LIN"]
    assert same["find_args"][0][1] == "ben LIN"
