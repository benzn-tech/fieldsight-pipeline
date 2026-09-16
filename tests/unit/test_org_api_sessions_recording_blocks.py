"""GET /api/org/sessions -- the `recording_blocks` key (design 2026-09-15 §5.4, F1, F9).

A separate file from test_org_api_sessions.py on purpose: another branch
(feat/day-scoped-reports) edits that one, and the two must merge without conflict.
The conventions (FakeConn, make_event, _row, the `wired` stubs) are copied from it.

Drives the real route, build_day_sessions, recording_blocks and the threshold read;
only the database-facing repository calls are stubbed.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}
DATE = "2026-09-02"
DAY = {"date": DATE, "user": "Ada_L"}
SID_A = "81a64d850bb34d8bbf01ec11fd2af02f"
SID_B = "c3d1e0a2b4f64e5f9a7b8c9d0e1f2a3b"
SID_C = "5e6f7a8b9c0d4e1f8a2b3c4d5e6f7a8b"

# (start, end, sid, HH-MM-SS, chunk, off, to) -- the constructed 2026-09-02 day.
_SPANS = [
    (39318.0, 39323.0, SID_A, "10-55-18", "c0001", "0.0", "5.0"),
    (39902.0, 39930.0, SID_A, "11-05-02", "c0021", "0.0", "28.0"),
    (40482.0, 40510.0, SID_A, "11-14-40", "c0041", "2.0", "30.0"),
    (41050.0, 41080.0, SID_A, "11-24-10", "c0061", "0.0", "30.0"),
    (41630.0, 41660.0, SID_A, "11-33-50", "c0079", "0.0", "30.0"),
    (41712.0, 41732.0, SID_A, "11-35-12", "c0081", "0.0", "20.0"),
    (62045.0, 62075.0, SID_B, "17-14-05", "c0001", "0.0", "30.0"),
    (62620.0, 62650.0, SID_B, "17-23-40", "c0019", "0.0", "30.0"),
    (63175.0, 63205.0, SID_B, "17-32-55", "c0037", "0.0", "30.0"),
    (63691.5, 63720.0, SID_B, "17-41-30", "c0055", "1.5", "30.0"),
    (64000.0, 64025.0, SID_B, "17-46-40", "c0065", "0.0", "25.0"),
    (64650.0, 64680.0, SID_C, "17-57-30", "c0001", "0.0", "30.0"),
    (65170.0, 65200.0, SID_C, "18-06-10", "c0017", "0.0", "30.0"),
    (65690.0, 65710.0, SID_C, "18-14-50", "c0033", "0.0", "20.0"),
]
SEGMENTS = [{
    "start": s, "end": e, "session_id": sid,
    "key": f"transcripts/Ada_L/{DATE}/ada_l_{DATE}_{t}_sid{sid}_{c}_off{o}_to{to}_srcwav.json",
} for s, e, sid, t, c, o, to in _SPANS]


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def make_event(method, path, sub="sub-1", params=None):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": params,
        "body": None,
        "requestContext": {"authorizer": {"claims": {"sub": sub}}},
    }


def body_of(res):
    return json.loads(res["body"])


def _row(**over):
    base = {
        "id": "t-1", "site_id": SITE_ID, "site_name": "UC PK", "user_name": "Ada L",
        "source_s3_key": f"extractions/Ada_L/{DATE}/sid{SID_A}.json", "category": "progress",
        "title": "Slab pour", "summary": "Discussed the pour.", "time_range": "10:56 – 11:10",
        "participants": ["Ben", "Neil"], "work_class": "work",
        "action_items": [], "safety_observations": [], "findings": [], "photos": [],
    }
    base.update(over)
    return base


def _key(sid):
    return f"extractions/Ada_L/{DATE}/sid{sid}.json"


def _grp_key(sid):
    return f"extractions/Ada_L/{DATE}/grp{sid}.json"


ROWS = [
    _row(id="t-a1", source_s3_key=_key(SID_A), time_range="10:56 – 11:10"),
    _row(id="t-a2", source_s3_key=_key(SID_A), time_range="11:30 – 11:36"),
    _row(id="t-b1", source_s3_key=_key(SID_B), time_range="17:30 – 17:45"),
    _row(id="t-c1", source_s3_key=_key(SID_C), time_range="18:00 – 18:14"),
]


@pytest.fixture
def wired(monkeypatch):
    """Caller resolved, no redactions, ALL-scope site reach, no stored segments yet."""
    calls = {"owner_lookups": [], "segment_reads": [], "prefix_reads": [], "group_lookups": [],
             "group_member_lookups": []}
    # groups: {lead_sid: {member sids}}; grp_tombstones: [(target_key, reverted)]
    state = {"stored": None, "prefixes": [], "redacted": {}, "groups": {}, "grp_tombstones": []}

    def deleted_groups(conn, session_ids):
        """Stands in for the SQL in repositories.day_recording_segments (run for real in
        tests/integration): a session is hidden when an ACTIVE grp tombstone names its
        group -- its own id if it is the lead, the lead's id if it is a member."""
        calls["group_lookups"].append(sorted({s for s in session_ids if s}))
        dead = {key.rsplit("/grp", 1)[1] for key, reverted in state["grp_tombstones"] if not reverted}
        return {sid for sid in session_ids if sid and any(
            sid == gid or sid in state["groups"].get(gid, ()) for gid in dead)}

    def group_members(conn, lead_sids):
        """Stands in for the SQL in repositories.day_recording_segments (run for real in
        tests/integration): every lead id plus every member merged under it, tombstone
        status irrelevant -- this is exclusion, not deletion."""
        leads = sorted({s for s in lead_sids if s})
        calls["group_member_lookups"].append(leads)
        out = set()
        for lead in leads:
            out.add(lead)
            out |= set(state["groups"].get(lead, ()))
        return out

    monkeypatch.setattr(org.day_recording_segments, "deleted_group_session_ids", deleted_groups)
    monkeypatch.setattr(org.day_recording_segments, "group_member_session_ids", group_members)
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org.users, "get_by_folder_name",
                        lambda conn, cid, folder: {"id": f"u-{folder}", "folder_name": folder})

    def owner(conn, folder):
        calls["owner_lookups"].append(folder)
        return {"id": f"u-{folder}", "folder_name": folder}

    def segment_read(conn, user_id, report_date):
        calls["segment_reads"].append((user_id, report_date))
        return state["stored"]

    def prefixes(conn, folder=None, date=None):
        calls["prefix_reads"].append((folder, date))
        return list(state["prefixes"])

    monkeypatch.setattr(org.users, "get_by_folder_name_global", owner)
    monkeypatch.setattr(org.day_recording_segments, "get", segment_read)
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes", prefixes)
    monkeypatch.setattr(org.redactions, "list_active_for_topics",
                        lambda conn, ids: dict(state["redacted"]))
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org.recordings, "duration_for_media",
                        lambda conn, cid, folder, date, sb: None)
    # Chunk-session bases (`sid{hex}`) resolve their start/end from meeting_session.
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: None)
    monkeypatch.delenv("REPORT_BLOCK_GAP_SECONDS", raising=False)
    monkeypatch.delenv("REPORT_LONG_BLOCK_SECONDS", raising=False)
    return {"mp": monkeypatch, "calls": calls, "state": state}


