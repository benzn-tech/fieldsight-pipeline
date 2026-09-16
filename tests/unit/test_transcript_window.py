"""Time is the only axis present on every recording: it comes out of the filename
plus the VAD offset, so this works on recordings that predate sessions and on days
with no session at all."""
import datetime as dt
import json

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
