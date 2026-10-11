"""lake_owner.Resolver: every rule, with fake lookups (the SQL itself is proven against a real
Postgres in tests/integration/test_lake_owner_sql.py and test_lake_manifest_drain.py).

The resolver is the riskiest logic of the lake-ownership plan: a wrong owner means one
company's data is exported to, or listed for, another. So the tests that matter most pin NULL.
"""
import uuid

import pytest

import lake_owner

CO_A, CO_B, CO_C = (str(uuid.uuid4()) for _ in range(3))
SITE_A, SITE_A2, SITE_B = (str(uuid.uuid4()) for _ in range(3))
USER_ADA, USER_EVE = str(uuid.uuid4()), str(uuid.uuid4())
SID = "a" * 32
SID2 = "b" * 32
DAY = "2026-10-01"


class Fake:
    """Dict-backed lookups; counts calls so the cache can be pinned."""

    def __init__(self, recs=None, sessions=None, groups=None, homes=None, fdays=None,
                 sites=None, slugs=None, externals=()):
        self.d = dict(rec=recs or {}, sid=sessions or {}, grp=groups or {}, home=homes or {},
                      fday=fdays or {}, site=sites or {}, slug=slugs or {})
        self.ext = set(externals)
        self.calls = []

    def _get(self, name, wanted):
        self.calls.append((name, sorted(wanted)))
        return {w: self.d[name][w] for w in wanted if w in self.d[name]}

    def recordings(self, keys): return self._get("rec", keys)
    def sessions(self, sids): return self._get("sid", sids)
    def groups(self, gids): return self._get("grp", gids)
    def homes(self, folders): return self._get("home", folders)
    def folder_days(self, pairs): return self._get("fday", pairs)
    def site_companies(self, ids): return self._get("site", ids)
    def slugs(self, slugs): return self._get("slug", slugs)
    def externals(self, ids): return {i for i in ids if i in self.ext}


def world(**kw):
    kw.setdefault("homes", {"Ada_L": {"id": USER_ADA, "company_id": CO_A},
                            "Eve_X": {"id": USER_EVE, "company_id": CO_B}})
    kw.setdefault("sites", {SITE_A: CO_A, SITE_A2: CO_A, SITE_B: CO_B})
    return Fake(**kw)


def resolve(fake, key, hint=None):
    return lake_owner.Resolver(None, lookups=fake).resolve(key, hint)


def day(sites=None, siteless=False):
    return {"sites": dict(sites or {}), "siteless": siteless}


T_KEY = f"transcripts/Ada_L/{DAY}/x.json"      # carries a folder and a day, no session
R_KEY = f"reports/{DAY}/Ada_L/daily_report.json"


# ---- rule 1: a recordings row ------------------------------------------------------------
def test_recording_row_beats_folder_and_hint():
    key = f"users/Ada_L/video/{DAY}/a.mp4"
    f = world(recs={key: {"company_id": CO_B, "site_id": SITE_B, "user_id": USER_EVE}},
              fdays={("Ada_L", DAY): day({SITE_A: CO_A})})
    o = resolve(f, key, {"company_id": CO_C})
    assert (o["company_id"], o["site_id"], o["user_id"], o["reason"]) == (
        CO_B, SITE_B, USER_EVE, "recording")


def test_unknown_shape_can_still_be_owned_by_a_recording_row():
    key = "users/Ada_L/odd/thing.bin"
    f = world(recs={key: {"company_id": CO_A, "site_id": None, "user_id": USER_ADA}})
    assert resolve(f, key)["company_id"] == CO_A


# ---- rule 2: the key names a site or a company ---------------------------------------------
def test_key_embedded_site_beats_a_conflicting_hint():
    key = f"match_requests/{SITE_B}/{DAY}/r.json"
    o = resolve(world(), key, {"company_id": CO_A, "site_id": SITE_A})
    assert (o["company_id"], o["site_id"], o["reason"]) == (CO_B, SITE_B, "key")


def test_site_uuid_of_an_unknown_site_is_not_guessed():
    o = resolve(world(), f"match_requests/{uuid.uuid4()}/{DAY}/r.json")
    assert o["company_id"] is None and o["reason"] == "unresolved"


def test_voiceprint_company_in_the_key():
    o = resolve(world(), f"voiceprint_requests/{CO_C}/base1/req.json")
    assert (o["company_id"], o["site_id"], o["reason"]) == (CO_C, None, "key")


def test_voice_key_takes_the_sites_company_not_the_callers_in_the_key():
    o = resolve(world(), f"voice/{CO_A}/{SITE_B}/clip.wav")
    assert (o["company_id"], o["site_id"]) == (CO_B, SITE_B)


