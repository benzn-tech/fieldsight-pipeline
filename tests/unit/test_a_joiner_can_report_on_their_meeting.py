"""Unit: a joiner can find, preview and report on the meeting they were in.

A multi-device merge writes ONE topic set under the LEAD's folder
(`extractions/{lead}/{date}/grp{id}.json`) and physically deletes each member's
own topics. `/timeline` and `/live-items` learned this through `_merged_keys_for`.
The meeting picker (`GET /sessions`) and the report route
(`POST /sessions/{id}/report[/preview]`) never did: both listed only
`extractions/{folder}/{date}/`.

So for a joiner, on TEST and prod:

  * the picker did not show the meeting they were in;
  * previewing or generating its report returned 404 "session not found";
  * the lead was unaffected, by coincidence -- the merged key happens to live
    under the lead's own prefix.

That is the route the per-meeting topic selection rides on, so the selection
could never be used by anyone who was not the lead.

The clipping rule is taken from `_render_timeline_for_user`, not invented here:

  * reading your OWN day, a merged row may pass the site clip -- you were in that
    meeting, so the record is yours, even when the lead recorded on a site you
    are not a member of;
  * reading SOMEONE ELSE's day, merged rows stay inside your site clip. A merged
    meeting spans devices and therefore sites by definition, which is exactly
    what the cross-user clip exists for.

And the membership lookup asks about the person whose day is being read, never
the person reading it (`_timeline_target_id`), so being in a meeting yourself
does not make it appear on a colleague's day.
"""
import pytest

pytest.importorskip("psycopg", reason="org-api imports repositories at module load")

from tests.unit.test_org_api_sessions import (  # noqa: E402
    OTHER_SITE_ID, SITE_ID, _get, _preview, _row, body_of, org, wired,  # noqa: F401
)

DATE = "2026-07-25"
GROUP = "a" * 32  # a real groupId is always 32 hex chars (see lambda_org_api._SID_RE)
SESSION = f"grp{GROUP}"
MERGED_KEY = f"extractions/Lead_Folder/{DATE}/{SESSION}.json"
OWN = {"date": DATE, "user": "Ada_L"}            # CALLER's own folder
OTHER = {"date": DATE, "user": "Joiner_Two"}     # somebody else's folder -> "u-2"


def _merged(**over):
    base = dict(id="t-merged", source_s3_key=MERGED_KEY, title="Toolbox talk",
                user_name="Lead Person", site_id=SITE_ID)
    base.update(over)
    return _row(**base)


def _world(wired_mp, *, own_rows=(), merged_rows=(), members=("u-uuid-1",),
           group_lookup_raises=False, calls=None):
    """One day. `members` are the user ids who were in the merged meeting."""
    own_rows, merged_rows = list(own_rows), list(merged_rows)

    def _list(conn, prefix, merged_keys=None):
        if calls is not None:
            calls.append({"prefix": prefix, "merged_keys": merged_keys})
        rows = [r for r in own_rows if r["source_s3_key"].startswith(prefix)]
        if merged_keys:
            rows += [r for r in merged_rows if r["source_s3_key"] in merged_keys]
        return rows

    def _groups(conn, user_id, date):
        if group_lookup_raises:
            raise RuntimeError("database unavailable")
        return [GROUP] if user_id in members else []

    wired_mp.setattr(org.topics, "list_topics_for_source_prefix", _list)
    wired_mp.setattr(org.meeting_session, "groups_for_user_on_date", _groups)
    wired_mp.setattr(org.session_group, "get",
                     lambda conn, gid: {"group_id": gid, "merged_key": MERGED_KEY})


def _session_ids(res):
    assert res["statusCode"] == 200, body_of(res)
    return [s["session_id"] for s in body_of(res)["sessions"]]


# ---- the defect ------------------------------------------------------------

def test_a_joiners_picker_lists_the_meeting_they_were_in(wired):
    """THE picker test. Before the fix this list was empty."""
    _world(wired, merged_rows=[_merged()])
    assert _session_ids(_get(OWN)) == [SESSION]


