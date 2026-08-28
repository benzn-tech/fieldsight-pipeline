"""Unit: POST /api/org/sessions/{base}/speaker-corrections — the first user-facing surface
of speaker naming.

This endpoint is the only entry to a chain that is otherwise fully built and entirely inert:
it queues one S3 request artifact, the non-VPC embedder picks it up, and an in-VPC writer
persists the result. org-api cannot invoke a Lambda (in-VPC, no NAT — BUG-36), so the handoff
is an artifact, exactly like `session_report_requests/`.

Two things are load-bearing here and both fail silently if they regress:

  * **the mode gate** — `SPEAKER_IDENTITY_MODE=off` must 404, because that is the rollback.
    A rollback that only stops *some* of the feature is not a rollback.
  * ~~**the profile query**~~ — this endpoint is NOT a caller of `profiles_for_matching` and
    has not been since propagation stopped scoring against stored profiles. The claim
    survived here as a fixture stub of a function nobody called; the consent and withdrawn
    filters are covered in test_voiceprints_repo and in the writer's tests, where they run.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-08-13",
}
# A real session id shape: the sid is what identifies a session, and a fixture
# without one was not a session at all — it just never got asked for the sid.
SESSION = "Benl1_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d"
PATH = f"/api/org/sessions/{SESSION}/speaker-corrections"


class FakeConn:
    #: The company that owns the folder under test, as `_same_company_as_folder` reads it.
    #: Default None = "no users row", the branch that fails OPEN for device folders. Every
    #: test in this file predates the cross-company guard and none of them is about it, so
    #: they take that branch — which is a real branch, not a hole punched for the tests.
    #: `test_voiceprint_stays_in_its_company.py` is where the guard itself is exercised.
    folder_owner = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return self.folder_owner


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}


def _event(body, sub="sub-1"):
    return {"httpMethod": "POST", "path": PATH, "queryStringParameters": None,
            "body": json.dumps(body),
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


BODY = {"source_filename": "ben_2026-08-13_11-49-00_c0000.wav",
        "start_sec": 12.0, "end_sec": 18.0, "display_name": "Ben L"}


@pytest.fixture
def wired(monkeypatch):
    s3 = FakeS3()
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "shadow")
    monkeypatch.setattr(org, "s3", lambda: s3)
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    # Default: the typed name is not in the directory. The tests in this file are about
    # consent and queuing, not about identity resolution, and letting the real resolver
    # reach the connection double would make them fail for a reason none of them are about.
    monkeypatch.setattr(org.users, "resolve_display_name",
                        lambda conn, company_id, name: (None, "not-in-directory"))
    # Default: the site could not be resolved, i.e. no narrowing. The real resolver walks
    # three repositories and these tests are about queuing, not attribution.
    monkeypatch.setattr(org, "_site_for_session",
                        lambda conn, company_id, folder, date, session_base: None)
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ada_L", None))
    # The ownership arm of _may_correct_speakers reads this; FakeConn has no cursor, and a
    # real query here would only be re-testing repositories.scope.
    monkeypatch.setattr(org.scope, "visible_scope",
                        lambda conn, caller: {"self_folder": "Ada_L", "user_scope": "ALL"})
    # No `profiles_for_matching` stub. This fixture carried one for months after the
    # endpoint stopped calling it — a stub of a function nothing invoked, which passed for
    # coverage of the consent filter while covering nothing. The filter is exercised where it
    # actually runs, in test_voiceprints_repo and the writer's tests.
    return s3


def _body(res):
    return json.loads(res["body"])


# ---- the mode gate ------------------------------------------------------


def test_the_route_404s_when_the_feature_is_off(wired, monkeypatch):
    """The rollback. `off` is the template default, so merging this changes nothing until
    somebody sets a repo variable — and setting it back must remove the surface entirely,
    not merely stop the downstream half."""
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    res = org.lambda_handler(_event(BODY), None)
    assert res["statusCode"] == 404
    assert wired.puts == [], "a correction was queued while the feature was switched off"


def test_the_route_works_in_shadow(wired):
    res = org.lambda_handler(_event(BODY), None)
    assert res["statusCode"] == 202, _body(res)
    assert len(wired.puts) == 1


# ---- what gets queued ---------------------------------------------------


def test_the_artifact_lands_under_the_prefix_the_embedder_watches(wired):
    org.lambda_handler(_event(BODY), None)
    key = wired.puts[0]["Key"]
    assert key.startswith("voiceprint_requests/"), key
    assert key.endswith(".json"), key


# `test_the_artifact_carries_the_profiles_the_query_allowed` was here and is deleted with the
# behaviour it pinned. It asserted the artifact contained a 192-dimension embedding — which
# was the defect, not the contract: those vectors went to an S3 object with no lifecycle
# rule and outlived the withdrawal of the person they belonged to. The consent filter it was
# really about is still in one place; there is simply nothing for it to filter INTO, because
# propagation names a cluster and scores against no stored profile.


def test_the_company_comes_from_the_caller_never_from_the_body(wired):
    """A body-supplied company id would let one tenant queue work scoped to another."""
    org.lambda_handler(_event(dict(BODY, company_id="c-999")), None)
    doc = json.loads(wired.puts[0]["Body"])
    assert doc["company_id"] == "c-1"


def test_the_window_and_the_name_travel_verbatim(wired):
    org.lambda_handler(_event(BODY), None)
    doc = json.loads(wired.puts[0]["Body"])
    assert doc["correction"]["start_sec"] == 12.0
    assert doc["correction"]["end_sec"] == 18.0
    assert doc["correction"]["display_name"] == "Ben L"
    # The CANONICAL key, not the URL spelling — that is the whole point of the
    # normalisation, and asserting the URL form here would re-encode the bug.
    import turn_name_overlay as tno
    assert doc["session_base"] == tno.session_base(SESSION)


def test_the_response_says_which_of_the_two_effects_happened(wired):
    """A correction propagates within the meeting AND may enrol a profile for future
    sessions, and the second needs consent the first does not. One flat 'success' would hide
    which of them the user actually got."""
    b = _body(org.lambda_handler(_event(BODY), None))
    assert "propagation" in b and "enrolment" in b


# ---- refusals -----------------------------------------------------------


def test_a_window_that_ends_before_it_starts_is_refused(wired):
    res = org.lambda_handler(_event(dict(BODY, start_sec=9.0, end_sec=4.0)), None)
    assert res["statusCode"] == 400
    assert wired.puts == []


def test_a_missing_name_is_refused(wired):
    res = org.lambda_handler(_event({k: v for k, v in BODY.items()
                                     if k != "display_name"}), None)
    assert res["statusCode"] == 400
    assert wired.puts == []


def test_a_malformed_body_is_refused_before_anything_is_read(wired):
    ev = _event(BODY)
    ev["body"] = "{not json"
    res = org.lambda_handler(ev, None)
    assert res["statusCode"] == 400
    assert wired.puts == []


def test_a_worker_may_not_correct_someone_elses_recording(wired, monkeypatch):
    """Was `test_a_worker_may_not_correct`, on the reasoning that "naming a voice is a claim
    about a person that follows them into future sessions".

    Two things retired that reasoning. It does not follow them into future sessions —
    matching against stored profiles is backend Phase 5 and has no caller; naming propagates
    inside ONE meeting. And the rule it produced meant the person who pressed record could
    not say who was in the room, which is the ordinary case and not an edge one.

    So the restriction narrowed from "not a worker" to "not someone else's recording", which
    is what it was protecting. Product decision, 2026-08-14."""
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Someone_Else", None))
    res = org.lambda_handler(_event(BODY), None)
    assert res["statusCode"] == 403
    assert wired.puts == []


def test_a_platform_admin_may_correct(wired, monkeypatch):
    """The standing trap: span-all is automatic on graded READ paths and has to be taught to
    every write endpoint separately. Each one that forgets is a silent 403 for operations."""
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="platform_admin"))
    res = org.lambda_handler(_event(BODY), None)
    assert res["statusCode"] == 202, _body(res)


def test_the_folder_comes_from_the_authorising_resolver_not_the_body(wired, monkeypatch):
    """A body-supplied folder would let a caller queue work against a recording they cannot
    see. `_resolve_org_media_folder` is the same gate every other media route uses, and its
    answer is what travels."""
    seen = {}

    def resolver(conn, caller, user, what=None):
        seen["user"] = user
        return "Ada_L", None
    monkeypatch.setattr(org, "_resolve_org_media_folder", resolver)
    org.lambda_handler(_event(dict(BODY, user="Someone_Else",
                                   user_folder="Hacked")), None)
    doc = json.loads(wired.puts[0]["Body"])
    assert doc["user_folder"] == "Ada_L", "the artifact carried a body-supplied folder"
    assert seen["user"] == "Someone_Else", "the resolver never saw the requested user"


def test_a_session_id_without_a_date_is_refused(wired):
    """The date is half the S3 key the audio lives under. Deriving it wrongly puts the read
    on a path that cannot exist, far from the cause."""
    ev = _event(BODY)
    ev["path"] = "/api/org/sessions/no-date-here/speaker-corrections"
    res = org.lambda_handler(ev, None)
    assert res["statusCode"] == 400
    assert wired.puts == []


def test_the_artifact_names_the_folder_and_the_date_the_consumer_needs(wired):
    org.lambda_handler(_event(BODY), None)
    doc = json.loads(wired.puts[0]["Body"])
    assert doc["user_folder"] == "Ada_L"
    assert doc["date"] == "2026-08-13"


def test_absolute_clock_seconds_are_refused_rather_than_queued(wired):
    """The transcript response carries both `start` (absolute clock) and `chunk_start`
    (in-file). Only the second is what a turn_ref is keyed by. Sending the first used to
    return 202 and write a row that matched no turn — visible only as `unmatchedNames`,
    which is a silence nobody investigates. It cost me two rounds of debugging tonight."""
    batch = ("ben_2026-08-13_11-49-00_sid" + "0" * 32 +
             "_c0000_bn4_off0.0_to114.0_srcwav.wav")
    res = org.lambda_handler(_event(dict(BODY, source_filename=batch,
                                         start_sec=41400.0, end_sec=41406.0)), None)
    assert res["statusCode"] == 400
    assert "chunk_start" in _body(res)["error"]
    assert wired.puts == []


def test_a_legitimate_offset_inside_the_file_is_accepted(wired):
    """The bound is the file's own length, not a guess about what a plausible number looks
    like — 7200 is a legitimate offset into a two-hour recording AND a legitimate clock
    second, so no threshold could separate them."""
    batch = ("ben_2026-08-13_11-49-00_sid" + "0" * 32 +
             "_c0000_bn4_off0.0_to114.0_srcwav.wav")
    res = org.lambda_handler(_event(dict(BODY, source_filename=batch,
                                         start_sec=34.92, end_sec=36.9)), None)
    assert res["statusCode"] == 202, _body(res)


def test_a_filename_that_declares_no_span_is_not_second_guessed(wired):
    """A per-chunk upload carries no `_off…_to…`, so there is nothing to check against and
    inventing a bound would reject real corrections."""
    res = org.lambda_handler(_event(dict(BODY, source_filename="x_c0000.wav",
                                         start_sec=900.0, end_sec=905.0)), None)
    assert res["statusCode"] == 202, _body(res)


def test_the_artifact_carries_this_session_s_turns_and_no_others(wired, monkeypatch):
    """The embedder cannot cluster a session it cannot see, and it must not be handed
    another session's turns: a day holds several, and two sessions routinely have turns
    starting at the same offset. Mistaking one for the other cost two rounds of debugging."""
    mine = ("ben_2026-08-13_11-49-00_sid" + "0" * 32 + "_c0000_bn4_off0.0_to114.0_srcwav.json")
    other = ("ben_2026-08-13_18-10-00_sid" + "1" * 32 + "_c0000_bn4_off0.0_to114.0_srcwav.json")
    monkeypatch.setattr(org, "_read_org_transcripts", lambda d, f, a, b, conn=None: {
        "speaker_segments": [
            {"source_filename": mine, "chunk_start": 4.88, "duration": 4.0},
            {"source_filename": mine, "chunk_start": 26.5, "duration": 6.0},
            {"source_filename": other, "chunk_start": 4.88, "duration": 4.0},
        ]})
    ev = _event(BODY)
    ev["path"] = "/api/org/sessions/ben_2026-08-13_11-49-00_sid" + "0" * 32 + \
                 "/speaker-corrections"
    org.lambda_handler(ev, None)
    doc = json.loads(wired.puts[0]["Body"])
    assert [t["start_sec"] for t in doc["turns"]] == [4.88, 26.5], (
        "another session's turn was handed to the clusterer")


def test_a_session_with_no_readable_transcript_still_queues_the_correction(wired,
                                                                           monkeypatch):
    """The turn the user pointed at is a fact regardless. Losing it because the transcript
    is not ready would be worse than propagating nothing."""
    def boom(*a, **k):
        raise RuntimeError("S3 down")
    monkeypatch.setattr(org, "_read_org_transcripts", boom)
    res = org.lambda_handler(_event(BODY), None)
    assert res["statusCode"] == 202
    assert json.loads(wired.puts[0]["Body"])["turns"] == []


def test_the_stored_session_key_is_the_one_the_reader_will_query_with(wired):
    """Rows are written by this endpoint and found by the transcript overlay, and each was
    computing the session key its own way — the writer from the URL, the reader from the
    filename. Rows landed and were never seen again, twice tonight, one layer apart.

    Only one spelling may ever be persisted, and it is whatever `session_base` returns.
    """
    import turn_name_overlay as tno
    org.lambda_handler(_event(BODY), None)
    stored = json.loads(wired.puts[0]["Body"])["session_base"]
    a_filename = SESSION + "_c0000_bn4_off0.0_to114.0_srcwav.json"
    assert stored == tno.session_base(a_filename), (
        "the writer stored a key the reader cannot construct from a filename")


def test_a_session_without_a_sid_is_refused_rather_than_stored_unfindable(wired):
    ev = _event(BODY)
    ev["path"] = "/api/org/sessions/Benl1_2026-03-20_12-18-34/speaker-corrections"
    res = org.lambda_handler(ev, None)
    assert res["statusCode"] == 400
    assert wired.puts == []


# ---- enrolment: the half that makes NEXT time work ---------------------
#
# The correction already names this meeting. Enrolment is what makes the person recognisable
# in future ones, and it is the half that stores biometric data — so it is opt-in, separate,
# and reported separately. A flat "success" would tell the user they got both when they
# asked for one.


def test_without_consent_nothing_is_enrolled_and_the_response_says_so(wired):
    b = _body(org.lambda_handler(_event(BODY), None))
    assert b["enrolment"] == "not_requested"
    doc = json.loads(wired.puts[0]["Body"])
    assert not doc.get("enrol"), "an enrolment was queued without being asked for"


def test_consent_enrols_and_the_artifact_carries_the_profile(wired, monkeypatch):
    seen = {}

    def upsert(conn, company_id, **kw):
        seen.update(kw)
        return {"id": "vp-1"}
    monkeypatch.setattr(org.voiceprints, "upsert_profile", upsert)
    b = _body(org.lambda_handler(_event(dict(BODY, consent_given=True,
                                                consented_by="u-9")), None))
    assert b["enrolment"] == "requested"
    doc = json.loads(wired.puts[0]["Body"])
    assert doc["enrol"]["voiceprint_id"] == "vp-1"
    assert seen["display_name"] == "Ben L"
    assert seen["consent_given"] is True


def test_the_consenting_person_is_recorded_not_assumed_to_be_the_caller(wired,
                                                                        monkeypatch):
    """§6: consent comes from the person whose voice it is. The caller is whoever is doing
    the labelling, and those are routinely different people — the wearer corrects a
    subcontractor's name. Recording the caller as the consenter would make the record say
    something nobody claimed."""
    seen = {}
    monkeypatch.setattr(org.voiceprints, "upsert_profile",
                        lambda conn, company_id, **kw: seen.update(kw) or {"id": "vp-1"})
    org.lambda_handler(_event(dict(BODY, consent_given=True,
                                   consented_by="u-the-person")), None)
    assert seen["consented_by"] == "u-the-person"


def test_a_refused_profile_refuses_the_whole_request(wired, monkeypatch):
    """If the repository will not create the profile, queuing the work anyway would leave an
    artifact pointing at nothing and an enrolment that silently never happens."""
    def boom(conn, company_id, **kw):
        raise ValueError("a named voiceprint cannot be created without consent")
    monkeypatch.setattr(org.voiceprints, "upsert_profile", boom)
    res = org.lambda_handler(_event(dict(BODY, consent_given=True,
                                         consented_by="u-9")), None)
    assert res["statusCode"] == 400
    assert "consent" in _body(res)["error"]
    assert wired.puts == []


def test_consent_without_naming_who_gave_it_is_refused(wired):
    """0042 added `consented_by` because a timestamp cannot tell the subject agreeing apart
    from the wearer clicking a box on their behalf. Optional, it recorded nothing in the
    common case — the same silence with an extra column."""
    res = org.lambda_handler(_event(dict(BODY, consent_given=True)), None)
    assert res["statusCode"] == 400
    assert "consented_by" in _body(res)["error"]
    assert wired.puts == []


def test_the_response_does_not_promise_an_enrolment_it_cannot_guarantee(wired, monkeypatch):
    """`queued` said the thing would happen. All that had happened was that it had been
    asked for — and the embedder refuses a window it cannot judge as one voice, which on
    TEST produced `enrolment: queued` in the response and `enrolment refused: window too
    short` in the log for the same request.

    This endpoint cannot know: the audio is read downstream, outside the VPC. So it says
    what it did, not what will come of it."""
    monkeypatch.setattr(org.voiceprints, "upsert_profile",
                        lambda conn, company_id, **kw: {"id": "vp-1"})
    b = _body(org.lambda_handler(_event(dict(BODY, consent_given=True,
                                             consented_by="u-9")), None))
    assert b["enrolment"] == "requested"
    assert b["enrolmentMayBeRefused"] is True


# ---- withdrawal ---------------------------------------------------------
#
# §6 requires a voiceprint to be withdrawable, and until now `withdraw()` existed with no
# endpoint — the feature could create biometric data and offered no way to take it back.
# That was tolerable while nothing was stored; enrolment made it not.


def _del_event(vp_id="vp-1", sub="sub-1"):
    return {"httpMethod": "DELETE",
            "path": f"/api/org/voiceprints/{vp_id}",
            "queryStringParameters": None, "body": None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def test_a_withdrawal_removes_the_vectors_and_reports_what_it_removed(wired, monkeypatch):
    monkeypatch.setattr(org.voiceprints, "withdraw",
                        lambda conn, company_id, vp: ["s1", "s2"])
    res = org.lambda_handler(_del_event(), None)
    assert res["statusCode"] == 200
    assert _body(res)["samplesRemoved"] == 2


def test_a_withdrawal_is_company_scoped(wired, monkeypatch):
    """The company comes from the caller, never the path. Otherwise a valid uuid from
    another tenant is a delete button on their data."""
    seen = {}
    monkeypatch.setattr(org.voiceprints, "withdraw",
                        lambda conn, company_id, vp: seen.update(
                            {"co": company_id, "vp": vp}) or [])
    org.lambda_handler(_del_event("vp-other"), None)
    assert seen["co"] == "c-1"


def test_a_worker_cannot_withdraw(wired, monkeypatch):
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    assert org.lambda_handler(_del_event(), None)["statusCode"] == 403


def test_withdrawal_404s_when_the_feature_is_off(wired, monkeypatch):
    """The rollback has to remove the whole surface, including the way out. Rows left behind
    by a switched-off feature are still rows somebody may need removed — but the route not
    existing is what `off` means everywhere else, and a half-present feature is worse."""
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    assert org.lambda_handler(_del_event(), None)["statusCode"] == 404


def test_the_request_artifact_carries_no_voice_vectors(wired):
    """The biometric-residence defect, fifth home. The reviews chased it out of an embedding
    cache and out of the enrolment result, and it was sitting in the `profiles` list all
    along: every correction wrote the full 192-d embedding of every consented profile in the
    company into an S3 object with no lifecycle rule and no deletion — so a withdrawn
    person's voiceprint stayed in the bucket after the withdrawal returned 200.

    They are not needed. Propagation names the cluster the corrected turn belongs to and
    never scores against a stored profile; the only thing the vectors produced was a `score`
    on the one row whose name the user typed himself.
    """
    org.lambda_handler(_event(BODY), None)
    doc = json.loads(wired.puts[0]["Body"])
    blob = json.dumps(doc)
    assert "embedding" not in blob, "a voice vector was written to S3"
    assert "profiles" not in doc, (
        "the artifact still carries a profile list; even empty, the field invites somebody "
        "to fill it")


# ---- taking a name off a meeting ---------------------------------------


def _unname_event(name="Ben L", sub="sub-1", session=None):
    return {"httpMethod": "DELETE",
            "path": f"/api/org/sessions/{session or SESSION}/speaker-names",
            "queryStringParameters": {"name": name}, "body": None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def test_a_name_can_be_taken_off_a_meeting(wired, monkeypatch):
    monkeypatch.setattr(org.voiceprints, "unname",
                        lambda conn, company_id, session_base, display_name, rejected_by=None: 3)
    res = org.lambda_handler(_unname_event(), None)
    assert res["statusCode"] == 200
    assert _body(res)["turnsUnnamed"] == 3


def test_it_works_when_there_is_no_voiceprint_to_withdraw(wired, monkeypatch):
    """The case that has no other route. A correction made without consent creates no
    profile, so `DELETE /voiceprints/{id}` has nothing to point at — on TEST that left six of
    seven named turns unreachable."""
    seen = {}
    monkeypatch.setattr(org.voiceprints, "unname",
                        lambda conn, company_id, session_base, display_name, rejected_by=None:
                        seen.update({"co": company_id, "s": session_base,
                                     "n": display_name}) or 6)
    org.lambda_handler(_unname_event(), None)
    assert seen["co"] == "c-1", "the company came from somewhere other than the caller"
    assert seen["n"] == "Ben L"
    import turn_name_overlay as tno
    assert seen["s"] == tno.session_base(SESSION), "the session key was not normalised"


def test_a_missing_name_is_refused(wired):
    ev = _unname_event()
    ev["queryStringParameters"] = None
    res = org.lambda_handler(ev, None)
    assert res["statusCode"] == 400


# `test_a_worker_cannot_take_a_name_off` was here and is deleted with the behaviour it
# pinned: a worker may now take a name off their OWN meeting (product decision 2026-08-14 —
# "每个用户都有权利标记对话的人是谁"). Three tests replaced it, at the bottom of this file,
# and they are stricter than it was: someone else's meeting is refused, an owner that cannot
# be established is refused, and the role arm still short-circuits the lookup.


def test_unnaming_404s_when_the_feature_is_off(wired, monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    assert org.lambda_handler(_unname_event(), None)["statusCode"] == 404


# ---- Phase 5: name a session from stored profiles ------------------------


SID = "sid" + "b" * 32
FULL = f"Benl1_2026-08-14_09-00-00_{SID}"


def _match_event(session=None, sub="sub-1"):
    return {"httpMethod": "POST",
            "path": f"/api/org/sessions/{session or FULL}/speaker-match",
            "body": json.dumps({"user": "Ben_UCPK2"}),
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def _turns(monkeypatch, n=3):
    monkeypatch.setattr(org, "_session_turns", lambda *a, **k: [
        {"source_filename": f"f{i}.json", "start_sec": float(i), "end_sec": float(i) + 5}
        for i in range(n)])


def test_a_match_can_be_asked_for_on_an_existing_session(wired, monkeypatch):
    """On demand rather than only at finalize: automatic naming helps future meetings, and
    the reason to want this at all is the archive that already exists."""
    _turns(monkeypatch)
    res = org.lambda_handler(_match_event(), None)
    assert res["statusCode"] == 202
    assert _body(res)["turnsQueued"] == 3


def test_the_queued_artifact_carries_no_voice_vectors(wired, monkeypatch):
    """The bucket has a 7-day expiry and nothing else. A voiceprint is biometric data whose
    storage was consented to in one specific column, and this defect has already relocated
    four times into stores nobody thought to sweep."""
    _turns(monkeypatch)
    org.lambda_handler(_match_event(), None)
    doc = json.loads(wired.puts[-1]["Body"])
    assert doc["op"] == "match"
    assert "profiles" not in doc and "embedding" not in json.dumps(doc)


def test_the_mode_travels_with_the_request(wired, monkeypatch):
    """The matcher must not read the switch itself: two readers of one switch disagree the
    moment one of them is deployed and the other is not."""
    _turns(monkeypatch)
    org.lambda_handler(_match_event(), None)
    assert json.loads(wired.puts[-1]["Body"])["mode"] == org.SPEAKER_IDENTITY_MODE


def test_the_answer_says_whether_names_will_actually_be_written(wired, monkeypatch):
    """"It ran and deliberately wrote nothing" and "it ran and found nobody" are different
    answers that otherwise look identical."""
    _turns(monkeypatch)
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "shadow")
    assert _body(org.lambda_handler(_match_event(), None))["willWriteNames"] is False
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    assert _body(org.lambda_handler(_match_event(), None))["willWriteNames"] is True


def test_a_session_with_no_turns_is_refused_rather_than_queued(wired, monkeypatch):
    """A 202 would promise work that cannot happen; the usual cause is transcripts that have
    not been written yet, which is a wait, not a failure."""
    monkeypatch.setattr(org, "_session_turns", lambda *a, **k: [])
    assert org.lambda_handler(_match_event(), None)["statusCode"] == 409


def test_a_worker_cannot_ask_for_a_match(wired, monkeypatch):
    _turns(monkeypatch)
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    assert org.lambda_handler(_match_event(), None)["statusCode"] == 403


def test_match_404s_when_the_feature_is_off(wired, monkeypatch):
    _turns(monkeypatch)
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    assert org.lambda_handler(_match_event(), None)["statusCode"] == 404


# ---- who may name a speaker --------------------------------------------
#
# Until 2026-08-14 this was a role list and nothing else, so a `worker` — the tier that
# means "you can see your own recordings and nobody else's" — could not put a name on a
# passage of their OWN meeting. That is the ordinary case, not an edge one: the person who
# pressed record is the person who knows who was in the room.
#
# The arm added here is ownership, and ONLY ownership. Visibility is untouched: a worker
# still cannot see, let alone rename, anyone else's recording.


def _as(role, folder="Ada_L"):
    c = dict(CALLER)
    c["global_role"] = role
    c["folder_name"] = folder
    return c


def test_a_worker_may_name_a_speaker_in_their_own_recording(wired, monkeypatch):
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: _as("worker"))
    monkeypatch.setattr(org.scope, "visible_scope",
                        lambda conn, caller: {"self_folder": "Ada_L", "user_scope": "SELF"})
    res = org.lambda_handler(_event(BODY), None)
    assert res["statusCode"] == 202, _body(res)
    assert len(wired.puts) == 1


def test_a_worker_may_not_name_a_speaker_in_someone_elses_recording(wired, monkeypatch):
    """The folder the request resolved to is not the caller's own. Refused even though the
    ACL let the read through — the two questions are different."""
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: _as("worker"))
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Someone_Else", None))
    monkeypatch.setattr(org.scope, "visible_scope",
                        lambda conn, caller: {"self_folder": "Ada_L", "user_scope": "SELF"})
    res = org.lambda_handler(_event(BODY), None)
    assert res["statusCode"] == 403, _body(res)
    assert wired.puts == [], "a correction was queued for another person's recording"


def test_a_role_that_can_VIEW_another_folder_still_cannot_name_in_it(wired, monkeypatch):
    """`regional_manager` is NOT in the correction roles, but its scope is SITE — it can
    view any member of an in-scope site, so `_resolve_org_media_folder` returns their folder
    happily. Leaning on that resolve succeeding would have handed it the write. The gate
    compares against the caller's OWN folder, not against whether the read was allowed."""
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: _as("regional_manager"))
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Someone_Else", None))
    monkeypatch.setattr(org.scope, "visible_scope",
                        lambda conn, caller: {"self_folder": "Ada_L", "user_scope": "SITE"})
    res = org.lambda_handler(_event(BODY), None)
    assert res["statusCode"] == 403, _body(res)
    assert wired.puts == []


