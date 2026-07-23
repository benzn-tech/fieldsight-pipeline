"""Tests for src/lambda_keyframe.py -- video-keyframe-to-photo plan, Task 3.

subprocess is ALWAYS mocked -- the ffmpeg argv contract is asserted here;
real-ffmpeg coverage is the opt-in integration smoke at the bottom (skipif no
local ffmpeg). Doubles mirror tests/unit/test_lambda_item_writer.py's
FakeConn/FakeS3 conventions.
"""
import io
import json
import shutil
import subprocess
import types

import pytest

lk = pytest.importorskip("lambda_keyframe", reason="requires psycopg (installed in CI)")
import keyframe_selection as ks


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class _Cur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Paginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.heads = set(self.objects)     # keys head_object finds (200)
        self.downloads = []                # (key, local)
        self.uploads = []                  # (local, key, extra)
        self.deletes = []
        self.head_calls = []               # every key head_object was asked about

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _Paginator(self.objects)

    def head_object(self, Bucket, Key):
        self.head_calls.append(Key)
        if Key in self.heads:
            return {}
        raise Exception("404 Not Found")

    def download_file(self, Bucket, Key, local):
        self.downloads.append((Key, local))
        with open(local, "wb") as f:
            f.write(b"fake-video-bytes")

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        self.uploads.append((Filename, Key, ExtraArgs))
        self.objects[Key] = b"jpg"
        self.heads.add(Key)

    def delete_object(self, Bucket, Key):
        self.deletes.append(Key)
        self.objects.pop(Key, None)
        self.heads.discard(Key)


class FakeRun:
    """subprocess.run double. returncodes is an optional queue consumed one
    per call (default: always 0). On a 0 return it writes the output file
    (last argv) so the handler's os.path.exists(out) check passes."""

    def __init__(self, returncodes=None):
        self.calls = []
        self.returncodes = list(returncodes) if returncodes is not None else None

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        rc = self.returncodes.pop(0) if self.returncodes is not None else 0
        if rc == 0:
            with open(cmd[-1], "wb") as f:
                f.write(b"\xff\xd8" + b"jpgdata" * 200)
        return types.SimpleNamespace(returncode=rc, stderr="" if rc == 0 else "ffmpeg boom")


class FakeConn:
    """SELECT 1 FROM topics -> existing governs which topic ids are 'alive'
    (None = all alive). Context-manager like the real get_connection()."""

    def __init__(self, existing=None):
        self.existing = set(existing) if existing is not None else None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "SELECT 1 FROM topics" in sql:
            tid = params[0]
            alive = self.existing is None or tid in self.existing
            return _Cur({"?column?": 1} if alive else None)
        return _Cur(None)


USER = "Ben_UCPK"
DATE = "2026-07-23"
SESSION_BASE = "Benl1_2026-07-23_10-15-34"
SESS_MARKER = "_kf_s101534.jpg"
VIDEO_KEY = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-15-34.mp4"
SIDECAR_KEY = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-15-34_off0_to600_srcmp4_vad_metadata.json"
PICS = "users/Ben_UCPK/pictures/2026-07-23/"
REQUEST_KEY = "keyframe_requests/Ben_UCPK/2026-07-23/deadbeef.json"


def _sidecar(source_key=VIDEO_KEY, source_type="video", dur=600):
    return json.dumps({"source_type": source_type, "source_key": source_key,
                       "total_duration_sec": dur})


def _request(topics):
    return json.dumps({
        "user_folder": USER, "date": DATE, "session_base": SESSION_BASE,
        "extraction_key": "extractions/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-15-34.json",
        "topics": topics,
    })


@pytest.fixture
def patched(monkeypatch):
    """Default wiring: alive topics, recorded add_topic_photo_if_absent, a
    FakeRun installed on subprocess. Tests override the S3 store / conn / run
    as needed."""
    monkeypatch.setattr(lk, "S3_BUCKET", "bkt")
    monkeypatch.setattr(lk, "get_connection", lambda *a, **k: FakeConn())
    inserts = []
    monkeypatch.setattr(lk, "add_topic_photo_if_absent",
                        lambda conn, tid, key, cap: inserts.append((tid, key, cap)) or True)
    monkeypatch._kf_inserts = inserts
    return monkeypatch


# --------------------------------------------------------------------------
# Behavior 1 -- happy path (single 2-min topic, one covered frame)
# --------------------------------------------------------------------------

