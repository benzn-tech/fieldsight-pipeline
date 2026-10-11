import uuid
import pytest
import lake_keys

U = "11111111-1111-1111-1111-111111111111"
S = "22222222-2222-2222-2222-222222222222"
SID = "a" * 32

D = "2026-10-01"
BASE = "Ada_L_2026-10-01_09-00-00_sid%s" % SID

# One case per inventory family (1.1-1.4); numbers are the inventory rows.
CASES = [
    # 1 users/ (audio with chunk sid, video, pictures)
    ("users/Ada_L/audio/%s/Ada_L_2026-10-01_09-00-00_sid%s_c0001.wav" % (D, SID),
     dict(scope="tenant", kind="media", folder="Ada_L", day=D, sid=SID)),
    ("users/Ada_L/video/%s/cam.mp4" % D, dict(scope="tenant", kind="media", folder="Ada_L", day=D, sid=None)),
    ("users/Ada_L/pictures/%s/p1.jpg" % D, dict(scope="tenant", kind="media", folder="Ada_L", day=D)),
    # 2 audio_segments (wav, sidecar, batch map)
    ("audio_segments/Ada_L/%s/x_off0.wav" % D, dict(scope="tenant", kind="segment", folder="Ada_L", day=D)),
    ("audio_segments/Ada_L/%s/x_vad_metadata.json" % D, dict(kind="segment", folder="Ada_L", day=D)),
    ("audio_segments/Ada_L/%s/b_batch_map.json" % D, dict(kind="segment", folder="Ada_L", day=D)),
    # 3 web_video, with a sid in the file name
    ("web_video/Ada_L/%s/x_sid%s.mp4" % (D, SID), dict(scope="tenant", kind="web_video", folder="Ada_L", day=D, sid=SID)),
    # 4 transcripts
    ("transcripts/Ada_L/%s/x.json" % D, dict(scope="tenant", kind="transcript", folder="Ada_L", day=D, session_base="x")),
    # 5 extractions, plus the merged group document
    ("extractions/Ada_L/%s/%s.json" % (D, BASE),
     dict(scope="tenant", kind="extraction", folder="Ada_L", day=D, sid=SID, session_base=BASE, group_id=None)),
    ("extractions/Ada_L/%s/grpG123.json" % D,
     dict(kind="extraction", folder="Ada_L", group_id="G123", session_base=None, sid=None)),
    # 6 reports under a folder (json, docx, debug, manifest, per-user weekly)
    ("reports/%s/Ada_L/daily_report.json" % D, dict(scope="tenant", kind="report", folder="Ada_L", day=D)),
    ("reports/%s/Ada_L/daily_report.docx" % D, dict(kind="report", folder="Ada_L")),
    ("reports/%s/Ada_L/daily_report_debug.json" % D, dict(kind="report", folder="Ada_L")),
    ("reports/%s/Ada_L/.meeting_manifest.json" % D, dict(kind="report", folder="Ada_L")),
    ("reports/%s/Ada_L/weekly_report.docx" % D, dict(kind="report", folder="Ada_L")),
    # 6b / 6d lake-wide
    ("reports/%s/summary_report.json" % D, dict(scope="lake_wide", kind="lake_wide", folder=None)),
    ("reports/%s/summary_report_debug.json" % D, dict(scope="lake_wide", kind="lake_wide")),
    ("reports/%s/daily_report.json" % D, dict(scope="lake_wide", kind="lake_wide", folder=None)),
    # 6c per-site reports (slug, not uuid)
    ("reports/%s/sites/tower-a/daily_report.json" % D,
     dict(scope="tenant", kind="site_report", site_slug="tower-a", site_id=None, folder=None, day=D)),
    # 7 embeddings
    ("embeddings/%s/Ada_L/vectors.json" % D, dict(scope="tenant", kind="embedding", folder="Ada_L", day=D)),
    # 8 / 9 reindex
    ("reindex_requests/%s/Ada_L/t1.json" % D, dict(scope="tenant", kind="reindex", folder="Ada_L", day=D)),
    ("reindex_vectors/%s/Ada_L/t1.json" % D, dict(scope="tenant", kind="reindex", folder="Ada_L", day=D)),
    # 10 meeting minutes
    ("meeting_minutes/%s/standup.docx" % D, dict(scope="lake_wide", kind="meeting_minutes")),
    # 11-13 session reports: sid is a directory segment (or "day")
    ("session_reports/Ada_L/%s/%s/r1.docx" % (D, SID), dict(kind="session_report", folder="Ada_L", day=D, sid=SID)),
    ("session_report_requests/Ada_L/%s/%s/r1.json" % (D, SID), dict(kind="session_report", folder="Ada_L", sid=SID)),
    ("session_report_results/Ada_L/%s/day/r1.json" % D, dict(kind="session_report", folder="Ada_L", day=D, sid=None)),
    # 14 / 15 rolling and brief
    ("session_rolling/Ada_L/%s/sid%s/latest.json" % (D, SID), dict(scope="tenant", kind="session_aux", folder="Ada_L", day=D, sid=SID)),
    ("session_brief/Ada_L/%s/%s/latest.json" % (D, SID), dict(scope="tenant", kind="session_aux", folder="Ada_L", day=D, sid=SID)),
    # 16 redactions, 17 voice_ask, 18 traces, 19 keyframe_requests, 20 report_requests, 21 photo selection
    ("redactions/Ada_L/%s/deleted_sessions.json" % D, dict(scope="tenant", kind="redaction", folder="Ada_L", day=D)),
    ("voice_ask/Ada_L/%s/090000-abc.json" % D, dict(scope="tenant", kind="voice_ask", folder="Ada_L", day=D)),
    ("traces/%s/Ada_L/090000-x-1.jsonl" % D, dict(scope="tenant", kind="trace", folder="Ada_L", day=D)),
    ("keyframe_requests/Ada_L/%s/h1.json" % D, dict(scope="tenant", kind="keyframe_req", folder="Ada_L", day=D)),
    ("report_requests/Ada_L/rid1.json", dict(scope="tenant", kind="report_request", folder="Ada_L", day=None)),
    ("report_photo_selection/Ada_L/%s.json" % D, dict(scope="tenant", kind="photo_selection", folder="Ada_L", day=D)),
    # 22-26 session-keyed markers
    ("extraction_requests/%s.json" % SID, dict(scope="tenant", kind="session_marker", sid=SID, session_base=SID, group_id=None)),
    ("extraction_requests/group-G9.json", dict(kind="session_marker", sid=None, session_base=None, group_id="G9")),
    ("extraction_pending/%s.json" % BASE, dict(kind="session_marker", sid=SID, session_base=BASE)),
    ("extraction_empty/%s.json" % BASE, dict(kind="session_marker", sid=SID, session_base=BASE)),
    ("session_finalize_requests/%s.json" % SID, dict(kind="session_marker", sid=SID, session_base=SID)),
    ("session_finalize_requests/%s-updated.json" % SID, dict(kind="session_marker", sid=SID, session_base=SID)),
    ("session_finalize_requests/%s-notes.json" % SID, dict(kind="session_marker", sid=SID, session_base=SID)),
    ("session_finalize_results/%s.json" % SID, dict(kind="session_marker", sid=SID, session_base=SID)),
    # 27-31 site / company keyed
    ("voice/%s/%s/a_b.m4a" % (U, S), dict(scope="tenant", kind="voice", company_id=U, site_id=S)),
    ("voiceprint_requests/%s/base1/r.json" % U, dict(scope="tenant", kind="voiceprint", company_id=U, session_base="base1")),
    ("match_requests/%s/%s/h.json" % (S, D), dict(scope="tenant", kind="match", site_id=S, day=D)),
    ("weather/%s/%s/actual.json" % (S, D), dict(scope="tenant", kind="weather", site_id=S, site_slug=None, day=D)),
    ("weather/tower-a/%s/actual.json" % D, dict(scope="tenant", kind="weather", site_id=None, site_slug="tower-a", day=D)),
    ("programmes/%s/programme.json" % S, dict(scope="tenant", kind="programme", site_id=S, site_slug=None)),
    ("programmes/tower-a/programme.json", dict(scope="tenant", kind="programme", site_id=None, site_slug="tower-a")),
    # 32-40 global / infra
    ("org-assets/pending/sub1/u.png", dict(scope="global")),
    ("notices/sent/external_member/n.json", dict(scope="global")),
    ("config/directory.json", dict(scope="global")),
    ("models/silero.onnx", dict(scope="global")),
    ("app-releases/app.apk", dict(scope="global")),
    ("scripts/fargate_downloader.py", dict(scope="global")),
    ("pending_downloads/x.mp4", dict(scope="global")),
    ("download_claims/x", dict(scope="global")),
    ("other/unknown_1", dict(scope="global")),
    ("lake_manifest/2026/x.json", dict(scope="global")),
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


def test_every_result_has_the_same_shape():
    shapes = {frozenset(lake_keys.parse(k)) for k, _ in CASES}
    assert len(shapes) == 1


RID = uuid.uuid4().hex


@pytest.mark.parametrize("key,sid,request_id", [
    ("report_requests/Ada_L/%s.json" % RID, None, RID),
    ("session_report_requests/Ada_L/%s/day/%s.json" % (D, RID), None, RID),
    ("session_report_results/Ada_L/%s/day/%s.json" % (D, RID), None, RID),
    ("session_reports/Ada_L/%s/day/%s.docx" % (D, RID), None, RID),
    ("session_report_requests/Ada_L/%s/%s/%s.json" % (D, SID, RID), SID, RID),
    ("session_reports/Ada_L/%s/%s/%s.docx" % (D, SID, RID), SID, RID),
])
def test_request_id_is_never_a_sid(key, sid, request_id):
    got = lake_keys.parse(key)
    assert got["sid"] == sid and got["request_id"] == request_id, got


def test_no_bare_hex_search_outside_known_positions():
    # a 32-hex path segment in a family with no sid position must not become a sid
    assert lake_keys.parse("transcripts/Ada_L/%s/%s.json" % (D, RID))["sid"] is None
    assert lake_keys.parse("keyframe_requests/Ada_L/%s/%s.json" % (D, RID))["sid"] is None