def test_site_report_slug_resolves_only_when_exactly_one_live_site_has_it():
    key = f"reports/{DAY}/sites/tower/site_report.json"
    one = world(slugs={"tower": [{"id": SITE_B, "company_id": CO_B}]})
    o = resolve(one, key)
    assert (o["company_id"], o["site_id"], o["reason"]) == (CO_B, SITE_B, "key")
    two = world(slugs={"tower": [{"id": SITE_A, "company_id": CO_A},
                                 {"id": SITE_B, "company_id": CO_B}]})
    o = resolve(two, key)
    assert o["company_id"] is None and o["site_id"] is None and o["reason"] == "unresolved"
    assert resolve(world(), key)["company_id"] is None  # no such slug


def test_ambiguous_slug_is_not_rescued_by_a_company_hint_that_disagrees_with_the_db():
    """R5a: with no folder and no session a company hint may fill a NULL, but a hint
    never overrides a DB answer -- here the DB answered (one live site)."""
    key = f"reports/{DAY}/sites/tower/site_report.json"
    one = world(slugs={"tower": [{"id": SITE_B, "company_id": CO_B}]})
    assert resolve(one, key, {"company_id": CO_A})["company_id"] == CO_B


# ---- rule 3: a session id ----------------------------------------------------------------
SESS = {SID: {"company_id": CO_B, "site_id": SITE_B, "user_id": USER_EVE}}


def test_sid_resolves_through_meeting_session_before_the_folder():
    key = f"session_report_results/Ada_L/{DAY}/{SID}/r.json"
    f = world(sessions=SESS, fdays={("Ada_L", DAY): day({SITE_A: CO_A})})
    o = resolve(f, key)
    assert (o["company_id"], o["site_id"], o["user_id"], o["reason"]) == (
        CO_B, SITE_B, USER_EVE, "session")


def test_a_session_keyed_object_still_resolves_on_a_split_day():
    """C2: only the folder-level documents of a split day belong to nobody."""
    key = f"session_report_results/Ada_L/{DAY}/{SID}/r.json"
    f = world(sessions=SESS,
              fdays={("Ada_L", DAY): day({SITE_A: CO_A, SITE_B: CO_B})})
    assert resolve(f, key)["company_id"] == CO_B


def test_session_marker_resolves_by_sid_and_unknown_sid_falls_to_unresolved():
    key = f"extraction_pending/{SID}.json"
    assert resolve(world(sessions=SESS), key)["company_id"] == CO_B
    o = resolve(world(), key, {"folder": "Ada_L", "company_id": CO_A})
    assert o["company_id"] is None and o["folder"] == "Ada_L"  # hint folder is descriptive only


# ---- rule 3b: groups (R5d) ---------------------------------------------------------------
def _grp(*members):
    return {"g1": [{"site_id": s, "site_company_id": c} for s, c in members]}


def test_group_with_one_site_company_is_owned_by_it():
    f = world(groups=_grp((SITE_A, CO_A), (SITE_A2, CO_A)))
    o = resolve(f, "extraction_requests/group-g1.json")
    assert (o["company_id"], o["site_id"], o["reason"]) == (CO_A, None, "session")
    f = world(groups=_grp((SITE_A, CO_A), (SITE_A, CO_A)))
    assert resolve(f, "extraction_requests/group-g1.json")["site_id"] == SITE_A


@pytest.mark.parametrize("members", [
    [(SITE_A, CO_A), (SITE_B, CO_B)],   # two companies
    [(SITE_A, CO_A), (None, None)],     # one member has no site
    [],                                  # unknown group
])
def test_uncertain_group_is_null_and_never_falls_to_the_folder(members):
    key = f"extractions/Ada_L/{DAY}/grpg1.json"  # carries a folder AND a group
    f = world(groups=_grp(*members), fdays={("Ada_L", DAY): day({SITE_A: CO_A})})
    o = resolve(f, key, {"company_id": CO_A})
    assert o["company_id"] is None and o["reason"] == "unresolved"
    assert o["folder"] == "Ada_L"


# ---- rule 4: folder and day, with ruling C2 ---------------------------------------------------
def test_one_site_that_day_is_the_site_and_its_company():
    f = world(fdays={("Ada_L", DAY): day({SITE_B: CO_B})})
    o = resolve(f, T_KEY)
    assert (o["company_id"], o["site_id"], o["user_id"], o["reason"]) == (
        CO_B, SITE_B, USER_ADA, "folder_day_site")


def test_several_sites_of_one_company_give_the_company_and_no_site():
    f = world(fdays={("Ada_L", DAY): day({SITE_A: CO_A, SITE_A2: CO_A})})
    o = resolve(f, T_KEY)
    assert (o["company_id"], o["site_id"]) == (CO_A, None)


