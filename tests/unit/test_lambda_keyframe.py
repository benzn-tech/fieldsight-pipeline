"""Tests for src/lambda_keyframe.py -- video-keyframe-to-photo plan, Task 3.

subprocess is ALWAYS mocked -- the ffmpeg argv contract is asserted here;
real-ffmpeg coverage is the opt-in integration smoke at the bottom (skipif no
local ffmpeg). Doubles mirror tests/unit/test_lambda_item_writer.py's
FakeConn/FakeS3 conventions.
"""
import contextlib
import io
import json
import re
import shutil
import subprocess
import types

import pytest

lk = pytest.importorskip("lambda_keyframe", reason="requires psycopg (installed in CI)")
import keyframe_selection as ks
import psycopg      # lambda_keyframe imported cleanly, so psycopg is present


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class _Cur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


# The structural signal the widened topic pre-check now fetches (Q7). The
# generator reads category/work_class/site_id/company_id from here; duration_min/
# frame_index/n_frames come from the plan tuple, not this row.
DEFAULT_TOPIC_META = {"time_range": "10:15 – 10:17", "category": "safety",
                      "work_class": "work", "site_id": "s-1", "company_id": "co-1"}


class _TopicCursor:
    """Serves the widened `SELECT ... FROM topics t LEFT JOIN sites ...` pre-check
    that now runs through conn.cursor(row_factory=dict_row). Honours the conn's
    `existing` set (None -> alive) so superseded topics still return None."""

    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        tid = params[0]
        alive = self.conn.existing is None or tid in self.conn.existing
        self._row = dict(self.conn.topic_meta) if alive else None
        return self

    def fetchone(self):
        return self._row


class _Paginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


class FakeS3:
    def __init__(self, objects=None, missing=None, download_error=None):
        self.objects = dict(objects or {})
        self.heads = set(self.objects)     # keys head_object finds (200)
        self.downloads = []                # (key, local) -- successful pulls only
        self.download_attempts = []        # every download_file(key) call (incl. failures)
        self.uploads = []                  # (local, key, extra)
        self.deletes = []
        self.head_calls = []               # every key head_object was asked about
        self.missing = set(missing or [])  # keys whose download_file 404s
        self.download_error = download_error  # exception to raise for a missing key

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
        self.download_attempts.append(Key)
        if Key in self.missing:
            raise (self.download_error or Exception("404 Not Found (video deleted)"))
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
    """The topic pre-check (via cursor) -> `existing` governs which topic ids are
    'alive' (None = all alive). Context-manager like the real get_connection()."""

    def __init__(self, existing=None, topic_meta=None):
        self.existing = set(existing) if existing is not None else None
        self.topic_meta = topic_meta or DEFAULT_TOPIC_META
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        # Telemetry writes are savepoint-wrapped (Q7); a no-op CM is enough for
        # the paths that don't model abort semantics -- see TxAbortConn for those.
        return contextlib.nullcontext()

    def cursor(self, *a, **k):
        return _TopicCursor(self)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _Cur(None)


class _Savepoint:
    """psycopg3 `conn.transaction()` semantics, faithfully: entering issues a
    SAVEPOINT, which FAILS on an already-aborted transaction; leaving via an
    exception rolls back to the savepoint, which CLEARS the aborted state (that
    is precisely what keeps the connection usable)."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        if self.conn.failed:
            raise psycopg.errors.InFailedSqlTransaction(
                "current transaction is aborted, commands ignored until "
                "end of transaction block")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.failed = False        # ROLLBACK TO SAVEPOINT un-poisons it
        return False                        # never suppress


class TxAbortConn:
    """Models a real aborted transaction: once a statement fails, `failed` is set
    and EVERY later statement (or a bare SAVEPOINT) raises
    InFailedSqlTransaction until something rolls back. Uses the REAL
    add_topic_photo_if_absent so its `with conn.transaction():` is genuinely
    exercised against this state machine."""

    def __init__(self, existing=None, topic_meta=None):
        self.existing = set(existing) if existing is not None else None
        self.topic_meta = topic_meta or DEFAULT_TOPIC_META
        self.failed = False
        self.executed = []
        self.rows = set()          # committed (topic_id, s3_key) binds

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return _Savepoint(self)

    def _guard(self):
        if self.failed:
            raise psycopg.errors.InFailedSqlTransaction(
                "current transaction is aborted, commands ignored until "
                "end of transaction block")

    def cursor(self, *a, **k):
        self._guard()
        return _TopicCursor(self)

    def execute(self, sql, params=None):
        self._guard()
        self.executed.append((sql, params))
        if "INSERT INTO topic_photos" in sql:
            pair = (params["tid"], params["key"])
            if pair in self.rows:
                return _Cur(None)
            self.rows.add(pair)
            return _Cur({"id": "new-photo-id"})
        return _Cur(None)


def _like_match(key, pattern):
    """Minimal SQL `LIKE ... ESCAPE '\\'` evaluator so a test can prove the M-2
    row-cleanup predicate matches (or spares) a given s3_key the same way
    Postgres would."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            out.append(re.escape(pattern[i + 1])); i += 2
        elif c == "%":
            out.append(".*"); i += 1
        elif c == "_":
            out.append("."); i += 1
        else:
            out.append(re.escape(c)); i += 1
    return re.fullmatch("".join(out), key) is not None


