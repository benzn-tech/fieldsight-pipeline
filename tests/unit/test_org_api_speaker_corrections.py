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
SESSION = "Benl1_2026-08-13_11-49-00"
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
    assert doc["session_base"] == SESSION


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
