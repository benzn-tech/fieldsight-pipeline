"""Topic-window handling in src/lambda_fieldsight_api.py's media reads.

This is the LEGACY media gateway. The dashboard only reaches it on the
`timelineSource !== 'aurora'` fallback path (scripts/api/audio.js), so the
2026-08-09 prod symptom -- a topic whose Transcript tab and Audio tab
described different moments -- surfaced through lambda_org_api. The same
three defects live here untouched, and this route is one env-var flip away
from serving them:

  1. get_audio_segments (:705) anchors the base time on a TRAILING "_off",
     but a chunk-session segment carries sid/chunk tokens in between
     (…_HH-MM-SS_sid{hex}_c{NNNN}_off…). Every chunk segment is skipped and
     the Audio tab comes up empty. lambda_org_api fixed this in its copy;
     the original never got the fix.

  2. get_audio_segments (:693) and get_video_segments (:813) apply NO
     buffer to the topic window, while get_transcripts (:549) applies
     +/-60s. Aurora stores topic.time_range at MINUTE precision and
     timeline.js parseTimeRange expands "12:07 - 12:07" to
     start="12:07:00", end="12:07:00" -- ZERO WIDTH. The overlap test then
     admits only what straddles that instant, which for a recorder
     emitting 30s chunks every 28s is always the PRECEDING chunk.

  3. get_transcripts (:593) prefilters files with `file_time_sec + 600`, a
     10-minute span no chunk has, so `segments[]` escapes windowing and
     returns the whole session.

Fixture data is the real prod chunk set (Ben_UCPK2, 2026-08-09).
Style mirrors tests/unit/test_lambda_fieldsight_api_acl.py.
"""
import datetime
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")

_TS = datetime.datetime(2026, 8, 9, 12, 0, 0)

DATE = "2026-08-09"
FOLDER = "Ben_UCPK2"
SID = "sid0e43b52d7d654101aa01ee139c79831e"

ADMIN_CALLER = {
    "sub": "sub-admin-1", "email": "a@x.nz", "name": "Ada Admin",
    "role": "admin", "display_name": "Ada_Admin", "device_id": "",
    "sites": [], "managed_sites": [], "company_id": "c-1",
}

# (clock stamped in the filename, chunk length). The recorder emits a 30s
# chunk every 28s, so consecutive chunks overlap ~2s; the last is short.
CHUNKS = [("12-05-21", "30.0"), ("12-05-51", "30.0"), ("12-06-19", "30.0"),
          ("12-06-47", "30.0"), ("12-07-15", "19.0")]


def _base(clock, length):
    return f"ben_ucpk2_{DATE}_{clock}_{SID}_c0000_off0.0_to{length}_srcwav"


def _clock_of(filename):
    return filename.split(f"{DATE}_")[1].split("_")[0]


def _transcript_doc(length):
    n = 4
    step = float(length) / n
    return {"results": {
        "transcripts": [{"transcript": "word " * n}],
        "audio_segments": [{"speaker_label": "spk_0", "transcript": "word word word word",
                            "start_time": "0.0", "end_time": length}],
        "items": [{"type": "pronunciation", "start_time": str(round(i * step, 1)),
                   "end_time": str(round((i + 1) * step, 1)),
                   "alternatives": [{"content": "word"}]} for i in range(n)],
    }}


AUDIO_KEYS, TRANSCRIPT_KEYS, BODIES = [], [], {}
for _clock, _len in CHUNKS:
    _b = _base(_clock, _len)
    AUDIO_KEYS.append(f"audio_segments/{FOLDER}/{DATE}/{_b}.wav")
    _j = f"transcripts/{FOLDER}/{DATE}/{_b}.json"
    TRANSCRIPT_KEYS.append(_j)
    BODIES[_j] = json.dumps(_transcript_doc(_len)).encode()

VIDEO_KEY = f"web_video/{FOLDER}/{DATE}/Benl1_{DATE}_12-07-15.mp4"


class FakePaginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        yield {"Contents": [{"Key": k, "LastModified": _TS, "Size": 100}
                            for k in self.keys if k.startswith(prefix)]}


class FakeS3:
    def __init__(self):
        self.keys = AUDIO_KEYS + TRANSCRIPT_KEYS + [VIDEO_KEY]

    def get_paginator(self, _op):
        return FakePaginator(self.keys)

    def list_objects_v2(self, Bucket=None, Prefix=""):
        return {"Contents": [{"Key": k, "LastModified": _TS, "Size": 100}
                             for k in self.keys if k.startswith(Prefix)]}

    def get_object(self, Bucket=None, Key=None):
        return {"Body": io.BytesIO(BODIES[Key])}

    def generate_presigned_url(self, _op, Params, ExpiresIn):
        return "https://example.invalid/signed"


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(fapi, "s3_client", fake)
    return fake


def _clocks(fn, start, end, field="segments"):
    res = fn({"date": DATE, "user": FOLDER, "start": start, "end": end}, ADMIN_CALLER)
    assert res["statusCode"] == 200, res["body"]
    return sorted(_clock_of(s["filename"]) for s in json.loads(res["body"])[field])


def audio_clocks(start, end):
    return _clocks(fapi.get_audio_segments, start, end)


def transcript_clocks(start, end):
    return _clocks(fapi.get_transcripts, start, end)


# ---------------------------------------------------------------
# M-1: chunk-session segments must be found at all.
# ---------------------------------------------------------------

def test_audio_segments_matches_chunk_session_filenames(s3):
    """`…_12-05-21_sid{hex}_c0000_off0.0_to30.0_…` -- sid/chunk tokens sit
    BETWEEN the time and the _off. Anchoring on a trailing "_off" (the old
    whole-file shape) skipped every chunk segment and the Audio tab was
    empty for every chunk-session recording."""
    assert audio_clocks("", "") == sorted(c for c, _ in CHUNKS)


# ---------------------------------------------------------------
# M-2: a topic inside one minute is not a zero-width window.
# ---------------------------------------------------------------

def test_audio_segments_single_minute_topic_gets_its_own_chunk(s3):
    # timeline.js sends 12:07:00-12:07:00 for topic "12:07 - 12:07".
    assert "12-07-15" in audio_clocks("12:07:00", "12:07:00")


def test_audio_segments_first_chunk_of_a_session_is_reachable(s3):
    assert audio_clocks("12:05:00", "12:05:00") != []


def test_video_segments_single_minute_topic_window_is_not_zero_width(s3):
    """A video that STARTS inside the topic's minute fails `vid_start >
    end_sec` and the Video tab goes empty for the topic that owns it."""
    res = fapi.get_video_segments(
        {"date": DATE, "user": FOLDER, "start": "12:07:00", "end": "12:07:00"},
        ADMIN_CALLER)
    assert res["statusCode"] == 200
    assert json.loads(res["body"])["count"] == 1


# ---------------------------------------------------------------
# M-3: the two tabs of one topic must describe the same moment.
# ---------------------------------------------------------------

@pytest.mark.parametrize("start,end", [
    ("12:05:00", "12:06:00"),
    ("12:06:00", "12:06:00"),
    ("12:07:00", "12:07:00"),
])
def test_transcripts_and_audio_segments_select_the_same_chunks(s3, start, end):
    assert transcript_clocks(start, end) == audio_clocks(start, end)


def test_transcripts_window_uses_the_chunks_real_length_not_a_600s_assumption(s3):
    """A 30s chunk that ended 12:05:51 is not in a 12:07 topic."""
    assert "12-05-21" not in transcript_clocks("12:07:00", "12:07:00")