class StatefulConn:
    """Context-manager conn that actually stores topic_photos s3_keys and applies
    the M-2 DELETE predicate (prefix LIKE + marker LIKE + <> ALL(expected)), so a
    test can prove the row cleanup self-heals from the DB side -- independent of
    what the S3 listing returned this run."""

    def __init__(self, rows=(), existing=None, topic_meta=None):
        self.rows = set(rows)
        self.existing = set(existing) if existing is not None else None
        self.topic_meta = topic_meta or DEFAULT_TOPIC_META
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return contextlib.nullcontext()

    def cursor(self, *a, **k):
        return _TopicCursor(self)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "DELETE FROM topic_photos" in sql:
            prefix_like, marker_like, expected = params
            self.rows = {
                k for k in self.rows
                if not (_like_match(k, prefix_like)
                        and _like_match(k, marker_like)
                        and k not in expected)
            }
            return _Cur(None)
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
    # Q7: default the tombstone check to a no-op (nothing tombstoned) and record
    # 'generated' events so existing behavior tests stay green and new ones can
    # assert on them. Tests override tombstoned_subset to exercise the skip.
    events = []
    monkeypatch.setattr(lk.keyframes, "tombstoned_subset", lambda conn, keys: set())
    monkeypatch.setattr(lk.keyframes, "record_event",
                        lambda conn, event, **kw: events.append((event, kw)) or {"id": "e"})
    monkeypatch._kf_events = events
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
    stale = PICS + "Benl1_2026-07-23_10-30-00_kf_s101534.jpg"       # this session, not expected
    expected_key = PICS + "Benl1_2026-07-23_10-16-00_kf_s101534.jpg"  # this run's frame
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar(),
                 stale: b"x"})
    patched.setattr(lk, "_s3_client", s3)
    conn = StatefulConn(rows={stale, expected_key})
    patched.setattr(lk, "get_connection", lambda *a, **k: conn)
    patched.setattr(lk.subprocess, "run", FakeRun())

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    # S3 object deleted AND its dangling topic_photos row removed by the DB
    # predicate; the row for THIS run's expected keyframe survives.
    assert s3.deletes == [stale]
    assert stale not in conn.rows
    assert expected_key in conn.rows
    row_deletes = [(sql, p) for sql, p in conn.executed if "DELETE FROM topic_photos" in sql]
    assert len(row_deletes) == 1
    # M-2: the predicate is derived from (prefix, marker, expected) -- NOT from
    # the keys the S3 listing returned this run.
    prefix_like, marker_like, expected = row_deletes[0][1]
    assert "%" in prefix_like and marker_like.startswith("%")
    assert expected_key in expected and stale not in expected


def test_row_cleanup_preserves_expected_row_when_nothing_stale(patched):
    # New M-2 shape: the topic_photos DELETE runs unconditionally (self-heal),
    # but its `<> ALL(expected)` guard means THIS run's expected keyframe row is
    # never removed when there is nothing stale.
    expected_key = PICS + "Benl1_2026-07-23_10-16-00_kf_s101534.jpg"
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar()})
    patched.setattr(lk, "_s3_client", s3)
    conn = StatefulConn(rows={expected_key})
    patched.setattr(lk, "get_connection", lambda *a, **k: conn)
    patched.setattr(lk.subprocess, "run", FakeRun())

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert s3.deletes == []
    assert expected_key in conn.rows            # expected row survived
    assert any("DELETE FROM topic_photos" in sql for sql, _ in conn.executed)  # still issued