def test_c2_split_day_sites_of_two_companies_belong_to_nobody():
    f = world(fdays={("Ada_L", DAY): day({SITE_A: CO_A, SITE_B: CO_B})})
    for key in (T_KEY, R_KEY, f"audio_segments/Ada_L/{DAY}/seg.json"):
        o = resolve(f, key, {"company_id": CO_A})
        assert o["company_id"] is None and o["site_id"] is None, key
        assert o["reason"] == "split_day" and o["user_id"] == USER_ADA, key


def test_c2_a_siteless_recording_counts_as_the_home_company():
    """Ada (home A) recorded one clip with no site and one on company B's site."""
    f = world(fdays={("Ada_L", DAY): day({SITE_B: CO_B}, siteless=True)})
    o = resolve(f, R_KEY)
    assert o["company_id"] is None and o["reason"] == "split_day"


def test_siteless_plus_a_site_of_the_home_company_is_the_home_company_without_a_site():
    f = world(fdays={("Ada_L", DAY): day({SITE_A: CO_A}, siteless=True)})
    o = resolve(f, R_KEY)
    assert (o["company_id"], o["site_id"]) == (CO_A, None)


def test_only_siteless_recordings_means_the_home_company():
    o = resolve(world(fdays={("Ada_L", DAY): day(siteless=True)}), R_KEY)
    assert (o["company_id"], o["site_id"], o["reason"]) == (CO_A, None, "folder_home")


def test_no_recordings_that_day_falls_back_to_the_home_company():
    o = resolve(world(), T_KEY)
    assert (o["company_id"], o["site_id"], o["reason"]) == (CO_A, None, "folder_home")


def test_a_key_with_no_day_uses_the_home_company():
    o = resolve(world(), "report_requests/Ada_L/req1.json")
    assert (o["company_id"], o["reason"]) == (CO_A, "folder_home")


def test_unknown_folder_is_unresolved_never_a_default_company():
    o = resolve(world(), f"transcripts/Nobody_Z/{DAY}/x.json", {"company_id": CO_A})
    assert o["company_id"] is None and o["reason"] == "unresolved"


def test_user_without_a_company_is_unresolved():
    f = world(homes={"Ada_L": {"id": USER_ADA, "company_id": None}})
    assert resolve(f, T_KEY)["company_id"] is None


def test_a_site_with_an_unknown_company_is_uncertain_not_the_home_company():
    f = world(fdays={("Ada_L", DAY): day({SITE_B: None})})
    o = resolve(f, T_KEY)
    assert o["company_id"] is None and o["reason"] == "unresolved"


# ---- rule 5: the hint (R5a) --------------------------------------------------------------
SR_KEY = f"reports/{DAY}/sites/tower/site_report.json"


def test_company_hint_fills_a_null_only_when_the_key_has_no_folder_or_session():
    o = resolve(world(), SR_KEY, {"company_id": CO_C})
    assert (o["company_id"], o["reason"]) == (CO_C, "hint")


def test_company_hint_is_ignored_when_the_key_carries_a_folder():
    o = resolve(world(), f"transcripts/Nobody_Z/{DAY}/x.json", {"company_id": CO_C})
    assert o["company_id"] is None


def test_company_hint_is_ignored_when_the_key_carries_a_session():
    o = resolve(world(), f"session_finalize_results/{SID}.json", {"company_id": CO_C})
    assert o["company_id"] is None


def test_company_hint_cannot_fill_a_split_day():
    f = world(fdays={("Ada_L", DAY): day({SITE_A: CO_A, SITE_B: CO_B})})
    assert resolve(f, R_KEY, {"company_id": CO_A})["company_id"] is None


def test_company_hint_never_overrides_a_resolved_owner():
    f = world(fdays={("Ada_L", DAY): day({SITE_A: CO_A})})
    assert resolve(f, T_KEY, {"company_id": CO_B})["company_id"] == CO_A


def test_site_hint_gives_the_sites_db_company_not_the_hints_company():
    o = resolve(world(), SR_KEY, {"site_id": SITE_B, "company_id": CO_A})
    assert (o["company_id"], o["site_id"], o["reason"]) == (CO_B, SITE_B, "hint")


def test_unknown_site_hint_falls_back_to_the_company_hint():
    o = resolve(world(), SR_KEY, {"site_id": str(uuid.uuid4()), "company_id": CO_C})
    assert (o["company_id"], o["site_id"]) == (CO_C, None)


def test_site_hint_fills_a_null_site_only_inside_the_resolved_company():
    f = world(fdays={("Ada_L", DAY): day(siteless=True)})
    assert resolve(f, T_KEY, {"site_id": SITE_A})["site_id"] == SITE_A       # same company A
    assert resolve(f, T_KEY, {"site_id": SITE_B})["site_id"] is None         # company B: no


def test_descriptive_hint_fields_fill_only_what_the_key_lacks():
    key = f"extraction_pending/{SID}.json"
    o = resolve(world(sessions=SESS), key, {"folder": "Ada_L", "session_base": "sidX"})
    assert (o["folder"], o["session_base"]) == ("Ada_L", SID)  # key's own base wins


