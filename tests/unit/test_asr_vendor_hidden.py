"""Customer-facing ASR surfaces must not name the speech-to-text vendor either
-- same rule as the LLM sweep (test_reports_name_the_model_that_wrote_them.py,
test_ask_reports_the_model_that_answered.py), applied to the transcript/
audio/video endpoints. Owner's ruling, 2026-09-20: "a customer must not be
able to tell which speech-to-text vendor we use either, not from a response
body and not from the shape of one."

Frontend contract (survey, read-only, in fieldsight-ui):
  scripts/composites/transcript-list.js   -- speaker_segments[].speaker,
    .text, .speaker_name, .speaker_state, .time_label, .start, .end,
    .source_filename, .chunk_start, .duration; top-level speakers, count,
    speaker_count, total_speaker_segments, unmatchedNames, message
  scripts/composites/audio-playlist.js    -- segments[].filename, .time_label,
    .duration, .url; top-level segments, count
  scripts/composites/video-player.js      -- videos[].is_preview, .url,
    .offset_sec, .time_label, .filename, .size_mb; top-level videos, count
None of those field NAMES are vendor-identifying, and this file does not
touch any of them. What it pins is the two-sided rule: those fields must
stay, and nothing else may appear.

The real risk (survey finding, not named in the ticket): raw AWS Transcribe
output is {"jobName", "accountId", "status", "results": {...}} -- and
`elevenlabs_utils.adapt_to_transcribe_json` deliberately reshapes ElevenLabs'
response into ONLY `{"results": {...}}`, omitting those three keys, so the
stored artifact's own TOP-LEVEL KEYS differ by vendor even with no string
naming one. `accountId` is also our real AWS account number, not just a
vendor tell. Every reshaping function below is already an allowlist (it
builds a new dict field by field rather than passing the S3 object through),
so today none of jobName/accountId/status leaks -- these tests make that a
pinned property instead of an accident, using a fixture that actually
carries those keys (the existing fixtures in test_lambda_org_api.py do not,
so they could not have caught a regression here).

A second, separate leak did exist and is fixed by this same commit: both
`/media/presigned-url` endpoints allowed the `transcripts/` prefix, which
handed back a link straight to the raw S3 object -- the ONE way the vendor's
shape could reach a customer's browser byte for byte, bypassing every
allowlist below. See test_lambda_org_api.py::
test_media_presign_transcripts_prefix_denied and
test_lambda_fieldsight_api_acl.py::
test_presign_transcripts_prefix_denied_even_for_own_recording.
"""
import io
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")

# Self-contained fixtures/helpers below (not imported from test_lambda_org_api.py
# / test_lambda_fieldsight_api_acl.py: pytest's per-file import isolation in
# this repo does not make one test module importable from another).

CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-04",
}

WORKER_CALLER = {
    "sub": "sub-worker-1", "email": "w@x.nz", "name": "Ben Test",
    "role": "worker", "display_name": "Ben Test", "device_id": "Benl1",
    "sites": ["s-1"], "managed_sites": [], "company_id": "c-1",
}


def make_event(method, path, sub="sub-1", body=None, params=None):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": params,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}},
    }


def body_of(res):
    return json.loads(res["body"])


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def transaction(self):
        return self

    def execute(self, *a, **k):
        class _Cur:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None
        return _Cur()


class _FakeS3Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        for p in self.pages:
            yield p


class FakeOrgS3:
    """Minimal double for org._s3_client -- only what these tests exercise."""

    def __init__(self):
        self.objects = {}
        self.list_objects_response = {"Contents": []}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakeS3Paginator([self.list_objects_response])

    def get_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        return "https://s3.example/" + Params["Key"]


class FakeLegacyS3:
    """Minimal double for lambda_fieldsight_api.s3_client."""

    def __init__(self):
        self.keys = []
        self.objects = {}

    def get_paginator(self, op):
        keys = self.keys

        class _P:
            def paginate(self, **kwargs):
                prefix = kwargs.get("Prefix", "")
                yield {"Contents": [{"Key": k} for k in keys if k.startswith(prefix)]}
        return _P()

    def list_objects_v2(self, Bucket=None, Prefix=""):
        return {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}

    def get_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        return "https://s3.example/" + Params["Key"]

    def head_object(self, Bucket=None, Key=None):
        raise KeyError(Key)


