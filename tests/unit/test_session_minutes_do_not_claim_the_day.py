"""Unit: minutes for ONE recording session must not replace the day.

`generate_meeting_minutes` was written for whole-day minutes, and on its way out
it writes three DAY-level objects under `reports/<date>/<user>/`:

  daily_report.json     a "compat" copy in the daily report's shape -- the SAME
                        key the nightly report generator writes, and the key
                        embed-report is triggered by
  .meeting_manifest.json marks the transcripts as consumed, so the nightly
                        report leaves them out; it REPLACES rather than merges
  meeting_minutes.json  read by fixed name by Ask (lambda_ask_agent.py:204) and
  meeting_minutes.docx  by the timeline's meeting view (fieldsight-ui meetings.js)

For a whole day that is the design: the day WAS a meeting. For one session of a
day, all three are wrong, and verified by running it on TEST on 2026-09-13:

  * the session's minutes overwrite that day's daily report;
  * the manifest claims only this session's transcripts and, being a
    replacement, un-claims any other session's -- so a second meeting on the same
    day puts the first meeting's content back into the daily report as well as
    leaving it in the first meeting's minutes;
  * two sessions overwrite each other's meeting_minutes.json.

Renaming those keys per session is not the fix: Ask and the timeline find
meeting_minutes.json by its fixed name, so a renamed copy is invisible to both.

So a session-scoped run writes ONLY under `meeting_minutes/`, where the key
already carries the session (see test_minutes_are_scoped_to_one_recording.py).
The day's report, its manifest, Ask and the timeline stay exactly as they would
be had the minutes never been generated. A whole-day run is unchanged.

Stated cost: minutes generated for one session are, for now, reachable only
from their archive key and the return value -- not from Ask or the timeline.
Nothing invokes this function today, so no user loses anything they had.
"""
import io
import os

import pytest

os.environ.setdefault("S3_BUCKET", "b")
os.environ.setdefault("ANTHROPIC_API_KEY", "k")

mm = pytest.importorskip(
    "lambda_meeting_minutes",
    reason="requires the meeting minutes lambda's dependencies (installed in CI)")

DATE = "2026-09-11"
USER = "Ben_Lin"
SID_A = "d740cd5fcaea4140bace19039dcc8649"
SID_B = "6237afecc49003fd264d66cf9db607d9"
DAY_PREFIX = f"reports/{DATE}/{USER}/"


class _S3:
    def __init__(self):
        self.keys = []

    def put_object(self, **kw):
        self.keys.append(kw["Key"])
        return {}


@pytest.fixture
def run(monkeypatch):
    """Drive generate_meeting_minutes with every external call replaced, and
    return (result, keys written, manifest calls)."""
    monkeypatch.setattr(mm, "S3_BUCKET", "b")

    def _go(session_id=None, title="Site coordination"):
        s3 = _S3()
        manifests = []
        monkeypatch.setattr(mm, "s3_client", s3)
        monkeypatch.setattr(mm, "collect_transcripts", lambda *a, **k: [{
            "key": f"transcripts/{USER}/{DATE}/ben_lin_{DATE}_13-40-38_sid{session_id or SID_A}_c0000.json",
            "device": USER, "word_count": 12, "segment_base_time": None,
            "full_text": "the slab pour finished", "speakers": ["spk_0", "spk_1"],
        }])
        monkeypatch.setattr(mm, "build_meeting_prompt", lambda t, c: "PROMPT")
        monkeypatch.setattr(mm, "call_claude_structured",
                            lambda prompt, max_tokens=None: (
                                '{"executive_summary": "done", "topics": []}', None))
        monkeypatch.setattr(mm, "save_debug_record", lambda *a, **k: None)
        monkeypatch.setattr(mm, "generate_word_document",
                            lambda minutes, title: io.BytesIO(b"docx"))
        monkeypatch.setattr(mm, "convert_to_daily_report_format",
                            lambda minutes, cfg, transcripts: ({"topics": []}, None))

        def _manifest(*a, **k):
            manifests.append((a, k))
            return f"{DAY_PREFIX}.meeting_manifest.json"

        monkeypatch.setattr(mm, "write_meeting_manifest", _manifest)
        cfg = {"date": DATE, "meeting_title": title, "meeting_type": "general",
               "attendees": ["Ben"], "user": USER, "transcript_prefix": None,
               "session_id": session_id, "triggered_by": "test"}
        result = mm.generate_meeting_minutes(cfg)
        return result, s3.keys, manifests

    return _go


# ---- a session-scoped run leaves the day alone --------------------------------

def test_a_sessions_minutes_do_not_overwrite_the_days_report(run):
    """THE test. Observed on TEST: the probe replaced daily_report.json."""
    result, keys, _ = run(session_id=SID_A)
    assert result["status"] == "success"
    assert f"{DAY_PREFIX}daily_report.json" not in keys
    assert result["compat_report_key"] is None


def test_a_sessions_minutes_do_not_claim_the_days_transcripts(run):
    """No manifest, so the nightly report still covers this session and nothing
    another session claimed is released."""
    result, _, manifests = run(session_id=SID_A)
    assert manifests == []
    assert result["manifest_key"] is None


def test_a_sessions_minutes_write_nothing_under_the_days_report_folder(run):
    """Stated as the whole rule rather than a list of known keys, so a fourth
    day-level write added later cannot slip past."""
    _, keys, _ = run(session_id=SID_A)
    assert [k for k in keys if k.startswith(DAY_PREFIX)] == []


def test_a_sessions_minutes_are_still_written_where_the_session_is_in_the_key(run):
    result, keys, _ = run(session_id=SID_A)
    archive = [k for k in keys if k.startswith(f"meeting_minutes/{DATE}/")]
    assert archive and all(SID_A[:8] in k for k in archive), archive
    assert any(k.endswith(".docx") for k in archive), "the Word copy is part of the minutes"
    assert result["s3_key"] in archive
    assert result["report_key"] is None


def test_two_sessions_on_one_day_share_no_key_at_all(run):
    _, keys_a, _ = run(session_id=SID_A)
    _, keys_b, _ = run(session_id=SID_B)
    assert set(keys_a).isdisjoint(keys_b), set(keys_a) & set(keys_b)


# ---- a whole-day run is exactly what it was ------------------------------------

def test_whole_day_minutes_still_write_every_day_level_object(run):
    """Every existing caller passes no session. Their minutes ARE the day, and
    the compat report is how that day reaches the index."""
    result, keys, manifests = run(session_id=None)
    assert f"{DAY_PREFIX}daily_report.json" in keys
    assert f"{DAY_PREFIX}meeting_minutes.json" in keys
    assert f"{DAY_PREFIX}meeting_minutes.docx" in keys
    assert len(manifests) == 1
    assert result["compat_report_key"] == f"{DAY_PREFIX}daily_report.json"
    assert result["report_key"] == f"{DAY_PREFIX}meeting_minutes.json"


def test_a_blank_session_id_is_a_whole_day_run(run):
    """Blank means absent, as it does for collection."""
    _, keys, manifests = run(session_id="   ")
    assert f"{DAY_PREFIX}daily_report.json" in keys
    assert len(manifests) == 1
