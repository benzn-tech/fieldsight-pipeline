"""org-api records a lake_objects row at the moment it mints a key (lake ownership, Task 2).

Contract: the row is best effort. The upload route is synchronous with no retry (BUG-43), so a
failed index write must neither fail the response nor abort the caller's transaction. The owner
follows the sub-project 3 rule: the SITE's company, else the recorder's home company.

The handlers in the second half are driven through the fixtures of their existing test modules
(imported below); each test only adds "the recorder saw the key that was also put/signed".
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

import tests.unit.test_expedite_and_pending as t_exp  # noqa: E402
import tests.unit.test_naming_same_name as t_name  # noqa: E402
import tests.unit.test_org_api_generate_request as t_gen  # noqa: E402
import tests.unit.test_regenerate_endpoint as t_regen  # noqa: E402
from tests.unit.test_a_person_regenerates_their_own_report import (  # noqa: E402,F401
    _post as regen_own_post, s3 as regen_own_s3)
from tests.unit.test_org_api_sessions import wired  # noqa: E402,F401
from tests.unit.test_expedite_and_pending import web  # noqa: E402,F401
from tests.unit.test_naming_same_name import post  # noqa: E402,F401
from tests.unit.test_org_api_generate_request import day  # noqa: E402,F401
from tests.unit.test_the_days_report_photos_can_be_chosen import api as photo_api  # noqa: E402,F401

COMPANY_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"      # the recorder's home company
COMPANY_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"      # the site's company
SITE = "cccccccc-cccc-cccc-cccc-cccccccccccc"

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": COMPANY_A, "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "global_role": "worker"}


class _Savepoint:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def transaction(self):
        return _Savepoint()


class FakeS3:
    def __init__(self):
        self.puts = []

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        return "https://signed/" + Params["Key"]

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    def rec(conn, key, **kw):
        calls.append({"key": key, **kw})
        return True
    monkeypatch.setattr(org.lake_objects, "record_best_effort", rec)
    return calls


@pytest.fixture
def upload(monkeypatch):
    s3 = FakeS3()
    state = {"existing": None, "inserted": []}
    monkeypatch.setattr(org, "s3", lambda: s3)
    monkeypatch.setattr(org.sites, "get_site",
                        lambda conn, sid: {"id": sid, "company_id": COMPANY_B})
    monkeypatch.setattr(org.memberships, "get_membership",
                        lambda conn, uid, sid: {"archived_at": None})
    monkeypatch.setattr(org.recordings, "get_by_client_uuid",
                        lambda conn, uid, cu: state["existing"])

    def insert(conn, **kw):
        state["inserted"].append(kw)
        return {"id": "rec-1"}
    monkeypatch.setattr(org.recordings, "insert_pending", insert)
    monkeypatch.setattr(org.device_heartbeat, "device_id", lambda conn, ident: None)
    monkeypatch.setattr(org, "_adopt_group_from_upload", lambda *a, **k: None)
    return state


def _mint(body_extra=None, conn=None):
    body = {"kind": "audio", "clientUuid": "cu-1", "fileName": "a_2026-10-12_09-00-00.wav",
            "contentType": "audio/wav", "startedAt": "2026-10-12T09:00:00+13:00"}
    body.update(body_extra or {})
    return org.create_recording_upload_url(conn or FakeConn(), dict(CALLER), body)


# ---- create_recording_upload_url -----------------------------------------------------

def test_an_upload_on_a_site_of_another_company_records_the_sites_company(upload, recorder):
    res = _mint({"siteId": SITE})
    assert res["statusCode"] == 200
    assert len(recorder) == 1
    r = recorder[0]
    assert r["key"] == json.loads(res["body"])["s3Key"]
    assert r["company_id"] == COMPANY_B and r["site_id"] == SITE
    assert r["user_id"] == "u-1" and r["source"] == "upload"


def test_an_upload_without_a_site_records_the_recorders_home_company(upload, recorder):
    assert _mint()["statusCode"] == 200
    assert recorder[0]["company_id"] == COMPANY_A and recorder[0]["site_id"] is None


def test_an_idempotent_resend_records_the_same_key_again(upload, recorder):
    first = json.loads(_mint({"siteId": SITE})["body"])
    upload["existing"] = {"id": "rec-1", "s3_key": first["s3Key"]}
    again = json.loads(_mint({"siteId": SITE})["body"])
    assert again["s3Key"] == first["s3Key"]
    assert [c["key"] for c in recorder] == [first["s3Key"]] * 2


def test_a_failing_index_write_never_fails_the_upload(upload, monkeypatch, caplog):
    """The REAL record_best_effort, with the repository write exploding: the route still
    answers 200 with the upload URL, and the failure leaves a WARNING."""
    def boom(conn, key, **kw):
        raise RuntimeError("index is on fire")
    monkeypatch.setattr(org.lake_objects, "upsert", boom)
    with caplog.at_level("WARNING"):
        res = _mint({"siteId": SITE})
    assert res["statusCode"] == 200
    assert json.loads(res["body"])["uploadUrl"].startswith("https://signed/")
    assert any("could not record" in r.getMessage() for r in caplog.records)


def test_a_connection_that_cannot_open_a_savepoint_never_fails_the_upload(upload):
    class Exploding(FakeConn):
        opened = 0

        def transaction(self):
            Exploding.opened += 1
            if Exploding.opened > 1:        # the first one is the route's own insert savepoint
                raise RuntimeError("db is on fire")
            return _Savepoint()
    res = _mint(conn=Exploding())
    assert Exploding.opened > 1             # the index write really was attempted
    assert res["statusCode"] == 200


# ---- record_best_effort itself --------------------------------------------------------

def test_record_best_effort_swallows_any_error_and_reports_false(caplog):
    class Boom:
        def transaction(self):
            raise ValueError("boom")
    with caplog.at_level("WARNING"):
        assert org.lake_objects.record_best_effort(
            Boom(), "users/Ada_L/audio/2026-10-12/a.wav", company_id=COMPANY_A) is False
    assert any("could not record" in r.getMessage() for r in caplog.records)


def test_record_best_effort_uses_a_savepoint_and_the_parsed_fields(monkeypatch):
    seen, entered = {}, []

    class Conn:
        def transaction(self):
            entered.append(1)
            return _Savepoint()
    monkeypatch.setattr(org.lake_objects, "upsert",
                        lambda conn, key, **kw: seen.update(key=key, **kw) or True)
    assert org.lake_objects.record_best_effort(
        Conn(), "users/Ada_L/audio/2026-10-12/a.wav", company_id=COMPANY_B, site_id=SITE,
        user_id="u-1") is True
    assert entered, "must run inside conn.transaction() (savepoint)"
    assert (seen["kind"], seen["folder"], str(seen["day"]), seen["source"]) == (
        "media", "Ada_L", "2026-10-12", "upload")
    assert seen["company_id"] == COMPANY_B and seen["site_id"] == SITE


def test_an_unparseable_key_still_records_with_its_prefix_as_kind(monkeypatch):
    seen = {}
    monkeypatch.setattr(org.lake_objects, "upsert",
                        lambda conn, key, **kw: seen.update(kw) or True)
    org.lake_objects.record_best_effort(FakeConn(), "mystery/thing.bin", company_id=COMPANY_A)
    assert seen["kind"] == "mystery"


# ---- the owner helpers ----------------------------------------------------------------

def test_the_site_helper_prefers_the_sites_company_and_falls_back(monkeypatch):
    monkeypatch.setattr(org.sites, "get_site",
                        lambda conn, sid: {"id": sid, "company_id": COMPANY_B})
    assert org._lake_company_for_sites(FakeConn(), CALLER, [SITE]) == COMPANY_B
    assert org._lake_company_for_sites(FakeConn(), CALLER, []) == COMPANY_A
    assert org._lake_company_for_sites(FakeConn(), CALLER, [SITE, "other"]) == COMPANY_A

    def boom(conn, sid):
        raise RuntimeError("x")
    monkeypatch.setattr(org.sites, "get_site", boom)
    assert org._lake_company_for_sites(FakeConn(), CALLER, [SITE]) == COMPANY_A


def test_the_session_helper_needs_exactly_one_site_company(monkeypatch):
    monkeypatch.setattr(org.recordings, "session_site_companies",
                        lambda conn, f, d, s: [("s1", COMPANY_B)])
    assert org._lake_company_for_session(FakeConn(), CALLER, "F", "2026-10-12", "sid") == COMPANY_B
    monkeypatch.setattr(org.recordings, "session_site_companies",
                        lambda conn, f, d, s: [("s1", COMPANY_B), ("s2", COMPANY_A)])
    assert org._lake_company_for_session(FakeConn(), CALLER, "F", "2026-10-12", "sid") == COMPANY_A
    monkeypatch.setattr(org.recordings, "session_site_companies", lambda conn, f, d, s: [])
    assert org._lake_company_for_session(FakeConn(), CALLER, "F", "2026-10-12", "sid") == COMPANY_A


# ---- the other minters: the recorder saw the key that was put / signed -----------------

def test_regenerate_own_report_records_its_request_key(regen_own_s3, recorder):
    res = regen_own_post({"report_type": "daily", "date": "2026-09-03"})
    assert res["statusCode"] == 202
    put = regen_own_s3["puts"][0]
    assert [c["key"] for c in recorder] == [put["Key"]]
    assert put["Key"].startswith("report_requests/Ada_L/")


def test_session_expedite_records_the_marker_key(web, recorder):
    assert org.session_expedite(None, t_exp.CALLER, t_exp.BASE)["statusCode"] == 200
    assert [c["key"] for c in recorder] == [web.puts[-1][0]]


def test_regenerate_session_records_the_extraction_request_key(monkeypatch, recorder):
    s3 = t_regen._S3()
    monkeypatch.setattr(org, "S3_BUCKET", "bucket")
    monkeypatch.setattr(org.boto3, "client", lambda name, **kw: s3)
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what: ("Ben_UCPK2", None))
    monkeypatch.setattr(org, "_lake_company_for_session", lambda *a: COMPANY_B)
    res = t_regen._call(monkeypatch, s3, [])
    assert res["statusCode"] == 202
    assert [c["key"] for c in recorder] == [s3.puts[0]["Key"]]
    assert recorder[0]["company_id"] == COMPANY_B


def test_voice_upload_records_the_site_company(monkeypatch, recorder):
    monkeypatch.setattr(org, "s3", lambda: FakeS3())
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE})
    monkeypatch.setattr(org.sites, "get_site",
                        lambda conn, sid: {"id": sid, "company_id": COMPANY_B})
    res = org.create_voice_upload_url(FakeConn(), dict(CALLER),
                                      {"siteId": SITE, "contentType": "audio/mp4"})
    assert res["statusCode"] == 200
    key = json.loads(res["body"])["s3Key"]
    assert [c["key"] for c in recorder] == [key]
    assert recorder[0]["company_id"] == COMPANY_B and recorder[0]["site_id"] == SITE


def test_photo_selection_records_the_selection_key(photo_api, recorder):
    call, store = photo_api
    code, _ = call("PUT", {"excluded": []})
    assert code == 200
    assert [c["key"] for c in recorder] == list(store.objects)


def test_speaker_corrections_records_the_request_key(post, recorder):
    res = t_name._correct()
    assert res["statusCode"] == 202
    assert len(recorder) == 1
    assert recorder[0]["key"].startswith(f"voiceprint_requests/{t_name.CO}/{t_name.SESSION}/")
    assert recorder[0]["company_id"] == t_name.CO


def test_day_and_session_report_requests_record_request_and_result_keys(day, recorder):
    _mp, puts = day
    res, _ = t_gen._generate_raw(day, {"deliver": "download"})
    assert res["statusCode"] == 202
    keys = [c["key"] for c in recorder]
    assert len(keys) == 2 and puts[0]["Key"] in keys
    assert json.loads(res["body"])["resultKey"] in keys
    recorder.clear()
    puts.clear()
    res, _ = t_gen._session_generate_raw(day, t_gen.S1300, {"deliver": "download"})
    assert res["statusCode"] == 202
    keys = [c["key"] for c in recorder]
    assert len(keys) == 2 and puts[0]["Key"] in keys
    assert json.loads(res["body"])["resultKey"] in keys


def test_speaker_match_records_each_run_key(monkeypatch, recorder):
    s3 = FakeS3()
    monkeypatch.setattr(org, "s3", lambda: s3)
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "shadow")
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what="media": ("Ben_UCPK2", None))
    monkeypatch.setattr(org, "_same_company_as_folder", lambda conn, c, f, w: None)
    monkeypatch.setattr(org, "_session_site_company_guard", lambda *a, **k: None)
    monkeypatch.setattr(org, "_session_turns", lambda *a, **k: [{"source_filename": "f.json", "start_sec": 1.0, "end_sec": 2.0,
                                                              "speaker_label": "spk_0", "text": "hi"}])
    monkeypatch.setattr(org, "_site_for_session", lambda *a: {"id": SITE})
    caller = dict(CALLER, global_role="pm")
    session = "ben_2026-08-12_16-50-28_sid" + "b" * 32
    res = org.speaker_match(FakeConn(), caller, session,
                            {"body": json.dumps({"user": "Ben_UCPK2"})})
    assert res["statusCode"] == 202, res
    assert [c["key"] for c in recorder] == [p["Key"] for p in s3.puts]
    assert recorder[0]["site_id"] == SITE and recorder[0]["company_id"] == COMPANY_A