def test_happy_path_downloads_original_grabs_frame_uploads_and_inserts(patched):
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar()})
    patched.setattr(lk, "_s3_client", s3)
    run = FakeRun()
    patched.setattr(lk.subprocess, "run", run)

    result = lk.lambda_handler(
        {"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    # one frame at 10:16:00; seek = 10:16:00 - 10:15:34 = 26.0s into the file
    expected_key = PICS + "Benl1_2026-07-23_10-16-00_kf_s101534.jpg"
    # downloaded the ORIGINAL video, not a preview
    assert s3.downloads and s3.downloads[0][0] == VIDEO_KEY
    downloaded_local = s3.downloads[0][1]
    # ffmpeg argv asserted EXACTLY (incl. -ss before -i and the .1f seek)
    assert len(run.calls) == 1
    cmd = run.calls[0]
    uploaded_local = s3.uploads[0][0]
    assert cmd == ks.ffmpeg_frame_cmd(lk.FFMPEG_PATH, downloaded_local, 26.0, uploaded_local)
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "26.0"
    # uploaded to the pictures prefix with image/jpeg
    assert s3.uploads[0][1] == expected_key
    assert s3.uploads[0][2] == {"ContentType": "image/jpeg"}
    # DB bind with the mid-topic caption
    assert patched._kf_inserts == [("topic-1", expected_key, "Auto keyframe @ 10:16")]
    assert result == {"results": [{"skipped": False, "keyframes": 1}]}


def test_multiple_frames_per_long_topic(patched):
    # 10:00 - 10:06 -> 3 frames at 10:01:00 / 10:03:00 / 10:05:00.
    sidecar = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-00-00_vad_metadata.json"
    video = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-00-00.mp4"
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:00 – 10:06"}]),
                 sidecar: _sidecar(source_key=video, dur=600)})
    patched.setattr(lk, "_s3_client", s3)
    run = FakeRun()
    patched.setattr(lk.subprocess, "run", run)

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert len(run.calls) == 3
    keys = sorted(k for _, k, _ in s3.uploads)
    assert keys == [
        PICS + "Benl1_2026-07-23_10-01-00_kf_s101534.jpg",
        PICS + "Benl1_2026-07-23_10-03-00_kf_s101534.jpg",
        PICS + "Benl1_2026-07-23_10-05-00_kf_s101534.jpg",
    ]
    assert result == {"results": [{"skipped": False, "keyframes": 3}]}


# --------------------------------------------------------------------------
# Behavior 2 -- audio-only day (no video sidecars) -> total no-op
# --------------------------------------------------------------------------

def test_audio_only_day_is_a_noop(patched):
    audio_sidecar = ("audio_segments/Ben_UCPK/2026-07-23/"
                     "Benl1_2026-07-23_10-15-34_vad_metadata.json")
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:20"}]),
                 audio_sidecar: _sidecar(source_type="audio")})
    patched.setattr(lk, "_s3_client", s3)
    run = FakeRun()
    patched.setattr(lk.subprocess, "run", run)

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert s3.downloads == [] and run.calls == [] and s3.uploads == []
    assert patched._kf_inserts == []
    assert result == {"results": [{"skipped": True, "reason": "no video coverage"}]}


# --------------------------------------------------------------------------
# Behavior 3 -- jpeg already exists -> no download/ffmpeg, DB insert still attempted
# --------------------------------------------------------------------------

def test_existing_jpeg_skips_ffmpeg_but_still_binds(patched):
    existing = PICS + "Benl1_2026-07-23_10-16-00_kf_s101534.jpg"
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar(),
                 existing: b"already-here"})
    patched.setattr(lk, "_s3_client", s3)
    run = FakeRun()
    patched.setattr(lk.subprocess, "run", run)

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert s3.downloads == [] and run.calls == []   # head_object 200 -> fast path
    assert patched._kf_inserts == [("topic-1", existing, "Auto keyframe @ 10:16")]
    assert result == {"results": [{"skipped": False, "keyframes": 1}]}


# --------------------------------------------------------------------------
# Behavior 4 -- topic id gone (SELECT 1 miss) -> skip, no insert, no raise
# --------------------------------------------------------------------------

def test_superseded_topic_is_skipped(patched):
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "gone", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar()})
    patched.setattr(lk, "_s3_client", s3)
    patched.setattr(lk, "get_connection", lambda *a, **k: FakeConn(existing=set()))
    run = FakeRun()
    patched.setattr(lk.subprocess, "run", run)

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert run.calls == [] and s3.uploads == []
    assert patched._kf_inserts == []
    assert result == {"results": [{"skipped": False, "keyframes": 0}]}


# --------------------------------------------------------------------------
# Behavior 5 -- ffmpeg non-zero -> warn+skip that frame, other topics proceed,
#               lambda does NOT raise
# --------------------------------------------------------------------------