def _wire_rows(wired, rows):
    wired["mp"].setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix, **kw: list(rows))


def _store(wired, segments=SEGMENTS):
    wired["state"]["stored"] = {"user_id": "u-Ada_L", "report_date": DATE,
                                "folder_name": "Ada_L", "segments": list(segments),
                                "source_object_count": len(segments), "dirty": False,
                                "computed_at": None}


def _get(params=None):
    return org.lambda_handler(make_event("GET", "/api/org/sessions", params=params), None)


def _spans(body):
    return [(b["from"], b["to"]) for b in body["recording_blocks"]]


def test_a_day_never_computed_has_no_key_at_all_and_gap_minutes_is_unchanged(wired):
    _wire_rows(wired, ROWS)
    res = _get(DAY)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert "recording_blocks" not in body
    assert body["gap_minutes"] == 15
    assert len(body["sessions"]) == 3


def test_blocks_carry_times_selectability_sessions_and_their_topics(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "17:47"), ("17:57", "18:15")]
    first = body["recording_blocks"][0]
    assert first["minutes"] == 40 and first["selectable_as_whole"] is True
    assert first["session_ids"] == [SID_A]
    assert [b["topic_row_ids"] for b in body["recording_blocks"]] == [
        ["t-a1", "t-a2"], ["t-b1"], ["t-c1"]]
    assert wired["calls"]["segment_reads"] == [("u-Ada_L", DATE)]
    assert wired["calls"]["prefix_reads"] == [("Ada_L", DATE)]