def test_a_manager_still_names_in_anyone_elses_recording(wired, monkeypatch):
    """The role arm is unchanged. This is the behaviour that already shipped."""
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: _as("site_manager"))
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Someone_Else", None))
    monkeypatch.setattr(org.scope, "visible_scope",
                        lambda conn, caller: {"self_folder": "Ada_L",
                                              "user_scope": "SELF_WORKERS"})
    res = org.lambda_handler(_event(BODY), None)
    assert res["statusCode"] == 202, _body(res)
    assert len(wired.puts) == 1


# ---- taking a name off YOUR OWN meeting ---------------------------------
#
# The DELETE carries a session and a name and nothing else — no folder — so "is this yours?"
# has to be answered from the session itself. It is answered against `topics`, whose
# extraction keys are `extractions/{folder}/{date}/{session_base}.json`, and NOT by parsing
# the folder out of the session id: the session id is caller input, and using caller input
# to decide whose recording it is would be the whole guard undone.


def _owned_by(*folders):
    return lambda conn, company_id, session_base: list(folders)


def test_a_worker_can_take_a_name_off_their_own_meeting(wired, monkeypatch):
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    monkeypatch.setattr(org.topics, "folders_for_session_base", _owned_by("Ada_L"))
    monkeypatch.setattr(org.voiceprints, "unname",
                        lambda conn, company_id, session_base, display_name, rejected_by=None: 3)
    res = org.lambda_handler(_unname_event(), None)
    assert res["statusCode"] == 200, _body(res)
    assert _body(res)["turnsUnnamed"] == 3


