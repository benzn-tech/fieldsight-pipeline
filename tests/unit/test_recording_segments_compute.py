"""Unit: lambda_recording_segments -- segment derivation, debounce, trailing pass, handler.

The repository is replaced by an in-memory double (its SQL is exercised for real in
tests/integration/test_day_recording_segments.py); S3 by a paginating double; the
DynamoDB flag by recording stubs. The functions under test are the real ones.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("psycopg", reason="requires psycopg (installed in CI)")
import lambda_recording_segments as rs  # noqa: E402
import recording_blocks  # noqa: E402
import sweep_state  # noqa: E402

SID_A = "81a64d850bb34d8bbf01ec11fd2af02f"
SID_B = "c3d1e0a2b4f64e5f9a7b8c9d0e1f2a3b"
SID_C = "5e6f7a8b9c0d4e1f8a2b3c4d5e6f7a8b"
PREFIX = "transcripts/Ben_UCPK2/2026-09-02/"
REAL_KEY = PREFIX + f"ben_ucpk2_2026-09-02_10-55-18_sid{SID_A}_c0001_off0.0_to5.0_srcwav.json"
NOW = datetime(2026, 9, 2, 6, 0, 0, tzinfo=timezone.utc)       # minute 0: not a safety tick
USER = {"id": "u-ben", "folder_name": "Ben_UCPK2"}

# Real key shapes (device name, date, time, sid, chunk index, VAD offsets, source) for
# the constructed 2026-09-02 day, plus a Transcribe write-check object that is not a
# transcript at all.
DAY_KEYS = [PREFIX + n for n in (
    f"ben_ucpk2_2026-09-02_10-55-18_sid{SID_A}_c0001_off0.0_to5.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-05-02_sid{SID_A}_c0021_off0.0_to28.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-14-40_sid{SID_A}_c0041_off2.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-24-10_sid{SID_A}_c0061_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-33-50_sid{SID_A}_c0079_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-35-12_sid{SID_A}_c0081_off0.0_to20.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-14-05_sid{SID_B}_c0001_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-23-40_sid{SID_B}_c0019_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-32-55_sid{SID_B}_c0037_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-41-30_sid{SID_B}_c0055_off1.5_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-46-40_sid{SID_B}_c0065_off0.0_to25.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-57-30_sid{SID_C}_c0001_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_18-06-10_sid{SID_C}_c0017_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_18-14-50_sid{SID_C}_c0033_off0.0_to20.0_srcwav.json",
    ".write_access_check_file.temp",
)]


class FakePaginator:
    def __init__(self, pages, calls):
        self.pages, self.calls = pages, calls

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.pages)


class FakeS3:
    """list_objects_v2 over `keys`, `page_size` per page. Records every paginate call."""

    def __init__(self, keys, page_size=4):
        self.pages = [{"Contents": [{"Key": k} for k in keys[i:i + page_size]]}
                      for i in range(0, len(keys), page_size)] or [{"KeyCount": 0}]
        self.paginate_calls = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self.pages, self.paginate_calls)


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _dirty_row(date, count=1):
    return {"user_id": "u-ben", "report_date": date, "folder_name": "Ben_UCPK2",
            "segments": [], "source_object_count": count, "dirty": True, "computed_at": NOW}


@pytest.fixture
def store(monkeypatch):
    """In-memory day_recording_segments with the same monotonic rule as the SQL, plus a
    recording stand-in for the sweep_state flag. `events` records flag calls and dirty
    listings in order."""
    state = {"rows": {}, "upserts": [], "dirty_marks": [], "events": [], "pending": True}

    def get(conn, user_id, report_date):
        return state["rows"].get((user_id, str(report_date)))

    def upsert_monotonic(conn, user_id, report_date, folder_name, segments, count):
        state["upserts"].append({"user_id": user_id, "report_date": str(report_date),
                                 "folder_name": folder_name, "segments": segments,
                                 "source_object_count": count})
        key = (user_id, str(report_date))
        old = state["rows"].get(key)
        if old is not None and count < old["source_object_count"]:
            return False
        state["rows"][key] = {"user_id": user_id, "report_date": str(report_date),
                              "folder_name": folder_name, "segments": segments,
                              "source_object_count": count, "dirty": False,
                              "computed_at": NOW}
        return True

    def mark_dirty(conn, user_id, report_date):
        state["dirty_marks"].append((user_id, str(report_date)))
        row = state["rows"].get((user_id, str(report_date)))
        if row is None:
            return False
        row["dirty"] = True
        return True

    def list_dirty(conn, limit=50):
        state["events"].append(("list", None))
        return [{"user_id": r["user_id"], "report_date": r["report_date"],
                 "folder_name": r["folder_name"]}
                for r in state["rows"].values() if r["dirty"]][:limit]

    monkeypatch.setattr(rs.day_recording_segments, "get", get)
    monkeypatch.setattr(rs.day_recording_segments, "upsert_monotonic", upsert_monotonic)
    monkeypatch.setattr(rs.day_recording_segments, "mark_dirty", mark_dirty)
    monkeypatch.setattr(rs.day_recording_segments, "list_dirty", list_dirty)
    monkeypatch.setattr(rs.users, "get_by_folder_name_global",
                        lambda conn, folder: dict(USER) if folder == "Ben_UCPK2" else None)
    monkeypatch.setattr(rs.sweep_state, "mark_pending",
                        lambda key, **kw: state["events"].append(("mark", key)) or True)
    monkeypatch.setattr(rs.sweep_state, "clear_pending",
                        lambda key, **kw: state["events"].append(("clear", key)) or True)
    monkeypatch.setattr(rs.sweep_state, "is_pending", lambda key, **kw: state["pending"])
    return state


# ---- segment_from_key --------------------------------------------------------

def test_a_real_chunk_key_yields_its_wall_clock_span_and_session():
    assert rs.segment_from_key(REAL_KEY) == {
        "start": 39318.0, "end": 39323.0, "session_id": SID_A, "key": REAL_KEY}


def test_the_vad_offset_moves_the_start_and_the_span_is_to_minus_off():
    key = PREFIX + f"ben_ucpk2_2026-09-02_17-41-30_sid{SID_B}_c0055_off1.5_to30.0_srcwav.json"
    seg = rs.segment_from_key(key)
    assert seg["start"] == 63691.5 and seg["end"] == 63720.0


def test_a_batch_key_reads_as_its_first_chunk():
    key = PREFIX + f"ben_ucpk2_2026-09-02_17-41-30_sid{SID_B}_c0055_bn4_off1.5_to114.0_srcwav.json"
    seg = rs.segment_from_key(key)
    assert seg["session_id"] == SID_B
    assert seg["start"] == 63691.5 and seg["end"] == 63804.0


def test_a_legacy_vad_key_has_no_session_and_keeps_its_offset():
    key = "transcripts/Benl1/2026-03-20/Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json"
    seg = rs.segment_from_key(key)
    assert seg["session_id"] is None
    assert seg["start"] == pytest.approx(45779.8) and seg["end"] == pytest.approx(46043.8)


def test_a_whole_file_key_has_no_length_in_its_name_so_end_equals_start():
    seg = rs.segment_from_key("transcripts/Benl1/2026-03-20/Benl1_2026-03-20_12-18-34.json")
    assert seg["start"] == 44314.0 and seg["end"] == 44314.0


def test_the_time_is_read_after_the_date_never_from_it():
    # BUG-01: a device name full of digit-dash pairs must not be read as the time.
    key = PREFIX + "cam26-02-09_2026-09-02_10-55-18_off0.0_to5.0_srcwav.json"
    assert rs.segment_from_key(key)["start"] == 39318.0


def test_names_without_a_base_time_and_non_json_objects_yield_nothing():
    assert rs.segment_from_key(PREFIX + "notes.json") is None
    assert rs.segment_from_key(PREFIX + ".write_access_check_file.temp") is None


# ---- event shapes and the flag -------------------------------------------------

def test_both_trigger_shapes_yield_the_key():
    eventbridge = {"source": "aws.s3", "detail-type": "Object Created",
                   "detail": {"object": {"key": REAL_KEY}}}
    s3_records = {"Records": [{"s3": {"object": {"key": REAL_KEY.replace("_", "%5F")}}}]}
    assert rs.keys_from_event(eventbridge) == [REAL_KEY]
    assert rs.keys_from_event(s3_records) == [REAL_KEY]


def test_the_schedule_event_is_told_apart_from_an_object_event():
    schedule = {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}
    obj = {"source": "aws.s3", "detail-type": "Object Created",
           "detail": {"object": {"key": REAL_KEY}}}
    assert rs.is_schedule_event(schedule) is True
    assert rs.is_schedule_event(obj) is False
    assert rs.keys_from_event(schedule) == []


def test_the_flag_is_a_separate_item_from_the_finalize_sweeps():
    assert rs.FLAG_KEY == f"RECORDING_BLOCKS#{rs.STAGE}"
    assert sweep_state._pk(rs.FLAG_KEY) != sweep_state._pk(rs.STAGE)


def test_exactly_one_five_minute_tick_per_hour_is_a_safety_tick_whatever_the_phase():
    for phase in range(5):
        ticks = [datetime(2026, 9, 2, 6, m, tzinfo=timezone.utc) for m in range(phase, 60, 5)]
        assert sum(rs.is_safety_tick(t) for t in ticks) == 1, f"phase {phase}"
    # and the window contains the finalize sweep's safety minute (7)
    assert rs.is_safety_tick(datetime(2026, 9, 2, 6, 7, tzinfo=timezone.utc))


# ---- handle_object_key -------------------------------------------------------

def test_first_transcript_of_a_day_lists_every_page_and_writes_sorted_segments(store, caplog):
    caplog.set_level(logging.INFO)
    s3 = FakeS3(DAY_KEYS, page_size=4)
    assert rs.handle_object_key(FakeConn(), s3, "bkt", REAL_KEY, NOW) == "computed"
    assert s3.paginate_calls == [{"Bucket": "bkt", "Prefix": PREFIX}]
    [write] = store["upserts"]
    assert write["user_id"] == "u-ben" and write["report_date"] == "2026-09-02"
    assert write["folder_name"] == "Ben_UCPK2"
    assert write["source_object_count"] == 15                  # every listed object
    assert len(write["segments"]) == 14                        # the .temp is not a segment
    starts = [s["start"] for s in write["segments"]]
    assert starts == sorted(starts)
    assert "skipped 1 object(s) with no base time" in caplog.text
    assert "recording_segments: computed folder=Ben_UCPK2 date=2026-09-02" in caplog.text
    assert store["events"] == []                               # a computed day raises no flag


def test_an_unresolvable_folder_is_skipped_without_listing_or_guessing(store, caplog):
    caplog.set_level(logging.INFO)
    s3 = FakeS3(DAY_KEYS)
    key = "transcripts/Nobody/2026-09-02/x_2026-09-02_10-00-00.json"
    assert rs.handle_object_key(FakeConn(), s3, "bkt", key, NOW) == "skipped-unresolved"
    assert s3.paginate_calls == [] and store["upserts"] == []
    assert "skipped-unresolved folder=Nobody" in caplog.text


def test_a_key_outside_the_day_layout_is_ignored_and_says_so(store, caplog):
    caplog.set_level(logging.INFO)
    s3 = FakeS3(DAY_KEYS)
    assert rs.handle_object_key(FakeConn(), s3, "bkt", "transcripts/flat.json", NOW) == "ignored"
    assert s3.paginate_calls == []
    assert "ignored key=transcripts/flat.json" in caplog.text


def test_a_day_computed_moments_ago_is_marked_dirty_and_flagged_not_recomputed(store, caplog):
    caplog.set_level(logging.INFO)
    row = _dirty_row("2026-09-02", count=3)
    row.update(dirty=False, computed_at=NOW - timedelta(seconds=rs.DEBOUNCE_SECONDS - 1))
    store["rows"][("u-ben", "2026-09-02")] = row
    s3 = FakeS3(DAY_KEYS)
    assert rs.handle_object_key(FakeConn(), s3, "bkt", REAL_KEY, NOW) == "debounced-dirty"
    assert s3.paginate_calls == [] and store["upserts"] == []
    assert store["rows"][("u-ben", "2026-09-02")]["dirty"] is True
    assert store["events"] == [("mark", rs.FLAG_KEY)]
    assert "debounced-dirty folder=Ben_UCPK2 date=2026-09-02" in caplog.text


def test_a_day_computed_longer_ago_than_the_debounce_is_recomputed(store):
    row = _dirty_row("2026-09-02", count=3)
    row.update(dirty=False, computed_at=NOW - timedelta(seconds=rs.DEBOUNCE_SECONDS))
    store["rows"][("u-ben", "2026-09-02")] = row
    assert rs.handle_object_key(FakeConn(), FakeS3(DAY_KEYS), "bkt", REAL_KEY, NOW) == "computed"
    assert store["dirty_marks"] == []


def test_the_debounce_constant_is_thirty_seconds():
    assert rs.DEBOUNCE_SECONDS == 30


# ---- sweep_dirty ---------------------------------------------------------------

def test_the_trailing_pass_recomputes_every_dirty_day_and_clears_it(store, caplog):
    caplog.set_level(logging.INFO)
    for date in ("2026-09-02", "2026-09-03"):
        store["rows"][("u-ben", date)] = _dirty_row(date)
    s3 = FakeS3(DAY_KEYS)
    assert rs.sweep_dirty(FakeConn(), s3, "bkt") == 2
    assert [c["Prefix"] for c in s3.paginate_calls] == [
        "transcripts/Ben_UCPK2/2026-09-02/", "transcripts/Ben_UCPK2/2026-09-03/"]
    assert not any(r["dirty"] for r in store["rows"].values())
    assert "swept 2 of 2 dirty day(s)" in caplog.text


def test_one_failing_day_does_not_stop_the_trailing_pass(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    for date in ("2026-09-02", "2026-09-03"):
        store["rows"][("u-ben", date)] = _dirty_row(date)
    real = rs.list_day_keys

    def flaky(s3, bucket, folder, date):
        if str(date) == "2026-09-02":
            raise RuntimeError("boom")
        return real(s3, bucket, folder, date)

    monkeypatch.setattr(rs, "list_day_keys", flaky)
    assert rs.sweep_dirty(FakeConn(), FakeS3(DAY_KEYS), "bkt") == 1
    assert "sweep failed folder=Ben_UCPK2 date=2026-09-02" in caplog.text
    assert "swept 1 of 2 dirty day(s)" in caplog.text


def test_a_refused_smaller_listing_is_logged_by_the_trailing_pass(store, caplog):
    caplog.set_level(logging.INFO)
    store["rows"][("u-ben", "2026-09-02")] = _dirty_row("2026-09-02", count=99)
    assert rs.sweep_dirty(FakeConn(), FakeS3(DAY_KEYS), "bkt") == 1
    assert "sweep kept the larger stored row folder=Ben_UCPK2" in caplog.text
    assert store["rows"][("u-ben", "2026-09-02")]["dirty"] is True


# ---- lambda_handler --------------------------------------------------------------

SCHEDULE = {"source": "aws.events", "detail-type": "Scheduled Event"}


@pytest.fixture
def wired_handler(monkeypatch, store):
    s3 = FakeS3(DAY_KEYS)
    connections = []

    def connect(*a, **k):
        connections.append(k)
        return FakeConn()

    monkeypatch.setattr("boto3.client", lambda name, *a, **k: s3)
    monkeypatch.setattr("db.connection.get_connection", connect)
    monkeypatch.setattr(rs, "S3_BUCKET", "bkt")
    monkeypatch.setattr(rs, "_utcnow", lambda: NOW)
    return {"s3": s3, "connections": connections, "mp": monkeypatch}


def test_an_idle_tick_outside_the_safety_window_never_connects(wired_handler, store, caplog):
    caplog.set_level(logging.INFO)
    store["pending"] = False
    assert rs.lambda_handler(SCHEDULE, None) == {"swept": 0, "skipped": "no-pending"}
    assert wired_handler["connections"] == []
    assert store["events"] == []
    assert "sweep skipped (no dirty days flagged)" in caplog.text


def test_a_flagged_tick_clears_the_flag_before_listing_then_sweeps(wired_handler, store):
    store["rows"][("u-ben", "2026-09-02")] = _dirty_row("2026-09-02")
    assert rs.lambda_handler(SCHEDULE, None) == {"swept": 1}
    assert store["events"] == [("clear", rs.FLAG_KEY), ("list", None)]
    assert wired_handler["connections"] == [{"autocommit": True}]


def test_the_safety_tick_sweeps_even_when_the_flag_reads_idle(wired_handler, store):
    store["pending"] = False
    store["rows"][("u-ben", "2026-09-02")] = _dirty_row("2026-09-02")
    wired_handler["mp"].setattr(rs, "_utcnow", lambda: NOW.replace(minute=7))
    assert rs.lambda_handler(SCHEDULE, None) == {"swept": 1}


def test_the_handler_computes_an_object_event(wired_handler, store):
    event = {"source": "aws.s3", "detail-type": "Object Created",
             "detail": {"object": {"key": REAL_KEY}}}
    assert rs.lambda_handler(event, None) == {"outcomes": ["computed"]}
    assert len(store["upserts"]) == 1


def test_one_failing_key_does_not_sink_the_rest(wired_handler, store, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    real = rs.handle_object_key

    def flaky(conn, s3, bucket, key, now):
        if "Broken" in key:
            raise RuntimeError("boom")
        return real(conn, s3, bucket, key, now)

    monkeypatch.setattr(rs, "handle_object_key", flaky)
    event = {"Records": [
        {"s3": {"object": {"key": "transcripts/Broken/2026-09-02/a_2026-09-02_10-00-00.json"}}},
        {"s3": {"object": {"key": REAL_KEY}}}]}
    assert rs.lambda_handler(event, None) == {"outcomes": ["failed", "computed"]}
    assert "failed for key=transcripts/Broken/" in caplog.text


def test_an_event_with_no_key_says_so_and_does_not_connect(wired_handler, caplog):
    caplog.set_level(logging.INFO)
    assert rs.lambda_handler({"source": "aws.s3", "detail": {}}, None) == {"outcomes": []}
    assert wired_handler["connections"] == []
    assert "no object key in event; nothing to do" in caplog.text


# ---- seam: what the writer stores is what the reader merges ----------------------

def test_the_stored_payload_merges_into_the_0902_blocks_after_a_jsonb_round_trip(store):
    rs.handle_object_key(FakeConn(), FakeS3(DAY_KEYS), "bkt", REAL_KEY, NOW)
    stored = json.loads(json.dumps(store["upserts"][0]["segments"]))   # what jsonb gives back
    blocks = recording_blocks.merge_segments(stored, 600, 5400)
    assert [(b["from"], b["to"]) for b in blocks] == [
        ("10:55", "11:35"), ("17:14", "17:47"), ("17:57", "18:15")]
    assert [b["session_ids"] for b in blocks] == [[SID_A], [SID_B], [SID_C]]
    tombstoned = recording_blocks.filter_segments(
        stored, set(), [f"extractions/Ben_UCPK2/2026-09-02/sid{SID_B}"])
    assert [(b["from"], b["to"]) for b in recording_blocks.merge_segments(tombstoned, 600, 5400)] \
        == [("10:55", "11:35"), ("17:57", "18:15")]