def test_ffmpeg_failure_skips_topic_and_continues(patched):
    topics = [
        {"topic_id": "topic-1", "time_range": "10:15 – 10:17"},   # frame 10:16:00 -> FAILS
        {"topic_id": "topic-2", "time_range": "10:17 – 10:19"},   # frame 10:18:00 -> OK
    ]
    s3 = FakeS3({REQUEST_KEY: _request(topics), SIDECAR_KEY: _sidecar()})
    patched.setattr(lk, "_s3_client", s3)
    run = FakeRun(returncodes=[1, 0])   # first grab fails, second succeeds
    patched.setattr(lk.subprocess, "run", run)

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert len(run.calls) == 2   # both topics attempted; the failure did not abort the loop
    ok_key = PICS + "Benl1_2026-07-23_10-18-00_kf_s101534.jpg"
    assert patched._kf_inserts == [("topic-2", ok_key, "Auto keyframe @ 10:18")]
    assert result == {"results": [{"skipped": False, "keyframes": 1}]}   # no raise


# --------------------------------------------------------------------------
# Behavior 6 -- stale cleanup: this session's non-expected keyframes are
#               deleted; other sessions' keyframes and normal photos survive
# --------------------------------------------------------------------------

def test_stale_keyframe_cleanup(patched):
    stale = PICS + "Benl1_2026-07-23_10-30-00_kf_s101534.jpg"      # this session, not expected
    other = PICS + "Benl1_2026-07-23_10-30-00_kf_s090000.jpg"      # a DIFFERENT session
    normal = PICS + "Benl1_2026-07-23_10-16-00.jpg"                 # a real user photo
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar(),
                 stale: b"x", other: b"x", normal: b"x"})
    patched.setattr(lk, "_s3_client", s3)
    patched.setattr(lk.subprocess, "run", FakeRun())

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert s3.deletes == [stale]           # only the stale same-session keyframe
    assert other not in s3.deletes and normal not in s3.deletes


# --------------------------------------------------------------------------
# Behavior 6b (M-2) -- stale cleanup also deletes the dangling topic_photos
#   rows, in the SAME connection/transaction as the S3 object deletes
# --------------------------------------------------------------------------

def test_stale_cleanup_also_deletes_topic_photos_rows(patched):
    stale = PICS + "Benl1_2026-07-23_10-30-00_kf_s101534.jpg"   # this session, not expected
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar(),
                 stale: b"x"})
    patched.setattr(lk, "_s3_client", s3)
    conn = FakeConn()
    patched.setattr(lk, "get_connection", lambda *a, **k: conn)
    patched.setattr(lk.subprocess, "run", FakeRun())

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    # S3 object deleted AND its topic_photos row deleted by the same conn
    assert s3.deletes == [stale]
    row_deletes = [(sql, params) for sql, params in conn.executed
                   if "DELETE FROM topic_photos" in sql]
    assert len(row_deletes) == 1
    assert stale in row_deletes[0][1][0]     # deleted key passed to WHERE s3_key = ANY(...)


def test_no_stale_files_means_no_topic_photos_delete(patched):
    # Nothing stale -> the DELETE FROM topic_photos is not issued at all.
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar()})
    patched.setattr(lk, "_s3_client", s3)
    conn = FakeConn()
    patched.setattr(lk, "get_connection", lambda *a, **k: conn)
    patched.setattr(lk.subprocess, "run", FakeRun())

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert s3.deletes == []
    assert not [1 for sql, _ in conn.executed if "DELETE FROM topic_photos" in sql]


# --------------------------------------------------------------------------
# M-3 -- one corrupt _vad_metadata.json sidecar among good ones is skipped
#   (warn-and-continue), never raised: the good recording still loads
# --------------------------------------------------------------------------

def test_corrupt_sidecar_is_skipped_not_fatal():
    good_video = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-15-34.mp4"
    good = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-15-34_vad_metadata.json"
    corrupt = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_11-00-00_vad_metadata.json"
    s3 = FakeS3({good: _sidecar(source_key=good_video, dur=600),
                 corrupt: '{"source_type": "video", "sou'})   # truncated JSON

    recs = lk.load_video_recordings(s3, "bkt", "Ben_UCPK", "2026-07-23")

    assert len(recs) == 1
    assert recs[0]["source_key"] == good_video


def test_sidecar_missing_source_key_is_skipped_not_fatal():
    good_video = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-15-34.mp4"
    good = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-15-34_vad_metadata.json"
    # valid JSON, source_type video, but no source_key -> KeyError, must skip
    bad = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_11-00-00_vad_metadata.json"
    s3 = FakeS3({good: _sidecar(source_key=good_video, dur=600),
                 bad: json.dumps({"source_type": "video", "total_duration_sec": 120})})

    recs = lk.load_video_recordings(s3, "bkt", "Ben_UCPK", "2026-07-23")

    assert [r["source_key"] for r in recs] == [good_video]


# --------------------------------------------------------------------------
# M-1 -- the HeadObject existence fast-path is actually exercised (its key
#   needs s3:GetObject on users/*/pictures/*_kf_s*.jpg, else it 403s silently)
# --------------------------------------------------------------------------

