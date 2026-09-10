"""Unit: a joiner's OWN timeline must show the meeting they were in.

A multi-device merge writes ONE topic set owned by the lead's session key and
`_delete_member_topics` PHYSICALLY deletes each member's own. `/live-items`
learned about this — `_merged_keys_for_caller` unions the merged artifact key
past the ACL — and `/timeline` never did.

The gate is the whole point, and it is why "just union the keys into the list
call" is not the fix:

    if not topics.has_topics_for_source_prefix(conn, prefix):
        return None

A joiner has zero rows under `extractions/{joiner}/{date}/` after the merge, so
this returns BEFORE any list call runs. Anything passed to
`list_topics_for_source_prefix` is unreachable on this path.

What the joiner gets instead is worse than an empty day. With the gate false the
handler falls through to the S3 `reports/{date}/{joiner}/daily_report.json`, and
the report generator has no knowledge of groups at all — it builds from
`transcripts/{joiner}/{date}/`, which the merge never touches. So after the
nightly run the joiner's timeline serves a plausible SINGLE-DEVICE account of a
meeting whose merged record their to-dos came from. A blank day gets reported; a
quietly divergent one does not.

`tests/unit/test_timeline_group_union.py` is named for this and does not test it:
every assertion there drives `list_topics_for_date`, which is the `/live-items`
query. `/timeline` does not call it.
"""
import json

import pytest

pytest.importorskip("psycopg", reason="org-api imports repositories at module load")

from tests.unit.test_lambda_org_api import (  # noqa: E402
    SITE_ID, _topic_row, body_of, make_event, org, presign_wired, wired,  # noqa: F401
)

JOINER = "Ben_UCPK2"
DATE = "2026-09-08"
GROUP = "AAA111"
MERGED_KEY = f"extractions/Lead_Folder/{DATE}/grp{GROUP}.json"


def _merged_row():
    """A row from the MERGED record: it carries the LEAD's identity, not the
    joiner's. That is the whole reason the union keys on source_s3_key."""
    return _topic_row(id="t-merged", title="Toolbox talk",
                      summary="Three crews, one meeting.",
                      user_name="Lead Person")


def _as_joiner(wired_mp, *, own_topics, merged_rows):
    """Wire one day for the joiner viewing their OWN timeline."""
    wired_mp.setattr(org.topics, "has_topics_for_source_prefix",
                     lambda conn, prefix: own_topics)
    wired_mp.setattr(org.topics, "list_topics_for_source_prefix",
                     lambda conn, prefix, **kw: list(merged_rows))
    wired_mp.setattr(org.sites, "list_company_sites",
                     lambda conn, cid, **kw: [{"id": SITE_ID}])
    wired_mp.setattr(org.users, "get_by_folder_name",
                     lambda conn, cid, folder: {"id": "u-joiner", "folder_name": folder})
    # The joiner IS in the group; the merged record has landed.
    wired_mp.setattr(org.meeting_session, "groups_for_user_on_date",
                     lambda conn, uid, date: [GROUP])
    wired_mp.setattr(org.session_group, "get",
                     lambda conn, gid: {"group_id": gid, "merged_key": MERGED_KEY})


def test_a_joiner_sees_the_merged_meeting_on_their_own_day(presign_wired):
    """THE test. Everything else in this file exists to stop it being satisfied
    the wrong way."""
    mp, _fake = presign_wired
    _as_joiner(mp, own_topics=False, merged_rows=[_merged_row()])

    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": DATE, "user": JOINER}), None)

    assert res["statusCode"] == 200, body_of(res)
    body = body_of(res)
    titles = [t.get("topic_title") for t in body.get("topics") or []]
    assert "Toolbox talk" in titles, (
        "the joiner's own topics were deleted by the merge; the merged record is "
        "the only account of the meeting and their day must show it")