def test_a_worker_cannot_take_a_name_off_someone_elses_meeting(wired, monkeypatch):
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    monkeypatch.setattr(org.topics, "folders_for_session_base", _owned_by("Someone_Else"))
    called = []
    monkeypatch.setattr(org.voiceprints, "unname",
                        lambda *a, **k: called.append(1) or 3)
    assert org.lambda_handler(_unname_event(), None)["statusCode"] == 403
    assert called == [], "a name was removed from another crew's meeting"


def test_an_unestablished_owner_is_a_refusal_not_a_pass(wired, monkeypatch):
    """No topics for the session yet — extraction may simply not have landed. Length != 1 is
    'could not establish', and could-not-establish is never permission."""
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    monkeypatch.setattr(org.topics, "folders_for_session_base", _owned_by())
    called = []
    monkeypatch.setattr(org.voiceprints, "unname", lambda *a, **k: called.append(1) or 3)
    assert org.lambda_handler(_unname_event(), None)["statusCode"] == 403
    assert called == []


def test_a_manager_does_not_pay_for_the_ownership_lookup(wired, monkeypatch):
    """The role arm short-circuits: managers were already allowed and must not start
    depending on a query that can legitimately return nothing."""
    looked = []
    monkeypatch.setattr(org.topics, "folders_for_session_base",
                        lambda *a, **k: looked.append(1) or [])
    monkeypatch.setattr(org.voiceprints, "unname",
                        lambda conn, company_id, session_base, display_name, rejected_by=None: 2)
    res = org.lambda_handler(_unname_event(), None)
    assert res["statusCode"] == 200, _body(res)
    assert looked == [], "the ownership query ran for a caller the role arm already allowed"