def test_m2_retry_self_heals_dangling_row_after_s3_already_deleted(patched):
    # Simulated crash: a PRIOR run deleted the stale keyframe's S3 object but
    # died before COMMIT, so the topic_photos row survived while the S3 key is
    # gone. On retry the S3 listing can't rediscover the key (nothing to delete),
    # yet the DB-side predicate still removes the dangling row -- self-heal.
    dangling = PICS + "Benl1_2026-07-23_10-30-00_kf_s101534.jpg"   # S3 object already gone
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar()})                          # note: no `dangling` object
    patched.setattr(lk, "_s3_client", s3)
    conn = StatefulConn(rows={dangling})
    patched.setattr(lk, "get_connection", lambda *a, **k: conn)
    patched.setattr(lk.subprocess, "run", FakeRun())

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert s3.deletes == []            # S3 listing found nothing (object already gone)
    assert dangling not in conn.rows   # ...but the dangling row was still removed


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
# NEW-2 -- a dead/deleted original (download 404) must skip only THAT frame|
#   topic; other topics in the same request still bind, handler never raises
# --------------------------------------------------------------------------

def test_missing_original_skips_its_topic_but_others_proceed(patched):
    video1 = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-15-34.mp4"   # DELETED
    sidecar1 = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-15-34_vad_metadata.json"
    video2 = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_11-00-00.mp4"   # healthy
    sidecar2 = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_11-00-00_vad_metadata.json"
    topics = [
        {"topic_id": "topic-1", "time_range": "10:15 – 10:17"},   # covered by video1 (gone)
        {"topic_id": "topic-2", "time_range": "11:00 – 11:02"},   # covered by video2 (ok)
    ]
    s3 = FakeS3({REQUEST_KEY: _request(topics),
                 sidecar1: _sidecar(source_key=video1, dur=600),
                 sidecar2: _sidecar(source_key=video2, dur=600)},
                missing={video1})
    patched.setattr(lk, "_s3_client", s3)
    patched.setattr(lk.subprocess, "run", FakeRun())

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    # dead video1 was attempted and skipped; healthy video2 still bound -- no raise
    assert video1 in s3.download_attempts
    assert [k for k, _ in s3.downloads] == [video2]     # only the healthy pull succeeded
    ok_key = PICS + "Benl1_2026-07-23_11-01-00_kf_s101534.jpg"
    assert patched._kf_inserts == [("topic-2", ok_key, "Auto keyframe @ 11:01")]
    assert result == {"results": [{"skipped": False, "keyframes": 1}]}


def test_missing_original_sibling_frames_do_not_retry_download(patched):
    # NEW-2: all frames of one dead video share a source_key -> the download is
    # attempted ONCE; the cached failure short-circuits the siblings. No raise.
    video = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-00-00.mp4"
    sidecar = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-00-00_vad_metadata.json"
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:00 – 10:06"}]),
                 sidecar: _sidecar(source_key=video, dur=600)},
                missing={video})
    patched.setattr(lk, "_s3_client", s3)
    patched.setattr(lk.subprocess, "run", FakeRun())

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert s3.download_attempts == [video]   # 3 planned frames, ONE download attempt
    assert s3.downloads == []
    assert patched._kf_inserts == []
    assert result == {"results": [{"skipped": False, "keyframes": 0}]}   # no raise


# --------------------------------------------------------------------------
# NEW-3 -- a full ephemeral disk (download OSError) travels the same skip+warn
#   path, never crashing the request
# --------------------------------------------------------------------------

def test_disk_full_oserror_skips_frame_without_raising(patched):
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar()},
                missing={VIDEO_KEY},
                download_error=OSError(28, "No space left on device"))
    patched.setattr(lk, "_s3_client", s3)
    patched.setattr(lk.subprocess, "run", FakeRun())

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert s3.downloads == [] and patched._kf_inserts == []
    assert result == {"results": [{"skipped": False, "keyframes": 0}]}   # no raise