def test_the_nightly_single_device_report_must_not_win(presign_wired):
    """The failure this replaces is not a 404 — it is a believable wrong answer.

    With the gate false the handler serves the S3 report verbatim. That document
    is built from the joiner's own transcript and knows nothing about the other
    devices, so it reads like a complete account of the meeting and is not one.
    """
    mp, fake = presign_wired
    _as_joiner(mp, own_topics=False, merged_rows=[_merged_row()])
    fake.objects[f"reports/{DATE}/{JOINER}/daily_report.json"] = json.dumps({
        "report_date": DATE, "site": "Alpha", "user_name": "Ben U",
        "executive_summary": "I attended a meeting.",
        "topics": [{"topic_id": 0, "topic_title": "What one device heard"}],
    }).encode()

    body = body_of(org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": DATE, "user": JOINER}), None))

    titles = [t.get("topic_title") for t in body.get("topics") or []]
    assert "What one device heard" not in titles, (
        "the single-device report must not be served over the merged record")
    assert "Toolbox talk" in titles


def test_a_day_with_no_group_is_untouched(presign_wired):
    """The gate still has to close. A day that is neither a member's nor has any
    topics keeps the S3 verbatim contract exactly as it does today."""
    mp, fake = presign_wired
    mp.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: False)
    mp.setattr(org.users, "get_by_folder_name",
               lambda conn, cid, folder: {"id": "u-joiner", "folder_name": folder})
    mp.setattr(org.meeting_session, "groups_for_user_on_date",
               lambda conn, uid, date: [])
    doc = {"report_date": DATE, "site": "Alpha", "user_name": "Ben U",
           "executive_summary": "Quiet day.", "topics": []}
    fake.objects[f"reports/{DATE}/{JOINER}/daily_report.json"] = json.dumps(doc).encode()

    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": DATE, "user": JOINER}), None)

    assert res["statusCode"] == 200
    assert body_of(res) == doc, "byte-verbatim passthrough must survive this change"


def test_a_group_whose_merge_has_not_landed_changes_nothing(presign_wired):
    """`session_group.merged_key` is written by the CLAIM, minutes before
    item-writer inserts anything — and forever, if the merge LLM call fails. A
    key that names no rows must behave exactly like no group at all, not like an
    empty topic list that suppresses the S3 fallback."""
    mp, fake = presign_wired
    _as_joiner(mp, own_topics=False, merged_rows=[])   # key resolves, no rows yet
    doc = {"report_date": DATE, "site": "Alpha", "user_name": "Ben U",
           "executive_summary": "Not merged yet.", "topics": []}
    fake.objects[f"reports/{DATE}/{JOINER}/daily_report.json"] = json.dumps(doc).encode()

    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": DATE, "user": JOINER}), None)

    assert res["statusCode"] == 200
    assert body_of(res) == doc


def test_the_key_is_read_never_re_derived(presign_wired):
    """A meeting can straddle NZ midnight: the joiner opens at 00:05 on D2 while
    the lead opened at 23:50 on D1, and `merged_key` is built from the LEAD's
    date. Reading `session_group.merged_key` is correct and survives that.
    Re-deriving `group_merged_key(folder, viewer_date, gid)` looks identical in
    review and produces a key for the wrong day.

    Pinned by making a re-derivation impossible to miss: the stored key names D1
    while the request asks for D2."""
    mp, _fake = presign_wired
    d1_key = f"extractions/Lead_Folder/2026-09-07/grp{GROUP}.json"
    seen = {}

    def _list(conn, prefix, **kw):
        seen["merged_keys"] = kw.get("merged_keys")
        return [_merged_row()]

    mp.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: False)
    mp.setattr(org.topics, "list_topics_for_source_prefix", _list)
    mp.setattr(org.sites, "list_company_sites", lambda conn, cid, **kw: [{"id": SITE_ID}])
    mp.setattr(org.users, "get_by_folder_name",
               lambda conn, cid, folder: {"id": "u-joiner", "folder_name": folder})
    mp.setattr(org.meeting_session, "groups_for_user_on_date",
               lambda conn, uid, date: [GROUP])
    mp.setattr(org.session_group, "get",
               lambda conn, gid: {"group_id": gid, "merged_key": d1_key})

    org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": DATE, "user": JOINER}), None)

    assert seen.get("merged_keys") == [d1_key], (
        "the stored key must be passed through verbatim, dates and all")