# ---- a long session is split, not timed out ------------------------------
#
# Measured on the deployed embedder: ~98 ms per second of audio. A three-hour
# meeting exceeds the 600 s Lambda timeout, and the failure would arrive at the
# very end with nothing written.


def test_a_long_session_is_split_across_runs(wired, monkeypatch):
    monkeypatch.setattr(org, "_session_turns", lambda *a, **k: [
        {"source_filename": f"f{i}.json", "start_sec": 0.0, "end_sec": 120.0}
        for i in range(60)])   # 7200 s of speech, twice the per-run budget
    body = _body(org.lambda_handler(_match_event(), None))
    assert body["runs"] >= 2, "one run would have timed out with nothing written"
    assert body["turnsQueued"] == 60
    assert len(wired.puts) == body["runs"]


def test_every_turn_lands_in_exactly_one_run(wired, monkeypatch):
    """Dropping a turn would be invisible: fewer names is what a refusal looks like."""
    monkeypatch.setattr(org, "_session_turns", lambda *a, **k: [
        {"source_filename": f"f{i}.json", "start_sec": 0.0, "end_sec": 120.0}
        for i in range(60)])
    org.lambda_handler(_match_event(), None)
    seen = [t["source_filename"]
            for p in wired.puts for t in json.loads(p["Body"])["turns"]]
    assert sorted(seen) == sorted(f"f{i}.json" for i in range(60))
    assert len(seen) == len(set(seen)), "a turn was queued twice"


