"""Unit: POST /api/org/sessions/{base}/speaker-corrections — the first user-facing surface
of speaker naming.

This endpoint is the only entry to a chain that is otherwise fully built and entirely inert:
it queues one S3 request artifact, the non-VPC embedder picks it up, and an in-VPC writer
persists the result. org-api cannot invoke a Lambda (in-VPC, no NAT — BUG-36), so the handoff
is an artifact, exactly like `session_report_requests/`.

Two things are load-bearing here and both fail silently if they regress:

  * **the mode gate** — `SPEAKER_IDENTITY_MODE=off` must 404, because that is the rollback.
    A rollback that only stops *some* of the feature is not a rollback.
  * **the profile query** — the consent and withdrawn filters live in
    `profiles_for_matching`, and this endpoint is the one caller. A profile without consent
    that still gets matched keeps naming people, correctly as far as anything downstream can
    tell.
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
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


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
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ada_L", None))
    monkeypatch.setattr(org, "profiles_for_matching",
                        lambda conn, company_id, site_id=None: [
                            {"id": "vp-1", "user_id": "u-9", "display_name": "Ben L",
                             "status": "confirmed", "embedding": [0.1] * 192}])
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


def test_the_artifact_carries_the_profiles_the_query_allowed(wired):
    """org-api reads them because it is the half with a database. The embedder has no way
    to obtain a profile except from here, which is what keeps the consent filter in one
    place instead of two."""
    org.lambda_handler(_event(BODY), None)
    doc = json.loads(wired.puts[0]["Body"])
    assert [p["person_key"] for p in doc["profiles"]] == ["u-9"]
    assert len(doc["profiles"][0]["embedding"]) == 192


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


def test_a_worker_may_not_correct(wired, monkeypatch):
    """Naming a voice is a claim about a person that follows them into future sessions."""
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
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
    monkeypatch.setattr(org, "_read_org_transcripts", lambda d, f, a, b: {
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