def test_downloaded_original_is_evicted_after_its_last_frame(patched):
    # NEW-3: the local original is os.remove'd once its last planned frame is
    # processed, so many source_keys (~200 MB each) don't pile up on the 2048 MB
    # ephemeral disk before the tmp_dir finally sweeps.
    video = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-00-00.mp4"
    sidecar = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-00-00_vad_metadata.json"
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:00 – 10:06"}]),
                 sidecar: _sidecar(source_key=video, dur=600)})
    patched.setattr(lk, "_s3_client", s3)
    patched.setattr(lk.subprocess, "run", FakeRun())
    removed = []
    real_remove = lk.os.remove
    patched.setattr(lk.os, "remove", lambda p: removed.append(p) or real_remove(p))

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert [k for k, _ in s3.downloads] == [video]   # pulled once
    assert len(removed) == 1                          # ...and evicted once, after 3 frames


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
# Q7 -- tombstoned key is skipped ENTIRELY (never head/download/ffmpeg/insert)
#   and, being excluded from `expected`, is garbage-collected by the stale S3
#   sweep + the M-2 DB row cleanup.
# --------------------------------------------------------------------------

def test_tombstoned_frame_is_skipped_entirely(patched):
    kf_key = PICS + "Benl1_2026-07-23_10-16-00_kf_s101534.jpg"
    # a lingering S3 object AND a dangling topic_photos row for the tombstoned key
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar(),
                 kf_key: b"stale-tombstoned-jpg"})
    patched.setattr(lk, "_s3_client", s3)
    conn = StatefulConn(rows={kf_key})
    patched.setattr(lk, "get_connection", lambda *a, **k: conn)
    patched.setattr(lk.keyframes, "tombstoned_subset", lambda conn, keys: {kf_key})
    run = FakeRun()
    patched.setattr(lk.subprocess, "run", run)

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    # never planned: no head, no download, no ffmpeg, no DB bind, no 'generated' event
    assert kf_key not in s3.head_calls
    assert s3.downloads == [] and run.calls == []
    assert patched._kf_inserts == [] and patched._kf_events == []
    # excluded from `expected` -> stale sweep deletes the S3 object AND the M-2
    # predicate removes the dangling row (tombstone's garbage collector)
    assert s3.deletes == [kf_key]
    assert kf_key not in conn.rows
    assert result == {"results": [{"skipped": False, "keyframes": 0}]}


def test_one_tombstoned_frame_among_live_ones_only_skips_that_frame(patched):
    # 10:00-10:06 -> 3 frames; tombstone only the middle one (10:03:00).
    sidecar = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-00-00_vad_metadata.json"
    video = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-00-00.mp4"
    tombstoned = PICS + "Benl1_2026-07-23_10-03-00_kf_s101534.jpg"
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:00 – 10:06"}]),
                 sidecar: _sidecar(source_key=video, dur=600)})
    patched.setattr(lk, "_s3_client", s3)
    patched.setattr(lk.keyframes, "tombstoned_subset", lambda conn, keys: {tombstoned})
    run = FakeRun()
    patched.setattr(lk.subprocess, "run", run)

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    keys = sorted(k for _, k, _ in s3.uploads)
    assert keys == [
        PICS + "Benl1_2026-07-23_10-01-00_kf_s101534.jpg",
        PICS + "Benl1_2026-07-23_10-05-00_kf_s101534.jpg",
    ]                                        # the tombstoned middle frame never produced
    assert result == {"results": [{"skipped": False, "keyframes": 2}]}


# --------------------------------------------------------------------------
# Q7 -- 'generated' telemetry: one row per frame ACTUALLY produced (real upload
#   only), with the right frame_index / n_frames_generated / duration_min; the
#   head-exists fast path writes NOTHING (S3-retry dedup).
# --------------------------------------------------------------------------

def test_generated_event_written_only_on_real_upload(patched):
    # 10:00-10:06 -> 3 frames; the middle one already exists in S3 (head hit),
    # so only the two freshly-decoded frames emit a 'generated' event.
    sidecar = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-00-00_vad_metadata.json"
    video = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-00-00.mp4"
    existing = PICS + "Benl1_2026-07-23_10-03-00_kf_s101534.jpg"   # frame_index 1, head hit
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:00 – 10:06"}]),
                 sidecar: _sidecar(source_key=video, dur=600),
                 existing: b"already-here"})
    patched.setattr(lk, "_s3_client", s3)
    run = FakeRun()
    patched.setattr(lk.subprocess, "run", run)

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    # exactly two 'generated' events -- the head-hit middle frame emitted none
    assert [e for e, _ in patched._kf_events] == ["generated", "generated"]
    by_index = {kw["frame_index"]: kw for _, kw in patched._kf_events}
    assert set(by_index) == {0, 2}                     # NOT frame_index 1 (head hit)
    for kw in by_index.values():
        assert kw["n_frames_generated"] == 3
        assert kw["duration_min"] == 6
        assert kw["topic_category"] == "safety"        # from the widened topic row
        assert kw["work_class"] == "work"
        assert kw["company_id"] == "co-1" and kw["site_id"] == "s-1"