def test_a_session_whose_topics_are_all_excluded_is_not_advertised(wired):
    rows = [r if r["id"] != "t-b1" else dict(r, work_class="non_work") for r in ROWS]
    _wire_rows(wired, rows)
    _store(wired)
    body = body_of(_get(DAY))
    assert body["excluded"]["non_work"] == 1
    assert _spans(body) == [("10:55", "11:35"), ("17:57", "18:15")]


def test_a_session_with_audio_but_no_topic_rows_still_shows_its_block(wired):
    _wire_rows(wired, [r for r in ROWS if r["id"] != "t-c1"])
    _store(wired)
    body = body_of(_get(DAY))
    assert _spans(body)[-1] == ("17:57", "18:15")
    assert body["recording_blocks"][-1]["topic_row_ids"] == []


def test_a_deleted_recording_is_not_advertised(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["state"]["prefixes"] = [f"extractions/Ada_L/{DATE}/sid{SID_C}"]
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "17:47")]


def test_a_redacted_topic_is_never_named_by_a_block(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["state"]["redacted"] = {"t-a2": {"scope": "redacted"}}
    body = body_of(_get(DAY))
    assert body["recording_blocks"][0]["topic_row_ids"] == ["t-a1"]
    assert _spans(body)[0] == ("10:55", "11:35")      # the session still has a visible topic


def test_the_thresholds_are_read_from_the_environment_per_request(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["mp"].setenv("REPORT_BLOCK_GAP_SECONDS", "630")
    wired["mp"].setenv("REPORT_LONG_BLOCK_SECONDS", "1980")
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "18:15")]
    assert [b["selectable_as_whole"] for b in body["recording_blocks"]] == [False, False]


def test_the_owner_is_the_folder_being_read_not_the_caller(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    body_of(_get({"date": DATE, "user": "Bob_K"}))
    assert "Bob_K" in wired["calls"]["owner_lookups"]
    assert wired["calls"]["segment_reads"] == [("u-Bob_K", DATE)]


def test_an_unresolvable_owner_has_no_key(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["mp"].setattr(org.users, "get_by_folder_name_global", lambda conn, folder: None)
    assert "recording_blocks" not in body_of(_get(DAY))


# ---- deleted merged (multi-device) meetings ------------------------------------------
#
# Deleting a merged meeting tombstones ONE prefix: extractions/{lead_folder}/{date}/grp{lead_sid}.
# It names no device sid in the stored segments, so filter_segments' prefix arm alone never
# hides them -- and for a member on another folder, deleted_source_prefixes(folder, date)
# never even returns it. The stub above keeps prefixes empty to prove exactly that.

MEMBER_B = "7a1b2c3d4e5f60718293a4b5c6d7e8f9"
MEMBER_C = "90abcdef12345678901234567890abcd"


def _segments_for(folder, rename):
    out = []
    for s in SEGMENTS:
        sid = rename.get(s["session_id"], s["session_id"])
        out.append(dict(s, session_id=sid,
                        key=s["key"].replace("/Ada_L/", f"/{folder}/").replace(s["session_id"], sid)))
    return out


def test_deleting_a_merged_meeting_hides_the_leads_own_block(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["state"]["groups"] = {SID_B: {MEMBER_B}}
    wired["state"]["grp_tombstones"] = [(f"extractions/Ada_L/{DATE}/grp{SID_B}", False)]
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:57", "18:15")]
    assert wired["calls"]["group_lookups"] == [sorted([SID_A, SID_B, SID_C])]
    assert wired["calls"]["prefix_reads"] == [("Ada_L", DATE)]     # the prefix arm saw nothing


def test_a_members_day_hides_its_block_when_the_leads_meeting_is_deleted(wired):
    _wire_rows(wired, [])
    _store(wired, _segments_for("Bob_K", {SID_B: MEMBER_B}))
    wired["state"]["groups"] = {SID_B: {MEMBER_B}}
    wired["state"]["grp_tombstones"] = [(f"extractions/Ada_L/{DATE}/grp{SID_B}", False)]
    body = body_of(_get({"date": DATE, "user": "Bob_K"}))
    assert _spans(body) == [("10:55", "11:35"), ("17:57", "18:15")]
    assert wired["calls"]["prefix_reads"] == [("Bob_K", DATE)]


def test_a_group_deleted_after_a_member_recorded_on_a_different_folder(wired):
    # Cy_M joined Ada_L's 17:57 meeting (lead SID_C) from his own device and folder, and also
    # recorded a solo session in the morning. Only the meeting goes.
    _wire_rows(wired, [])
    _store(wired, _segments_for("Cy_M", {SID_C: MEMBER_C}))
    wired["state"]["groups"] = {SID_C: {MEMBER_C}}
    wired["state"]["grp_tombstones"] = [(f"extractions/Ada_L/{DATE}/grp{SID_C}", False)]
    body = body_of(_get({"date": DATE, "user": "Cy_M"}))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "17:47")]
    assert body["recording_blocks"][0]["session_ids"] == [SID_A]


def test_a_reverted_group_tombstone_hides_nothing(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["state"]["groups"] = {SID_B: {MEMBER_B}}
    wired["state"]["grp_tombstones"] = [(f"extractions/Ada_L/{DATE}/grp{SID_B}", True)]
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "17:47"), ("17:57", "18:15")]


