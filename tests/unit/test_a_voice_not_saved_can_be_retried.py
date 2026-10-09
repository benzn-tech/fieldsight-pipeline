"""A refused voice must say so, and say how to store it again.

Owner, 2026-10-01: "no silent failure or success". The homogeneity guard refuses a thin or
mixed window; until now the only trace was a row on the Voices page and no way to try again.

Fixtures use the REAL shapes: `session_base` is the bare `sid<32hex>`, and the turn_ref stem
carries a DEVICE prefix that is not the user folder (see
test_a_confirmed_proposal_is_a_human_correction.py for the 400 a dated spelling once hid).
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO = "11111111-1111-1111-1111-111111111111"
VP = "22222222-2222-2222-2222-222222222222"
VP2 = "44444444-4444-4444-4444-444444444444"
SESSION = "sid9db9293e82b94a4d9611572b1233f82d"
STEM = "Benl1_2026-09-30_11-49-00_sid9db9293e82b94a4d9611572b1233f82d_c0000_off0.0_to60.0_srcwav"
SRC = STEM + ".json"

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": CO, "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-09-30"}


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


def _event(method, path, body=None, qs=None):
    return {"httpMethod": method, "path": "/api/org" + path, "queryStringParameters": qs,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


def _body(resp):
    return json.loads(resp["body"])


def _profile(vid, outcome, name="Ben Lin"):
    return {"id": vid, "display_name": name, "status": "active", "user_id": None,
            "linked_on": None, "consent_at": None, "samples": 0, "human_samples": 0,
            "last_attempt_at": "2026-09-30T01:00:00Z", "last_attempt_outcome": outcome,
            "last_attempt_detail": "window too mixed"}


@pytest.fixture
def wired(monkeypatch):
    st = {"profiles": [_profile(VP, "refused"), _profile(VP2, "enrolled", "Sam Yu")],
          "corrections": {VP: {"session_base": SESSION, "turn_ref": STEM + "@9.0"}},
          "located": ("Ben_UCPK", "2026-09-30"),
          "turn_reads": [], "correction_lookups": [], "locates": [], "queued": []}

    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(org.voiceprints, "list_profiles", lambda conn, co: st["profiles"])
    monkeypatch.setattr(org.voiceprints, "get_profile",
                        lambda conn, co, vid: next(
                            (dict(p) for p in st["profiles"] if p["id"] == vid), None))

    def latest(conn, co, vid, name):
        st["correction_lookups"].append(vid)
        return st["corrections"].get(vid)

    def locate(conn, co, sb):
        st["locates"].append(sb)
        return st["located"]

    def turns(conn, folder, date, sb, with_text=False, **k):
        st["turn_reads"].append((folder, date, sb))
        return [{"source_filename": SRC, "speaker_label": "spk_0",
                 "start_sec": 0.0, "end_sec": 4.0},
                {"source_filename": SRC, "speaker_label": "spk_0",
                 "start_sec": 9.0, "end_sec": 31.0}]

    monkeypatch.setattr(org.voiceprints, "latest_human_correction", latest)
    monkeypatch.setattr(org.recordings, "locate_session", locate)
    monkeypatch.setattr(org, "_session_turns", turns)
    monkeypatch.setattr(org.voiceprints, "refused_recently_count", lambda conn, co, days=7: 2)
    monkeypatch.setattr(org.speaker_name_proposals, "pending_by_person", lambda conn, co: [])
    monkeypatch.setattr(org.speaker_intro_suggestions, "pending_count", lambda conn, co: 0)

    def corrections(conn, caller, sid, event):
        st["queued"].append((sid, json.loads(event["body"])))
        return {"statusCode": 202, "headers": {}, "body": json.dumps({"status": "queued"})}

    monkeypatch.setattr(org, "speaker_corrections", corrections)
    return st


def _rows(resp):
    return {r["id"]: r for r in _body(resp)["voiceprints"]}


# ---- 1. GET /voiceprints -------------------------------------------------------------

def test_a_refused_row_carries_the_passage_to_try_again(wired):
    rows = _rows(org.lambda_handler(_event("GET", "/voiceprints"), None))
    assert rows[VP]["retry"] == {
        "date": "2026-09-30", "userFolder": "Ben_UCPK", "sessionBase": SESSION,
        "sourceFilename": SRC, "startSec": 9.0, "endSec": 31.0}


def test_the_folder_comes_from_the_recording_not_from_the_filename_prefix(wired):
    """The device prefix `Benl1` is not the folder `Ben_UCPK`."""
    rows = _rows(org.lambda_handler(_event("GET", "/voiceprints"), None))
    assert rows[VP]["retry"]["userFolder"] == "Ben_UCPK" != STEM.split("_")[0]
    assert wired["locates"] == [SESSION]
    assert wired["turn_reads"] == [("Ben_UCPK", "2026-09-30", SESSION)]


def test_only_refused_rows_pay_for_the_lookup(wired):
    rows = _rows(org.lambda_handler(_event("GET", "/voiceprints"), None))
    assert rows[VP2]["retry"] is None
    assert wired["correction_lookups"] == [VP], "a non-refused row triggered a lookup"


def test_retry_is_null_when_the_session_cannot_be_located(wired):
    wired["located"] = None
    rows = _rows(org.lambda_handler(_event("GET", "/voiceprints"), None))
    assert rows[VP]["retry"] is None
    assert wired["turn_reads"] == []


def test_retry_is_null_when_no_turn_matches(wired, monkeypatch):
    monkeypatch.setattr(org, "_session_turns", lambda conn, f, d, sb, with_text=False, **k: [
        {"source_filename": SRC, "start_sec": 50.0, "end_sec": 60.0}])
    rows = _rows(org.lambda_handler(_event("GET", "/voiceprints"), None))
    assert rows[VP]["retry"] is None


def test_retry_is_null_when_no_human_correction_exists(wired):
    wired["corrections"] = {}
    rows = _rows(org.lambda_handler(_event("GET", "/voiceprints"), None))
    assert rows[VP]["retry"] is None


def test_an_unreadable_transcript_does_not_break_the_listing(wired, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("s3 down")
    monkeypatch.setattr(org, "_session_turns", boom)
    resp = org.lambda_handler(_event("GET", "/voiceprints"), None)
    assert resp["statusCode"] == 200
    assert _rows(resp)[VP]["retry"] is None


# ---- 2. POST /voiceprints/{id}/retry -------------------------------------------------

def test_retry_delegates_to_speaker_corrections_with_the_same_passage(wired):
    resp = org.lambda_handler(_event("POST", f"/voiceprints/{VP}/retry"), None)
    assert resp["statusCode"] == 202, resp
    assert wired["queued"] == [(SRC, {
        "user": "Ben_UCPK", "source_filename": SRC, "start_sec": 9.0, "end_sec": 31.0,
        "display_name": "Ben Lin",
        # the known profile, so a retry can never land on a same-named one
        "voiceprint_id": VP})]


def test_retry_with_nothing_to_retry_is_a_plain_409(wired):
    wired["corrections"] = {}
    resp = org.lambda_handler(_event("POST", f"/voiceprints/{VP}/retry"), None)
    assert resp["statusCode"] == 409
    assert "error" in _body(resp) and wired["queued"] == []


def test_retry_on_a_profile_that_was_not_refused_is_409(wired):
    resp = org.lambda_handler(_event("POST", f"/voiceprints/{VP2}/retry"), None)
    assert resp["statusCode"] == 409 and wired["queued"] == []


def test_retry_on_another_companys_profile_is_404(wired):
    resp = org.lambda_handler(_event("POST", "/voiceprints/nobody/retry"), None)
    assert resp["statusCode"] == 404 and wired["queued"] == []


def test_retry_needs_a_naming_role(wired, monkeypatch):
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    resp = org.lambda_handler(_event("POST", f"/voiceprints/{VP}/retry"), None)
    assert resp["statusCode"] == 403 and wired["queued"] == []


def test_retry_is_gone_when_the_feature_is_off(wired, monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    resp = org.lambda_handler(_event("POST", f"/voiceprints/{VP}/retry"), None)
    assert resp["statusCode"] == 404 and wired["queued"] == []


def test_retry_is_not_swallowed_by_the_withdraw_route(wired, monkeypatch):
    """`DELETE /voiceprints/{id}` withdraws a biometric. A POST to `.../retry` must never
    reach it, whatever the route order."""
    called = []
    monkeypatch.setattr(org, "withdraw_voiceprint", lambda *a, **k: called.append(a))
    org.lambda_handler(_event("POST", f"/voiceprints/{VP}/retry"), None)
    assert called == []


# ---- 3. GET /name-proposals badge ----------------------------------------------------

def test_the_badge_reports_how_many_voices_were_not_saved(wired):
    b = _body(org.lambda_handler(_event("GET", "/name-proposals"), None))
    assert b["notSaved"] == 2


def test_the_badge_not_saved_count_reads_no_transcript(wired):
    org.lambda_handler(_event("GET", "/name-proposals"), None)
    assert wired["turn_reads"] == [] and wired["correction_lookups"] == []


def test_the_per_person_shape_is_unchanged(wired, monkeypatch):
    monkeypatch.setattr(org.speaker_name_proposals, "pending_for_person",
                        lambda conn, co, vp, limit=5: [])
    b = _body(org.lambda_handler(_event("GET", "/name-proposals", qs={"voiceprint": VP}), None))
    assert "notSaved" not in b