def test_a_joiner_can_preview_the_meeting_they_were_in(wired):
    """THE report test. Before the fix this was a 404."""
    _world(wired, merged_rows=[_merged()])
    res = _preview(SESSION, OWN)
    assert res["statusCode"] == 200, body_of(res)
    assert [t["topic_title"] for t in body_of(res)["topics"]] == ["Toolbox talk"]


def test_the_joiners_other_recordings_that_day_are_still_there(wired):
    """A merge removes the joiner's copy of THAT meeting, not their whole day."""
    own = _row(id="t-own", source_s3_key=f"extractions/Ada_L/{DATE}/Benl1_2026-07-25_14-05-00.json",
               title="Afternoon walk")
    _world(wired, own_rows=[own], merged_rows=[_merged()])
    assert set(_session_ids(_get(OWN))) == {SESSION, "Benl1_2026-07-25_14-05-00"}


# ---- the clip rule, copied from the timeline --------------------------------

def test_own_day_a_merged_meeting_on_another_site_is_still_yours(wired):
    """The lead recorded on a site the joiner is not a member of. The joiner was
    in the room; the record is theirs."""
    _world(wired, merged_rows=[_merged(site_id=OTHER_SITE_ID)])
    assert _session_ids(_get(OWN)) == [SESSION]
    assert _preview(SESSION, OWN)["statusCode"] == 200


def test_someone_elses_merged_meeting_stays_inside_your_site_clip(wired):
    """Viewing a colleague's day must not reveal an out-of-scope site's meeting
    through their group membership. Same 404 as any unknown session."""
    _world(wired, merged_rows=[_merged(site_id=OTHER_SITE_ID)], members=("u-2",))
    assert _session_ids(_get(OTHER)) == []
    assert _preview(SESSION, OTHER)["statusCode"] == 404


def test_someone_elses_merged_meeting_on_your_site_is_visible(wired):
    """The positive half: the lookup resolved the TARGET's membership, and the
    clip is the only thing that decides."""
    _world(wired, merged_rows=[_merged()], members=("u-2",))
    assert _session_ids(_get(OTHER)) == [SESSION]
    assert _preview(SESSION, OTHER)["statusCode"] == 200


def test_being_in_a_meeting_yourself_does_not_put_it_on_a_colleagues_day(wired):
    """Membership is asked about the person whose day it is. The caller was in
    the meeting; the colleague was not."""
    _world(wired, merged_rows=[_merged()], members=("u-uuid-1",))
    assert _session_ids(_get(OTHER)) == []
    assert _preview(SESSION, OTHER)["statusCode"] == 404


# ---- exclusions still apply to merged rows ---------------------------------

def test_a_merged_personal_conversation_is_still_excluded(wired):
    _world(wired, merged_rows=[_merged(work_class="non_work")])
    assert _session_ids(_get(OWN)) == []
    assert _preview(SESSION, OWN)["statusCode"] == 404


def test_a_merged_redacted_topic_is_still_excluded(wired):
    _world(wired, merged_rows=[_merged()])
    wired.setattr(org.redactions, "list_active_for_topics",
                  lambda conn, ids: {"t-merged": {"id": "r-1"}})
    assert _preview(SESSION, OWN)["statusCode"] == 404


# ---- nothing changes for a day with no merge --------------------------------

def test_a_failed_group_lookup_leaves_the_day_as_it_was(wired):
    """A missing merged record is a stale view; a raised one must not become no
    view at all."""
    own = _row(id="t-own", source_s3_key=f"extractions/Ada_L/{DATE}/Benl1_2026-07-25_13-00-11.json")
    _world(wired, own_rows=[own], group_lookup_raises=True)
    assert _session_ids(_get(OWN)) == ["Benl1_2026-07-25_13-00-11"]


def test_no_group_means_the_list_call_is_exactly_what_it_was(wired):
    """No merged_keys argument at all when there is nothing to union -- every
    existing stub and query path stays on the call it always made."""
    calls = []
    own = _row(id="t-own", source_s3_key=f"extractions/Ada_L/{DATE}/Benl1_2026-07-25_13-00-11.json")
    _world(wired, own_rows=[own], members=(), calls=calls)
    _get(OWN)
    _preview("Benl1_2026-07-25_13-00-11", OWN)
    assert calls and all(c["merged_keys"] is None for c in calls), calls