def test_a_short_session_is_still_one_run(wired, monkeypatch):
    _turns(monkeypatch, 3)
    assert _body(org.lambda_handler(_match_event(), None))["runs"] == 1


def test_each_run_says_which_part_it_is(wired, monkeypatch):
    monkeypatch.setattr(org, "_session_turns", lambda *a, **k: [
        {"source_filename": f"f{i}.json", "start_sec": 0.0, "end_sec": 120.0}
        for i in range(60)])
    org.lambda_handler(_match_event(), None)
    docs = [json.loads(p["Body"]) for p in wired.puts]
    assert sorted(d["part"] for d in docs) == list(range(1, len(docs) + 1))
    assert {d["of"] for d in docs} == {len(docs)}


def test_removing_a_name_records_who_rejected_it(wired, monkeypatch):
    """The tombstone stops inference re-deriving the name. Who rejected it is the part that
    makes the record answerable later — the same discipline as `consented_by`."""
    seen = {}
    monkeypatch.setattr(org.voiceprints, "unname",
                        lambda conn, company_id, session_base, display_name,
                        rejected_by=None: seen.update({"by": rejected_by}) or 1)
    org.lambda_handler(_unname_event(), None)
    assert seen["by"], "the rejection has no author"


# ---- a profile belongs to a person, not to a string ----------------------
#
# `profiles_for_matching`'s site branch keeps a profile when `p.user_id IS NULL`
# — deliberately, so an unnamed recurring voice stays in scope. But nothing has
# ever written `user_id`, so every profile takes that escape and site narrowing
# has been a no-op since the day it was written.


