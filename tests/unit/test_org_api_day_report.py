"""POST /days/{date}/report/preview and /days/{date}/report.

A day report is every reportable topic of one person's day, across meetings,
through the same scope core as a meeting report. Spec 2026-09-15 §5.1, §5.2.
"""
import json
import re
from datetime import datetime

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
DATE = "2026-07-25"
S1300 = "Benl1_2026-07-25_13-00-11"
S1405 = "Benl1_2026-07-25_14-05-00"
KEY_1300 = f"extractions/Ada_L/{DATE}/{S1300}.json"
KEY_1405 = f"extractions/Ada_L/{DATE}/{S1405}.json"
CALLER = {"id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
          "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _event(method, path, params=None, body=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": params,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


def _row(**over):
    base = {"id": "t-1", "site_id": SITE_ID, "site_name": "UC PK", "user_name": "Ada L",
            "source_s3_key": KEY_1300, "category": "progress", "title": "Slab pour",
            "summary": "Discussed the pour.", "time_range": "13:00 – 13:40",
            "participants": ["Ben"], "work_class": "work", "action_items": [],
            "safety_observations": [], "findings": [], "photos": []}
    base.update(over)
    return base


@pytest.fixture
def day(monkeypatch):
    puts = []
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org.users, "get_by_folder_name",
                        lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    monkeypatch.setattr(org.redactions, "list_active_for_topics", lambda conn, ids: {})
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes",
                        lambda conn, folder=None, date=None: [])
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org.recordings, "duration_for_media",
                        lambda conn, cid, folder, date, sb: None)
    monkeypatch.setattr(org, "s3", lambda: type("S", (), {
        "put_object": staticmethod(lambda **kw: puts.append(kw))})())
    return monkeypatch, puts


def _rows(mp, rows):
    mp.setattr(org.topics, "list_topics_for_source_prefix", lambda conn, prefix, **k: list(rows))


ROWS = [
    _row(id="t-late", source_s3_key=KEY_1300, title="Late in first meeting", time_range="13:30 – 13:40"),
    _row(id="t-second", source_s3_key=KEY_1405, title="Second meeting", time_range="14:05 – 14:20"),
    _row(id="t-early", source_s3_key=KEY_1300, title="Early in first meeting", time_range="13:05 – 13:10"),
    _row(id="t-personal", source_s3_key=KEY_1300, title="Personal", work_class="non_work"),
]

# A real F2SP chunk session: base is the bare `sid{32hex}` token (no timestamp
# to parse), and a multi-device merge: base is `grp{32hex}`, written under the
# LEAD's folder but pulled into a joiner's own day by exact key (a mirror).
CHUNK_HEX = "a" * 32
CHUNK_SID = f"sid{CHUNK_HEX}"
KEY_CHUNK = f"extractions/Ada_L/{DATE}/{CHUNK_SID}.json"

GRP_HEX = "b" * 32
GRP_SID = f"grp{GRP_HEX}"
KEY_GRP_ADA = f"extractions/Ada_L/{DATE}/{GRP_SID}.json"
KEY_GRP_BEN = f"extractions/Ben_UCPK/{DATE}/{GRP_SID}.json"


def _preview(params=None, body=None):
    return org.lambda_handler(_event("POST", f"/api/org/days/{DATE}/report/preview",
                                     params or {"user": "Ada_L"}, body), None)


def _generate(body, params=None):
    return org.lambda_handler(_event("POST", f"/api/org/days/{DATE}/report",
                                     params or {"user": "Ada_L"}, body), None)


def test_a_day_preview_spans_every_meeting_in_order(day):
    mp, _ = day
    _rows(mp, ROWS)
    res = _preview()
    assert res["statusCode"] == 200
    b = json.loads(res["body"])
    assert b["scope"] == "day"
    assert [t["topic_title"] for t in b["topics"]] == [
        "Early in first meeting", "Late in first meeting", "Second meeting"]
    assert b["sessionIds"] == [S1300, S1405]
    assert b["siteNames"] == ["UC PK"]
    assert b["title"] == f"{DATE} · UC PK"
    assert b["fieldDefaults"]["site"] == "UC PK"


def test_a_day_with_nothing_reportable_says_so(day):
    mp, _ = day
    _rows(mp, [_row(id="t-p", work_class="non_work")])
    res = _preview()
    assert res["statusCode"] == 404
    assert json.loads(res["body"])["error"] == "nothing to report for that day"


def test_a_bad_date_is_a_400(day):
    mp, _ = day
    _rows(mp, ROWS)
    res = org.lambda_handler(_event("POST", "/api/org/days/nope/report/preview", {"user": "Ada_L"}), None)
    assert res["statusCode"] == 400


def test_an_empty_selection_is_refused(day):
    mp, _ = day
    _rows(mp, ROWS)
    assert _preview(body={"topicRowIds": []})["statusCode"] == 400


def test_a_selection_narrows_the_day(day):
    mp, _ = day
    _rows(mp, ROWS)
    b = json.loads(_preview(body={"topicRowIds": ["t-second", "t-personal"]})["body"])
    assert [t["topic_title"] for t in b["topics"]] == ["Second meeting"]
    assert b["sessionIds"] == [S1405]


def test_generate_enqueues_a_day_artifact_under_the_day_segment(day):
    mp, puts = day
    _rows(mp, ROWS)
    res = _generate({"deliver": "download"})
    assert res["statusCode"] == 202
    b = json.loads(res["body"])
    assert b["scope"] == "day" and re.fullmatch(r"[0-9a-f]{32}", b["requestId"])
    assert b["resultKey"] == f"session_report_results/Ada_L/{DATE}/day/{b['requestId']}.json"
    assert len(puts) == 1
    assert puts[0]["Key"] == f"session_report_requests/Ada_L/{DATE}/day/{b['requestId']}.json"
    art = json.loads(puts[0]["Body"])
    assert art["scope"] == "day"
    assert art["sessionIds"] == [S1300, S1405]
    assert art["mirrorFolders"] == ["Ada_L"]
    assert art["resultKey"] == b["resultKey"]
    assert "sessionId" not in art