def test_malformed_hint_values_are_ignored_not_fatal():
    for hint in ({"company_id": "not-a-uuid"}, {"site_id": 5}, "garbage", None,
                 {"company_id": True}):
        o = resolve(world(), SR_KEY, hint)
        assert o["company_id"] is None and o["reason"] == "unresolved"


# ---- scope and unknown shapes (R5b, R5f) ---------------------------------------------------
@pytest.mark.parametrize("key,scope", [
    ("lake_manifest/2026/10/12/abc.jsonl", "global"),
    ("config/directory.json", "global"),
    (f"reports/{DAY}/summary_report.json", "lake_wide"),
    (f"reports/{DAY}/daily_report.json", "lake_wide"),
    ("meeting_minutes/2026-10-01/standup.docx", "lake_wide"),
])
def test_global_and_lake_wide_keys_never_get_a_company(key, scope):
    o = resolve(world(), key, {"company_id": CO_A, "folder": "Ada_L"})
    assert o["scope"] == scope and o["company_id"] is None and o["reason"] == scope


def test_unknown_shape_is_null_with_its_first_segment_as_kind_and_ignores_the_hint():
    o = resolve(world(), "mystery/Ada_L/2026-10-01/x", {"company_id": CO_A})
    assert (o["scope"], o["kind"], o["company_id"], o["reason"]) == (
        "unknown", "mystery", None, "unresolved")


# ---- batching and cache --------------------------------------------------------------------
def test_resolve_many_batches_and_caches_each_lookup():
    f = world(fdays={("Ada_L", DAY): day({SITE_A: CO_A})})
    r = lake_owner.Resolver(None, lookups=f)
    out = r.resolve_many([(T_KEY, None), (R_KEY, None), (f"transcripts/Ada_L/{DAY}/y.json", None)])
    assert all(o["company_id"] == CO_A for o in out.values())
    assert [c for c in f.calls if c[0] == "fday"] == [("fday", [("Ada_L", DAY)])]
    assert [c for c in f.calls if c[0] == "home"] == [("home", ["Ada_L"])]
    n = len(f.calls)
    r.resolve(T_KEY)
    assert [c for c in f.calls[n:] if c[0] in ("home", "fday", "sid", "site")] == []


# ---- R5g: the home-company fallback is certain only for a never-external user ------------------
def test_r5g_external_members_dayless_request_is_null_not_home():
    f = world(externals={USER_ADA})
    o = resolve(f, "report_requests/Ada_L/req1.json", {"company_id": CO_A})
    assert o["company_id"] is None and o["reason"] == "external_member_uncertain"
    assert o["user_id"] == USER_ADA


def test_r5g_never_external_users_request_resolves_to_home():
    f = world(externals={USER_EVE})  # somebody else is external
    o = resolve(f, "report_requests/Ada_L/req1.json")
    assert (o["company_id"], o["reason"]) == (CO_A, "folder_home")


def test_r5g_external_member_with_no_recordings_that_day_is_null():
    o = resolve(world(externals={USER_ADA}), T_KEY)
    assert o["company_id"] is None and o["reason"] == "external_member_uncertain"


def test_r5g_does_not_touch_days_that_have_recordings():
    f = world(externals={USER_ADA}, fdays={("Ada_L", DAY): day({SITE_A: CO_A})})
    assert resolve(f, T_KEY)["company_id"] == CO_A


# ---- review fixes F1 / F2 ---------------------------------------------------------------------
def test_f1_voice_key_with_a_missing_site_ignores_a_company_hint():
    key = f"voice/{CO_A}/{uuid.uuid4()}/clip.wav"
    o = resolve(world(), key, {"company_id": CO_A})
    assert o["company_id"] is None and o["reason"] == "unresolved"


AMBIG = {"tower": [{"id": SITE_A, "company_id": CO_A}, {"id": SITE_B, "company_id": CO_B}]}


def test_f2_ambiguous_slug_accepts_a_hint_company_that_is_a_candidate_and_sets_its_site():
    o = resolve(world(slugs=AMBIG), SR_KEY, {"company_id": CO_B})
    assert (o["company_id"], o["site_id"], o["reason"]) == (CO_B, SITE_B, "hint")


def test_f2_ambiguous_slug_rejects_a_hint_company_that_is_not_a_candidate():
    o = resolve(world(slugs=AMBIG), SR_KEY, {"company_id": CO_C})
    assert o["company_id"] is None and o["site_id"] is None


def test_f2_ambiguous_slug_rejects_a_hint_site_that_is_not_a_candidate():
    o = resolve(world(slugs=AMBIG), SR_KEY, {"site_id": SITE_A2})
    assert o["company_id"] is None