def test_head_object_fast_path_is_exercised(patched):
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar()})
    patched.setattr(lk, "_s3_client", s3)
    patched.setattr(lk.subprocess, "run", FakeRun())

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    expected_key = PICS + "Benl1_2026-07-23_10-16-00_kf_s101534.jpg"
    assert expected_key in s3.head_calls


def test_original_downloaded_once_per_source_key(patched):
    # M-4: a 3-frame topic seeks one downloaded original, not three downloads.
    sidecar = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-00-00_vad_metadata.json"
    video = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-00-00.mp4"
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:00 – 10:06"}]),
                 sidecar: _sidecar(source_key=video, dur=600)})
    patched.setattr(lk, "_s3_client", s3)
    run = FakeRun()
    patched.setattr(lk.subprocess, "run", run)

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert len(run.calls) == 3                       # 3 frames decoded
    assert [k for k, _ in s3.downloads] == [video]   # but the original pulled ONCE


# --------------------------------------------------------------------------
# Behavior 7 -- duration source: recordings from *_vad_metadata.json sidecars,
#               source_type=='video' only, base_s via transcript_utils
# --------------------------------------------------------------------------

def test_load_video_recordings_filters_to_video_sidecars():
    video_key = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-15-34.mp4"
    s3 = FakeS3({
        "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-15-34_vad_metadata.json":
            _sidecar(source_key=video_key, source_type="video", dur=600),
        "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_11-00-00_vad_metadata.json":
            _sidecar(source_key="users/Ben_UCPK/audio/2026-07-23/x.wav",
                     source_type="audio", dur=120),
        # a non-metadata object under the same prefix must be ignored
        "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-15-34_off0_to10_srcmp4.wav": b"x",
    })

    recs = lk.load_video_recordings(s3, "bkt", "Ben_UCPK", "2026-07-23")

    assert len(recs) == 1
    rec = recs[0]
    assert rec["source_key"] == video_key
    assert rec["base_s"] == 10 * 3600 + 15 * 60 + 34   # 10:15:34 via extract_base_time_from_filename
    assert rec["duration_s"] == 600.0


# --------------------------------------------------------------------------
# add_topic_photo_if_absent (repositories/topics.py) -- NOT-EXISTS idempotency
# --------------------------------------------------------------------------

def test_add_topic_photo_if_absent_inserts_once_then_dedupes():
    from repositories.topics import add_topic_photo_if_absent

    import contextlib

    class PhotoConn:
        def __init__(self):
            self.rows = set()

        def transaction(self):
            # L-1: add_topic_photo_if_absent now wraps the insert in a SAVEPOINT
            # (conn.transaction()); a no-op CM is enough for the happy path.
            return contextlib.nullcontext()

        def execute(self, sql, params):
            pair = (params["tid"], params["key"])
            if pair in self.rows:
                return _Cur(None)          # NOT EXISTS is false -> zero rows
            self.rows.add(pair)
            return _Cur({"id": "new-photo-id"})

    conn = PhotoConn()
    assert add_topic_photo_if_absent(conn, "t1", "users/k.jpg", "cap") is True
    # duplicate (topic_id, s3_key) -> no second row
    assert add_topic_photo_if_absent(conn, "t1", "users/k.jpg", "cap") is False
    # a different key for the same topic inserts
    assert add_topic_photo_if_absent(conn, "t1", "users/other.jpg", "cap") is True


def test_add_topic_photo_if_absent_skips_fk_violation_without_raising():
    # L-1: the topic cascaded away between the caller's SELECT 1 and this
    # insert -> the FK violation is caught inside the SAVEPOINT and the row is
    # skipped (return False), never re-raised to abort the whole request.
    import contextlib
    import psycopg

    class FKConn:
        def transaction(self):
            return contextlib.nullcontext()

        def execute(self, sql, params):
            raise psycopg.errors.ForeignKeyViolation("topic gone")

    from repositories.topics import add_topic_photo_if_absent
    assert add_topic_photo_if_absent(FKConn(), "t-gone", "users/k.jpg", "cap") is False


# --------------------------------------------------------------------------
# Opt-in real-ffmpeg smoke (no binary fixture committed -- generate one)
# --------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="local ffmpeg required")
def test_real_ffmpeg_extracts_one_frame(tmp_path):
    src = tmp_path / "t.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", "testsrc=duration=4:size=320x240:rate=10", str(src)],
                   check=True, capture_output=True)
    out = tmp_path / "f.jpg"
    subprocess.run(ks.ffmpeg_frame_cmd("ffmpeg", str(src), 2.0, str(out)),
                   check=True, capture_output=True)
    assert out.exists() and out.stat().st_size > 1000