def _wire_org(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org.redactions, "list_active_for_topics", lambda conn, ids: {})
    monkeypatch.setattr(org.users, "get_by_folder_name",
                        lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    fake = FakeOrgS3()
    monkeypatch.setattr(org, "_s3_client", fake)
    return fake


def _wire_legacy(monkeypatch):
    fake = FakeLegacyS3()
    monkeypatch.setattr(fapi, "s3_client", fake)
    monkeypatch.setattr(fapi, "load_user_mapping", lambda: {
        "mapping": {"Benl1": {"name": "Ben Test", "role": "worker", "sites": ["s-1"]}},
        "sites": {"s-1": {"name": "Site One"}},
    })
    return fake

# The real 355-byte AWS Transcribe artifact shape (mirrors
# test_empty_vs_unreadable_transcript.py's REAL_EMPTY fixture), with content
# this time instead of an empty transcript. `accountId` is deliberately the
# real prod account number, so a test that forgets to check for it would
# still look like it was checking something.
_AWS_SHAPED_TRANSCRIPT = {
    "jobName": "fieldsight_Ben_UCPK_2026-09-20_08-00-00",
    "accountId": "509194952652",
    "status": "COMPLETED",
    "results": {
        "language_code": "en-US",
        "transcripts": [{"transcript": "Hello there. All good on site."}],
        "audio_segments": [
            {"speaker_label": "spk_0", "transcript": "Hello there.",
             "start_time": "0.0", "end_time": "2.0"},
            {"speaker_label": "spk_1", "transcript": "All good on site.",
             "start_time": "2.5", "end_time": "5.0"},
        ],
        "items": [
            {"type": "pronunciation", "start_time": "0.0", "end_time": "0.5",
             "alternatives": [{"content": "Hello"}]},
        ],
    },
}

# Strings that must never appear ANYWHERE in a customer-facing response body,
# whether as a key or a value -- checked against the serialized JSON so a
# nested or renamed occurrence cannot slip past a keys-only check.
_VENDOR_TELLS = ["jobName", "accountId", "509194952652", "elevenlabs",
                 "ElevenLabs", "scribe_v2", "provider"]


def _assert_no_vendor_tells(body):
    blob = json.dumps(body)
    for tell in _VENDOR_TELLS:
        assert tell not in blob, "%r leaked into a customer-facing response: %s" % (tell, blob)


# ------------------------------------------------------- org-api /transcripts

def test_org_transcripts_response_carries_no_asr_vendor_tell(monkeypatch):
    fake = _wire_org(monkeypatch)
    key = "transcripts/Ben_UCPK/2026-09-20/Ben_UCPK_2026-09-20_08-00-00.json"
    fake.list_objects_response = {"Contents": [{"Key": key}]}
    fake.objects[key] = json.dumps(_AWS_SHAPED_TRANSCRIPT).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/transcripts",
        params={"date": "2026-09-20", "user": "Ben_UCPK"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert body["speaker_segments"], "the fixture must actually produce turns"
    _assert_no_vendor_tells(body)
    # The frontend contract (transcript-list.js) still holds.
    seg = body["speaker_segments"][0]
    for field in ("speaker", "text", "time_label", "start", "end",
                  "source_filename", "chunk_start", "duration"):
        assert field in seg, "transcript-list.js reads %r" % field
    for field in ("speakers", "count", "speaker_count", "total_speaker_segments"):
        assert field in body, "transcript-list.js reads %r" % field


def test_legacy_transcripts_response_carries_no_asr_vendor_tell(monkeypatch):
    fake = _wire_legacy(monkeypatch)
    key = "transcripts/Ben_Test/2026-09-20/Ben_Test_2026-09-20_08-00-00.json"
    fake.keys = [key]
    fake.objects = {key: json.dumps(_AWS_SHAPED_TRANSCRIPT).encode()}
    res = fapi.get_transcripts(
        {"date": "2026-09-20", "user": "Ben Test"}, WORKER_CALLER)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert body["speaker_segments"], "the fixture must actually produce turns"
    _assert_no_vendor_tells(body)
    seg = body["speaker_segments"][0]
    for field in ("speaker", "text", "time_label", "start", "end"):
        assert field in seg, "transcript-list.js reads %r" % field
    for field in ("speakers", "count", "speaker_count", "total_speaker_segments"):
        assert field in body, "transcript-list.js reads %r" % field


# ---------------------------------------------------- audio/video: field contract
#
# These endpoints never touched the raw provider payload (they derive
# everything from filenames + wav/mp4 bytes), so there is no vendor-tell
# fixture to build -- the risk here is purely "did the allowlist stay
# complete", i.e. the frontend contract from audio-playlist.js /
# video-player.js.

def test_org_audio_segments_field_contract(monkeypatch):
    fake = _wire_org(monkeypatch)
    key = "audio_segments/Ben_UCPK/2026-09-20/Benl1_2026-09-20_08-00-00_off60.0_to120.0_srcwav.wav"
    fake.list_objects_response = {"Contents": [{"Key": key}]}
    fake.objects[key] = b""
    res = org.lambda_handler(make_event(
        "GET", "/api/org/audio-segments",
        params={"date": "2026-09-20", "user": "Ben_UCPK",
                "start": "08:00:00", "end": "08:03:00"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert body["segments"], "the fixture must actually produce a segment"
    seg = body["segments"][0]
    # scripts/composites/audio-playlist.js reads exactly these.
    for field in ("filename", "time_label", "duration", "url"):
        assert field in seg, "audio-playlist.js reads %r" % field
    _assert_no_vendor_tells(body)


def test_org_video_segments_field_contract(monkeypatch):
    fake = _wire_org(monkeypatch)
    key = "web_video/Ben_UCPK/2026-09-20/Benl1_2026-09-20_08-00-00.mp4"
    fake.list_objects_response = {"Contents": [{"Key": key, "Size": 1048576}]}
    res = org.lambda_handler(make_event(
        "GET", "/api/org/video-segments",
        params={"date": "2026-09-20", "user": "Ben_UCPK",
                "start": "08:01:00", "end": "08:05:00"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert body["videos"], "the fixture must actually produce a video"
    v = body["videos"][0]
    # scripts/composites/video-player.js reads exactly these.
    for field in ("is_preview", "url", "offset_sec", "time_label", "filename", "size_mb"):
        assert field in v, "video-player.js reads %r" % field
    _assert_no_vendor_tells(body)
