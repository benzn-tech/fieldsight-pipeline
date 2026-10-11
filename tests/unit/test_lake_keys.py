import pytest
import lake_keys

U = "11111111-1111-1111-1111-111111111111"
S = "22222222-2222-2222-2222-222222222222"
SID = "a" * 32

CASES = [
    ("users/Ada_L/audio/2026-10-01/Ada_L_2026-10-01_09-00-00_sid%s_c0001.wav" % SID,
     dict(scope="tenant", kind="media", folder="Ada_L", day="2026-10-01", sid=SID)),
    ("audio_segments/Ada_L/2026-10-01/x_off0.wav", dict(kind="segment", folder="Ada_L", day="2026-10-01")),
    ("web_video/Ada_L/2026-10-01/x.mp4", dict(kind="web_video", folder="Ada_L")),
    ("transcripts/Ada_L/2026-10-01/x.json", dict(kind="transcript", folder="Ada_L", day="2026-10-01")),
    ("extractions/Ada_L/2026-10-01/x_sid%s.json" % SID, dict(kind="extraction", sid=SID)),
    ("reports/2026-10-01/Ada_L/daily_report.json", dict(kind="report", folder="Ada_L", day="2026-10-01")),
    ("reports/2026-10-01/Ada_L/daily_report.docx", dict(kind="report", folder="Ada_L")),
    ("reports/2026-10-01/summary_report.json", dict(scope="lake_wide", kind="lake_wide")),
    ("reports/2026-10-01/daily_report.json", dict(scope="lake_wide", kind="lake_wide")),
    ("reports/2026-10-01/sites/tower-a/daily.json", dict(kind="site_report", site_slug="tower-a", folder=None)),
    ("meeting_minutes/2026-10-01/standup.docx", dict(scope="lake_wide", kind="meeting_minutes")),
    ("embeddings/2026-10-01/Ada_L/vectors.json", dict(kind="embedding", folder="Ada_L")),
    ("session_reports/Ada_L/2026-10-01/day/r1.docx", dict(kind="session_report", folder="Ada_L")),
    ("extraction_pending/Ada_L_2026-10-01_09-00-00_sid%s.json" % SID,
     dict(kind="session_marker", sid=SID, session_base="Ada_L_2026-10-01_09-00-00_sid%s" % SID)),
    ("extraction_requests/%s.json" % SID, dict(kind="session_marker", sid=SID, session_base=SID)),
    ("voice/%s/%s/a_b.m4a" % (U, S), dict(kind="voice", company_id=U, site_id=S)),
    ("voiceprint_requests/%s/base/r.json" % U, dict(kind="voiceprint", company_id=U)),
    ("weather/%s/2026-10-01/actual.json" % S, dict(kind="weather", site_id=S)),
    ("programmes/%s/programme.json" % S, dict(kind="programme", site_id=S)),
    ("traces/2026-10-01/Ada_L/090000-x-1.jsonl", dict(kind="trace", folder="Ada_L", day="2026-10-01")),
    ("config/directory.json", dict(scope="global")),
    ("models/silero.onnx", dict(scope="global")),
    ("download_claims/x", dict(scope="global")),
]


@pytest.mark.parametrize("key,want", CASES)
def test_parse(key, want):
    got = lake_keys.parse(key)
    assert got is not None, key
    for k, v in want.items():
        assert got[k] == v, (key, k, got)


def test_unknown_shape_is_none_not_a_guess():
    assert lake_keys.parse("weird/thing") is None
    assert lake_keys.parse("users/onlyfolder") is None
    assert lake_keys.parse("") is None
    assert lake_keys.parse(None) is None


def test_lake_wide_report_never_yields_a_folder():
    # reports/{d}/summary_report.json must not be read as folder "summary_report.json".
    assert lake_keys.parse("reports/2026-10-01/summary_report.json")["folder"] is None