def _consenting(**over):
    return dict(BODY, consent_given=True, consented_by="u-9", **over)


def test_a_named_correction_links_the_profile_to_the_directory(wired, monkeypatch):
    seen = {}
    monkeypatch.setattr(org.users, "resolve_display_name",
                        lambda conn, company_id, name: ({"id": "u-42"}, "folder_name"))
    monkeypatch.setattr(org.voiceprints, "upsert_profile",
                        lambda conn, company_id, **kw: seen.update(kw) or {"id": "vp-1"})
    b = _body(org.lambda_handler(_event(_consenting()), None))
    assert seen["user_id"] == "u-42"
    assert seen["linked_on"] == "folder_name"
    assert seen["linked_by"], "the link has no author"
    assert b["linkedTo"] == {"userId": "u-42", "matchedOn": "folder_name"}


def test_an_ambiguous_name_links_to_nobody_and_says_why(wired, monkeypatch):
    """Two people called Ben. Guessing files one person's voice under another's identity,
    and the site filter then hides it from the site they are on while offering it on one
    they are not — a wrong answer nobody can see."""
    seen = {}
    monkeypatch.setattr(org.users, "resolve_display_name",
                        lambda conn, company_id, name: (None, "ambiguous"))
    monkeypatch.setattr(org.voiceprints, "upsert_profile",
                        lambda conn, company_id, **kw: seen.update(kw) or {"id": "vp-1"})
    b = _body(org.lambda_handler(_event(_consenting()), None))
    assert seen["user_id"] is None
    assert b["linkedTo"] is None
    assert b["linkReason"] == "ambiguous"