# ---- fix round 1 (I1): a grp/legacy base excluded by non_work/redacted must still hide --
#
# device_session_id only matches a chunk session's `sid{32hex}` base -- a `grp{lead_sid}`
# base (a merged meeting) or a legacy whole-file base both map to None, and used to cancel
# out of both "has topics" and "listed" instead of being excluded (I1).

def test_a_merged_meetings_block_is_hidden_on_the_leads_day_when_all_its_topics_are_non_work(wired):
    rows = [r if r["id"] != "t-b1" else dict(r, source_s3_key=_grp_key(SID_B), work_class="non_work")
            for r in ROWS]
    _wire_rows(wired, rows)
    _store(wired)
    wired["state"]["groups"] = {SID_B: {MEMBER_B}}
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:57", "18:15")]
    assert wired["calls"]["group_member_lookups"] == [[SID_B]]


def test_a_merged_meetings_block_is_hidden_on_a_members_day_when_all_its_topics_are_non_work(wired):
    rows = [r if r["id"] != "t-b1" else dict(r, source_s3_key=_grp_key(SID_B), work_class="non_work")
            for r in ROWS]
    _wire_rows(wired, rows)
    _store(wired, _segments_for("Bob_K", {SID_B: MEMBER_B}))
    wired["state"]["groups"] = {SID_B: {MEMBER_B}}
    body = body_of(_get({"date": DATE, "user": "Bob_K"}))
    assert _spans(body) == [("10:55", "11:35"), ("17:57", "18:15")]


def test_a_merged_meetings_block_is_hidden_when_all_its_topics_are_redacted(wired):
    rows = [r if r["id"] != "t-b1" else dict(r, source_s3_key=_grp_key(SID_B)) for r in ROWS]
    _wire_rows(wired, rows)
    _store(wired)
    wired["state"]["groups"] = {SID_B: {MEMBER_B}}
    wired["state"]["redacted"] = {"t-b1": {"scope": "redacted"}}
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:57", "18:15")]


def test_a_legacy_whole_file_sessions_block_is_hidden_when_all_its_topics_are_excluded(wired):
    legacy_base = f"ada_l_{DATE}_19-26-40"
    legacy_row = _row(id="t-legacy", source_s3_key=f"extractions/Ada_L/{DATE}/{legacy_base}.json",
                      time_range="19:26 – 19:27", work_class="non_work")
    legacy_segment = {
        "start": 70000.0, "end": 70030.0, "session_id": None,
        "key": f"transcripts/Ada_L/{DATE}/{legacy_base}_off0.0_to30.0_srcwav.json",
    }
    _wire_rows(wired, ROWS + [legacy_row])
    _store(wired, SEGMENTS + [legacy_segment])
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "17:47"), ("17:57", "18:15")]


def test_a_merged_meeting_with_one_surviving_topic_still_shows_its_block(wired):
    rows = [r if r["id"] != "t-b1" else dict(r, source_s3_key=_grp_key(SID_B)) for r in ROWS]
    _wire_rows(wired, rows)
    _store(wired)
    wired["state"]["groups"] = {SID_B: {MEMBER_B}}
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "17:47"), ("17:57", "18:15")]
    assert body["recording_blocks"][1]["topic_row_ids"] == ["t-b1"]


def test_a_failing_blocks_read_still_serves_the_sessions(wired):
    _wire_rows(wired, ROWS)

    def broken(conn, user_id, report_date):
        raise RuntimeError("relation day_recording_segments does not exist")

    wired["mp"].setattr(org.day_recording_segments, "get", broken)
    res = _get(DAY)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert "recording_blocks" not in body and len(body["sessions"]) == 3
