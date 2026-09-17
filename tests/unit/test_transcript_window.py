"""Time is the only axis present on every recording: it comes out of the filename
plus the VAD offset, so this works on recordings that predate sessions and on days
with no session at all."""
import datetime as dt
import json

import pytest

import transcript_window


BODY = {"results": {"transcripts": [{"transcript": "morning"}],
                    "items": [], "speaker_labels": {"segments": []}}}


class FakeS3:
    def __init__(self, keys):
        self._keys = keys
        self.listed = []

    def list_objects_v2(self, **kw):
        self.listed.append(kw.get("Prefix"))
        page = [{"Key": k} for k in self._keys[:2]] if "ContinuationToken" not in kw else \
               [{"Key": k} for k in self._keys[2:]]
        more = "ContinuationToken" not in kw and len(self._keys) > 2
        out = {"Contents": page, "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = "t"
        return out

    def get_object(self, Bucket=None, Key=None):
        return {"Body": type("B", (), {"read": staticmethod(
            lambda: json.dumps(BODY).encode("utf-8"))})()}


def _k(name):
    return "transcripts/Ben_UCPK2/2026-09-10/" + name


KEY_0858 = _k("Ben_UCPK2_2026-09-10_08-58-00_to180_vad.json")   # ends 09:01 -- straddles
KEY_0930 = _k("Ben_UCPK2_2026-09-10_09-30-00_to120_vad.json")   # inside
KEY_1200 = _k("Ben_UCPK2_2026-09-10_12-00-00_to120_vad.json")   # after


def test_a_recording_straddling_the_start_is_kept_whole():
    s3 = FakeS3([KEY_0858, KEY_0930, KEY_1200])
    picked = transcript_window.select_keys(
        s3, "b", "Ben_UCPK2", "2026-09-10",
        dt.datetime(2026, 9, 10, 9, 0), dt.datetime(2026, 9, 10, 11, 30))
    assert [k for _, k in picked] == [KEY_0858, KEY_0930]


def test_every_page_of_the_listing_is_read():
    s3 = FakeS3([KEY_0858, KEY_0930, KEY_1200])
    transcript_window.select_keys(
        s3, "b", "Ben_UCPK2", "2026-09-10",
        dt.datetime(2026, 9, 10, 0, 0), dt.datetime(2026, 9, 10, 23, 59))
    assert s3.listed == ["transcripts/Ben_UCPK2/2026-09-10/", "transcripts/Ben_UCPK2/2026-09-10/"]


def test_an_unreadable_filename_is_skipped_not_guessed():
    s3 = FakeS3([_k("not-a-recording.json"), KEY_0930])
    picked = transcript_window.select_keys(
        s3, "b", "Ben_UCPK2", "2026-09-10",
        dt.datetime(2026, 9, 10, 9, 0), dt.datetime(2026, 9, 10, 11, 30))
    assert [k for _, k in picked] == [KEY_0930]


def test_assemble_returns_timestamped_lines_in_order():
    s3 = FakeS3([KEY_0858, KEY_0930])
    picked = transcript_window.select_keys(
        s3, "b", "Ben_UCPK2", "2026-09-10",
        dt.datetime(2026, 9, 10, 9, 0), dt.datetime(2026, 9, 10, 11, 30))
    turns = transcript_window.assemble(s3, "b", picked)
    assert turns and all("at" in t and "line" in t for t in turns)
    assert turns == sorted(turns, key=lambda t: t["at"])


# ----------------------------------------------------------
# CRITICAL 2 — select_keys/assemble ran unbounded before the model call's own
# budget. `deadline` bounds the read phase itself: an oversized picked list is
# refused before the first S3 read, and a deadline that has already passed
# stops the loop mid-way, both with a "window too large" error rather than a
# silent SIGKILL.
# ----------------------------------------------------------

def test_a_picked_list_over_the_object_cap_is_refused_before_any_read():
    keys = [_k("Ben_UCPK2_2026-09-10_%02d-00-00_to60_vad.json" % h)
           for h in range(transcript_window.MAX_TRANSCRIPT_OBJECTS + 1)]
    s3 = FakeS3(keys)
    picked = [(dt.datetime(2026, 9, 10, 0, 0), k) for k in keys]
    with pytest.raises(transcript_window.WindowTooLarge, match="window too large"):
        transcript_window.assemble(s3, "b", picked, deadline=999999999999.0)


def test_a_deadline_already_passed_stops_the_read_phase():
    s3 = FakeS3([KEY_0858, KEY_0930])
    picked = transcript_window.select_keys(
        s3, "b", "Ben_UCPK2", "2026-09-10",
        dt.datetime(2026, 9, 10, 9, 0), dt.datetime(2026, 9, 10, 11, 30))
    with pytest.raises(transcript_window.WindowTooLarge, match="window too large"):
        transcript_window.assemble(s3, "b", picked, deadline=0.0)


def test_no_deadline_means_no_cap_or_time_check():
    """`deadline=None` (the pre-existing signature) must behave exactly as
    before -- no caller that never asked for a bound gets a new failure mode."""
    n = transcript_window.MAX_TRANSCRIPT_OBJECTS + 1
    keys = [_k("Ben_UCPK2_2026-09-10_%02d-%02d-00_to60_vad.json" % (h % 24, h // 24))
           for h in range(n)]
    s3 = FakeS3(keys)
    picked = [(dt.datetime(2026, 9, 10, 0, 0), k) for k in keys]
    turns = transcript_window.assemble(s3, "b", picked)
    assert len(turns) == len(keys)


# ----------------------------------------------------------
# The object cap was a hardcoded 500 that no measurement supports. It is now read
# from the environment (default 2000) so it can move without a code change. Kept
# module-level: the cap tests above read transcript_window.MAX_TRANSCRIPT_OBJECTS
# as the effective limit.
# ----------------------------------------------------------

import importlib


def test_the_object_cap_defaults_to_two_thousand(monkeypatch):
    monkeypatch.delenv("MAX_TRANSCRIPT_OBJECTS", raising=False)
    try:
        assert importlib.reload(transcript_window).MAX_TRANSCRIPT_OBJECTS == 2000
    finally:
        importlib.reload(transcript_window)


def test_the_object_cap_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MAX_TRANSCRIPT_OBJECTS", "7")
    try:
        assert importlib.reload(transcript_window).MAX_TRANSCRIPT_OBJECTS == 7
    finally:
        monkeypatch.delenv("MAX_TRANSCRIPT_OBJECTS")
        importlib.reload(transcript_window)