def test_a_name_not_in_the_directory_still_makes_a_working_profile(wired, monkeypatch):
    monkeypatch.setattr(org.voiceprints, "upsert_profile",
                        lambda conn, company_id, **kw: {"id": "vp-1"})
    b = _body(org.lambda_handler(_event(_consenting()), None))
    assert b["enrolment"] == "requested"
    assert b["linkReason"] == "not-in-directory"


def test_a_correction_without_consent_reports_no_link_rather_than_a_false_one(wired):
    b = _body(org.lambda_handler(_event(BODY), None))
    assert b["linkedTo"] is None
    assert b["linkReason"] == "not-requested"


# ---- the site travels with the request -----------------------------------
#
# The matcher reads `req.get("site_id")` and the artifact never carried the key,
# so the site-scoped query was inert for a SECOND, independent reason. Fixing
# only the query would have moved nothing.


def test_the_match_request_carries_the_site(wired, monkeypatch):
    _turns(monkeypatch)
    monkeypatch.setattr(org, "_site_for_session",
                        lambda conn, company_id, folder, date, session_base: {"id": "st-1"})
    body = _body(org.lambda_handler(_match_event(), None))
    assert body["siteId"] == "st-1"
    assert all(json.loads(p["Body"])["site_id"] == "st-1" for p in wired.puts)