def test_telemetry_db_failure_does_not_abort_tx_or_drop_bindings(patched):
    """The real failure mode: a DB-level telemetry failure (e.g. keyframe_events
    not migrated in this environment) ABORTS the transaction. Without a SAVEPOINT
    around record_event, the next statement -- add_topic_photo_if_absent's own
    `with conn.transaction():` -- raises InFailedSqlTransaction, which its
    `except ForeignKeyViolation` does not catch; that escapes the per-plan
    try/finally and rolls back EVERY bind of the request (S3 objects uploaded,
    zero rows committed, request permanently dropped after S3's retries).

    Uses the REAL add_topic_photo_if_absent against a connection that models
    psycopg3 abort semantics, so the savepoint is genuinely exercised."""
    from repositories.topics import add_topic_photo_if_absent

    # 3-frame topic: proves the blast radius is the WHOLE request, not one frame.
    sidecar = "audio_segments/Ben_UCPK/2026-07-23/Benl1_2026-07-23_10-00-00_vad_metadata.json"
    video = "users/Ben_UCPK/video/2026-07-23/Benl1_2026-07-23_10-00-00.mp4"
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:00 – 10:06"}]),
                 sidecar: _sidecar(source_key=video, dur=600)})
    patched.setattr(lk, "_s3_client", s3)
    conn = TxAbortConn()
    patched.setattr(lk, "get_connection", lambda *a, **k: conn)
    patched.setattr(lk, "add_topic_photo_if_absent", add_topic_photo_if_absent)

    def failing_record_event(conn_, event, **kw):
        conn_.failed = True                       # the INSERT poisoned the tx
        raise psycopg.errors.UndefinedTable(
            'relation "keyframe_events" does not exist')
    patched.setattr(lk.keyframes, "record_event", failing_record_event)
    patched.setattr(lk.subprocess, "run", FakeRun())

    result = lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    # never raised, and EVERY frame still bound despite telemetry failing on each
    assert result == {"results": [{"skipped": False, "keyframes": 3}]}
    assert len(s3.uploads) == 3
    assert conn.rows == {
        ("topic-1", PICS + "Benl1_2026-07-23_10-01-00_kf_s101534.jpg"),
        ("topic-1", PICS + "Benl1_2026-07-23_10-03-00_kf_s101534.jpg"),
        ("topic-1", PICS + "Benl1_2026-07-23_10-05-00_kf_s101534.jpg"),
    }
    assert conn.failed is False       # savepoint rollback left the conn usable


def test_telemetry_write_is_wrapped_in_a_savepoint(patched):
    """Belt-and-braces on the same defect: assert the record_event call site is
    lexically inside a `with conn.transaction():`, by failing the test if
    record_event is ever reached without an open savepoint."""
    depth = {"n": 0}

    class _CountingSavepoint(_Savepoint):
        def __enter__(self):
            r = super().__enter__()
            depth["n"] += 1
            return r

        def __exit__(self, *a):
            depth["n"] -= 1
            return super().__exit__(*a)

    class CountingConn(TxAbortConn):
        def transaction(self):
            return _CountingSavepoint(self)

    seen = []
    s3 = FakeS3({REQUEST_KEY: _request([{"topic_id": "topic-1", "time_range": "10:15 – 10:17"}]),
                 SIDECAR_KEY: _sidecar()})
    patched.setattr(lk, "_s3_client", s3)
    patched.setattr(lk, "get_connection", lambda *a, **k: CountingConn())
    patched.setattr(lk.keyframes, "record_event",
                    lambda conn_, event, **kw: seen.append(depth["n"]) or {"id": "e"})
    patched.setattr(lk.subprocess, "run", FakeRun())

    lk.lambda_handler({"Records": [{"s3": {"object": {"key": REQUEST_KEY}}}]}, None)

    assert seen, "record_event was never called"
    assert all(d >= 1 for d in seen), \
        "record_event ran OUTSIDE conn.transaction() -- a DB failure would abort the tx"


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