def test_email_without_recipients_is_refused_and_nothing_is_enqueued(day):
    mp, puts = day
    _rows(mp, ROWS)
    assert _generate({"deliver": "email"})["statusCode"] == 400
    assert puts == []


def test_a_failed_tombstone_lookup_enqueues_nothing(day):
    mp, puts = day
    _rows(mp, ROWS)

    def boom(conn, folder=None, date=None):
        raise RuntimeError("redactions unreadable")
    mp.setattr(org.redactions, "deleted_source_prefixes", boom)
    assert _generate({"deliver": "download"})["statusCode"] == 500
    assert puts == []


def test_the_folder_gate_is_the_media_one(day):
    mp, puts = day
    _rows(mp, ROWS)
    seen = {}

    def gate(conn, caller, user, what="media"):
        seen["what"] = what
        return None, org.error("not permitted to view this user's day report", 403)
    mp.setattr(org, "_resolve_org_media_folder", gate)
    assert _generate({"deliver": "download"})["statusCode"] == 403
    assert seen["what"] == "day report" and puts == []


# ----------------------------------------------------------
# Real F2SP recordings are chunk sessions (`sid{32hex}`) and merged meetings
# (`grp{32hex}`), never the legacy whole-file base every test above used.
# `build_day_sessions` takes a different path for those --
# `_chunk_session_start`/`_chunk_session_close` -> `meeting_session.get` --
# that no day-report test reached before this one, so an ordering/type
# mismatch between the DB-derived start and the legacy parsed start would
# ship silently.
# ----------------------------------------------------------

def test_a_chunk_session_orders_by_its_db_start_not_insertion_order(day):
    mp, puts = day
    mp.setattr(org.meeting_session, "get", lambda conn, sid: {
        # 2026-07-25T00:30 UTC -> 12:30 NZ (July = NZST, UTC+12) -- BEFORE the
        # legacy S1300 session's parsed 13:00:11 start. `_chunk_session_start`
        # reads this real column shape: a naive/aware datetime (psycopg
        # timestamptz), not a string, and converts UTC -> NZ itself.
        "opened_at": datetime(2026, 7, 25, 0, 30),
        "closed_at": datetime(2026, 7, 25, 0, 45),
    })
    rows = ROWS + [_row(id="t-chunk", source_s3_key=KEY_CHUNK,
                        title="Chunk session topic", time_range="12:30 – 12:45")]
    _rows(mp, rows)

    res = _preview()
    assert res["statusCode"] == 200
    b = json.loads(res["body"])
    # True time order across sessions, not the order ROWS/dict-insertion put
    # them in: the chunk session (12:30) sorts before the legacy one (13:00).
    assert [t["topic_title"] for t in b["topics"]] == [
        "Chunk session topic", "Early in first meeting",
        "Late in first meeting", "Second meeting"]
    assert b["sessionIds"] == [CHUNK_SID, S1300, S1405]

    gen = _generate({"deliver": "download"})
    assert gen["statusCode"] == 202
    art = json.loads(puts[0]["Body"])
    assert art["sessionIds"] == [CHUNK_SID, S1300, S1405]


def test_a_chunk_session_with_no_db_row_sorts_last_not_crashes(day):
    """`meeting_session.get` returning None (unknown/expired session) must not
    raise -- `_chunk_session_start` declines to None and the session sorts
    after every dated one, same as an unparseable legacy base."""
    mp, puts = day
    mp.setattr(org.meeting_session, "get", lambda conn, sid: None)
    rows = ROWS + [_row(id="t-chunk", source_s3_key=KEY_CHUNK,
                        title="Chunk session topic", time_range="? – ?")]
    _rows(mp, rows)
    res = _preview()
    assert res["statusCode"] == 200
    b = json.loads(res["body"])
    assert [t["topic_title"] for t in b["topics"]] == [
        "Early in first meeting", "Late in first meeting",
        "Second meeting", "Chunk session topic"]


def test_a_merged_meeting_mirrored_under_two_folders_counts_once(day):
    """A multi-device merge writes one topic set under the LEAD's folder, but
    a joiner's own day pulls those rows in by exact key alongside rows that
    live under their own folder (`_day_report_rows`) -- two different
    `parse_extraction_key` folders for the SAME `grp` session. `session_ref`
    keys purely on the session_base, so it must still collapse to one entry
    in `sessionIds`, and `mirrorFolders` must name both folders it drew rows
    from (the artifact's per-folder deletion-mirror check)."""
    mp, puts = day
    rows = [
        _row(id="t-grp-a", source_s3_key=KEY_GRP_ADA, title="Grp early", time_range="09:00 – 09:10"),
        _row(id="t-grp-b", source_s3_key=KEY_GRP_BEN, title="Grp late", time_range="09:10 – 09:20"),
    ]
    _rows(mp, rows)

    res = _preview()
    assert res["statusCode"] == 200
    b = json.loads(res["body"])
    assert [t["topic_title"] for t in b["topics"]] == ["Grp early", "Grp late"]
    assert b["sessionIds"] == [GRP_SID]

    gen = _generate({"deliver": "download"})
    assert gen["statusCode"] == 202
    art = json.loads(puts[0]["Body"])
    assert art["sessionIds"] == [GRP_SID]
    assert art["mirrorFolders"] == ["Ada_L", "Ben_UCPK"]