def test_an_unresolved_site_means_no_narrowing_not_a_failure(wired, monkeypatch):
    """Before this feature there was no narrowing at all. An unresolved site returns to
    that, which is the safe direction — narrowing to the WRONG roster drops the right
    person and presents as a refusal."""
    _turns(monkeypatch)
    res = org.lambda_handler(_match_event(), None)
    assert res["statusCode"] == 202
    assert json.loads(wired.puts[-1]["Body"])["site_id"] is None


def test_every_run_of_a_split_session_carries_the_same_site(wired, monkeypatch):
    monkeypatch.setattr(org, "_site_for_session",
                        lambda conn, company_id, folder, date, session_base: {"id": "st-1"})
    monkeypatch.setattr(org, "_session_turns", lambda *a, **k: [
        {"source_filename": f"f{i}.json", "start_sec": 0.0, "end_sec": 120.0}
        for i in range(60)])
    org.lambda_handler(_match_event(), None)
    assert {json.loads(p["Body"])["site_id"] for p in wired.puts} == {"st-1"}


def test_the_session_exact_site_beats_the_days_majority(monkeypatch):
    """`lambda_item_writer` settled this order after BUG-41. Starting at the day-majority
    rung gives somebody who recorded at two sites in one day the wrong site — the pool
    narrows to the wrong roster and the right person is dropped."""
    monkeypatch.setattr(org.recordings, "site_for_media",
                        lambda *a, **k: {"id": "exact"})
    called = []
    monkeypatch.setattr(org.recordings, "site_for_day",
                        lambda *a, **k: called.append(1) or {"id": "majority"})
    out = org._site_for_session(None, "co-1", "Ada_L", "2026-08-16", "x_sid" + "a" * 32)
    assert out == {"id": "exact"}
    assert called == [], "the day-majority rung ran even though the session was known"


def test_a_site_from_another_tenant_is_refused(monkeypatch):
    """It would narrow this company's pool against a roster that is not theirs."""
    monkeypatch.setattr(org.recordings, "site_for_media", lambda *a, **k: None)
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: {"site_id": "st-9"})
    monkeypatch.setattr(org.sites, "get_site",
                        lambda conn, sid: {"id": "st-9", "company_id": "other-co"})
    monkeypatch.setattr(org.recordings, "site_for_day", lambda *a, **k: None)
    assert org._site_for_session(None, "co-1", "Ada_L", "2026-08-16",
                                 "x_sid" + "a" * 32) is None


# ---- being able to see why a profile is empty ----------------------------


def _list_event(sub="sub-1"):
    return {"httpMethod": "GET", "path": "/api/org/voiceprints", "body": None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def test_a_profile_says_what_happened_the_last_time_somebody_tried(wired, monkeypatch):
    """"Empty because the window was refused" and "empty because the embedder died" produce
    the same row. Both happened on TEST on 2026-08-16 and nothing distinguished them."""
    monkeypatch.setattr(org.voiceprints, "list_profiles", lambda conn, co: [
        {"id": "vp-1", "display_name": "Ben Lin", "status": "tentative", "user_id": "u-1",
         "linked_on": "folder_name", "consent_at": None, "samples": 0, "human_samples": 0,
         "last_attempt_at": "2026-08-16T17:00:00Z", "last_attempt_outcome": "refused",
         "last_attempt_detail": "this window does not hold one voice"}])
    b = _body(org.lambda_handler(_list_event(), None))
    row = b["voiceprints"][0]
    assert row["samples"] == 0
    assert row["lastAttemptOutcome"] == "refused"
    assert "one voice" in row["lastAttemptDetail"]


def test_the_listing_separates_vouched_samples_from_inferred_ones(wired, monkeypatch):
    monkeypatch.setattr(org.voiceprints, "list_profiles", lambda conn, co: [
        {"id": "vp-1", "display_name": "Ben Lin", "status": "tentative", "user_id": None,
         "linked_on": None, "consent_at": None, "samples": 6, "human_samples": 1,
         "last_attempt_at": None, "last_attempt_outcome": None,
         "last_attempt_detail": None}])
    row = _body(org.lambda_handler(_list_event(), None))["voiceprints"][0]
    assert row["samples"] == 6 and row["humanSamples"] == 1


def test_the_listing_never_carries_a_vector(wired, monkeypatch):
    """Biometric data. Nothing in a listing needs it, and the one place it may travel is the
    synchronous fetch the matcher makes."""
    monkeypatch.setattr(org.voiceprints, "list_profiles", lambda conn, co: [
        {"id": "vp-1", "display_name": "X", "status": "tentative", "user_id": None,
         "linked_on": None, "consent_at": None, "samples": 1, "human_samples": 1,
         "embedding": [0.1] * 192,
         "last_attempt_at": None, "last_attempt_outcome": None, "last_attempt_detail": None}])
    assert "embedding" not in json.dumps(_body(org.lambda_handler(_list_event(), None)))


def test_a_worker_cannot_list_voiceprints(wired, monkeypatch):
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    assert org.lambda_handler(_list_event(), None)["statusCode"] == 403


def test_listing_404s_when_the_feature_is_off(wired, monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    assert org.lambda_handler(_list_event(), None)["statusCode"] == 404
