"""Unit: item-writer's half of the merge (Phase C, Task 6).

Four failures, every one of them silent in production:

  * A `grp` base falls through every rung of the site ladder — site_for_media
    matches on the media filename, _site_from_meeting_session's
    device_session_id only recognises `sid`, and an admin/gm lead has no
    recordings row. The result is "identity bridge miss ... zero writes": the
    merge discarded, AFTER the members' topics were deleted.

  * The member deletes are keyed on source_s3_key and
    delete_topics_for_source returns a rowcount rather than raising. A key that
    differs by one character removes nothing and leaves the duplicate the merge
    exists to eliminate, with no error anywhere.

  * Suppression must compare COVERAGE, not timing. "Anything written after the
    merge" fires on the lead's own final pass — which the sweep requested
    BEFORE the merge ran — so every group would re-merge and re-email once in
    the completely ordinary case, and the cap would be spent before a genuinely
    late device arrived.

  * The updated-email requests must carry ONE summary. lambda_session_finalize
    re-derives its own from that member's solo transcripts, so N members would
    otherwise get N different bodies at the cost of N LLM calls.
"""
import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")

GID = "a" * 32
JOINER = "b" * 32
MERGED_KEY = f"extractions/Ben_UCPK/2026-08-07/grp{GID}.json"


def test_a_grp_base_is_not_a_device_session():
    # Today's behaviour, pinned: this is WHY the ladder needs a new rung.
    assert iw._device_session_id("grp" + GID) is None
    assert iw._device_session_id("sid" + GID) == GID


def test_the_group_id_is_recoverable_from_a_grp_base():
    assert iw._group_id_from_base("grp" + GID) == GID
    assert iw._group_id_from_base("sid" + GID) is None
    assert iw._group_id_from_base("") is None


def test_a_grp_base_resolves_its_site_from_the_lead_session(monkeypatch):
    monkeypatch.setattr(iw.meeting_session, "get",
                        lambda conn, sid: {"site_id": "site-1"} if sid == GID else None)
    monkeypatch.setattr(iw.sites, "get_site",
                        lambda conn, sid: {"id": "site-1", "company_id": "co-1"})
    site = iw._site_from_group_lead(object(), "co-1", "grp" + GID)
    assert site["id"] == "site-1"


def test_a_grp_site_from_another_company_is_refused(monkeypatch):
    # Same tenant re-check _site_from_meeting_session does: a stale or rogue row
    # must never attribute across tenants.
    monkeypatch.setattr(iw.meeting_session, "get", lambda conn, sid: {"site_id": "site-1"})
    monkeypatch.setattr(iw.sites, "get_site",
                        lambda conn, sid: {"id": "site-1", "company_id": "OTHER"})
    assert iw._site_from_group_lead(object(), "co-1", "grp" + GID) is None


def test_every_member_key_is_deleted(monkeypatch):
    deleted = []
    art = {"tier": "group", "groupId": GID,
           "mergedMembers": [f"extractions/A/2026-08-07/sid{GID}.json",
                             f"extractions/B/2026-08-08/sid{JOINER}.json"]}
    iw._delete_member_topics(object(), art, delete=lambda conn, k: deleted.append(k) or 3)
    assert deleted == art["mergedMembers"]


def test_a_delete_that_removed_nothing_is_logged_loudly(monkeypatch, caplog):
    art = {"tier": "group", "groupId": GID,
           "mergedMembers": [f"extractions/A/2026-08-07/sid{GID}.json"]}
    with caplog.at_level("WARNING"):
        iw._delete_member_topics(object(), art, delete=lambda conn, k: 0)
    assert "removed 0" in caplog.text, \
        "a delete that matched nothing must be loud — the duplicate survives silently"


def test_a_solo_extraction_already_covered_by_the_merge_brings_nothing_new():
    merged = {"source_transcripts": ["t1.json", "t2.json"]}
    solo = {"source_transcripts": ["t1.json"]}
    assert iw._brings_new_content(solo, merged) is False


def test_a_genuinely_late_member_brings_new_content():
    merged = {"source_transcripts": ["t1.json"]}
    solo = {"source_transcripts": ["t1.json", "t3.json"]}
    assert iw._brings_new_content(solo, merged) is True


def test_an_unreadable_merged_artifact_is_treated_as_covering_nothing():
    # Erring towards re-merging rather than towards silently dropping a late
    # device: a wasted merge costs an email, a dropped one costs content.
    assert iw._brings_new_content({"source_transcripts": ["t1.json"]}, None) is True


def test_the_todos_are_dicts_not_strings():
    # _clean_todos in the email renderer does t.get("text"); a list of strings
    # raises AttributeError and takes the whole email with it. The solo path
    # gets this shape from the rolling summary, which a merged artifact does
    # not have.
    art = {"topics": [{"action_items": [
        {"action": "PPE compliance", "responsible": "Unidentified worker",
         "deadline": "Immediate"}]}]}
    todos = iw._todos_from_topics(art)
    assert todos == [{"text": "PPE compliance",
                      "responsible": "Unidentified worker", "due": "Immediate"}]


def test_todos_from_a_merge_with_no_action_items_is_empty():
    assert iw._todos_from_topics({"topics": [{"action_items": []}]}) == []
    assert iw._todos_from_topics({}) == []


def test_one_updated_request_per_member_all_with_the_same_summary():
    written = []
    art = {"tier": "group", "groupId": GID, "summary": "One shared summary.",
           "topics": [], "memberSessions": [GID, JOINER]}
    iw._enqueue_updated_emails(art, put=lambda key, body: written.append((key, body)))
    assert [k for k, _ in written] == [
        f"session_finalize_requests/{GID}-updated.json",
        f"session_finalize_requests/{JOINER}-updated.json"]
    assert {b["summary"] for _, b in written} == {"One shared summary."}
    assert {b["kind"] for _, b in written} == {"updated"}
    assert {b["groupId"] for _, b in written} == {GID}


def _group_row(merged_at="now", count=1, key=MERGED_KEY, result=None):
    return {"group_id": GID, "merged_at": merged_at, "merge_count": count,
            "merged_key": key, "merge_result": result}


def test_a_members_solo_write_is_suppressed_once_the_group_has_merged(monkeypatch):
    # The lead's own final pass is requested by the sweep BEFORE the merge runs,
    # so it routinely lands afterwards. Writing it would reintroduce exactly the
    # duplicate the merge just removed.
    monkeypatch.setattr(iw, "_group_for_session", lambda conn, sb: _group_row())
    monkeypatch.setattr(iw, "_read_merged_artifact",
                        lambda key: {"source_transcripts": ["t1.json"]})
    decision = iw._group_supersedes_solo(
        object(), "sid" + GID, {"source_transcripts": ["t1.json"]})
    assert decision == "suppress"


def test_a_genuinely_late_member_re_arms_instead_of_being_dropped(monkeypatch):
    rearmed = []
    monkeypatch.setattr(iw, "_group_for_session", lambda conn, sb: _group_row())
    monkeypatch.setattr(iw, "_read_merged_artifact",
                        lambda key: {"source_transcripts": ["t1.json"]})
    monkeypatch.setattr(iw.session_group, "rearm",
                        lambda conn, gid: rearmed.append(gid) or True)
    decision = iw._group_supersedes_solo(
        object(), "sid" + JOINER, {"source_transcripts": ["t1.json", "t9.json"]})
    assert decision == "suppress" and rearmed == [GID], \
        "late content must trigger a re-merge, not be written as a duplicate"


def test_past_the_cap_late_content_is_written_rather_than_lost(monkeypatch):
    # Without a cap a device drip-feeding chunks would re-merge and re-email all
    # day. Past it the content is still written -- only its inclusion in the
    # merged record is lost, never the content itself.
    monkeypatch.setattr(iw, "_group_for_session", lambda conn, sb: _group_row(count=2))
    monkeypatch.setattr(iw, "_read_merged_artifact",
                        lambda key: {"source_transcripts": ["t1.json"]})
    decision = iw._group_supersedes_solo(
        object(), "sid" + JOINER, {"source_transcripts": ["t1.json", "t9.json"]})
    assert decision == "write"


def test_a_session_in_no_group_is_never_suppressed(monkeypatch):
    monkeypatch.setattr(iw, "_group_for_session", lambda conn, sb: None)
    assert iw._group_supersedes_solo(object(), "sid" + GID, {}) == "write"


def test_an_unmerged_group_does_not_suppress_its_members(monkeypatch):
    # Before the merge runs, members write normally — that is what gives each
    # person their own timely record.
    monkeypatch.setattr(iw, "_group_for_session",
                        lambda conn, sb: _group_row(merged_at=None))
    assert iw._group_supersedes_solo(object(), "sid" + GID, {}) == "write"


def test_the_merged_artifact_itself_is_never_suppressed(monkeypatch):
    monkeypatch.setattr(iw, "_group_for_session", lambda conn, sb: _group_row())
    assert iw._group_supersedes_solo(object(), "grp" + GID, {}) == "write", \
        "the merge must not suppress itself"


def test_the_flag_being_off_leaves_every_group_branch_inert(monkeypatch):
    # prod ships with the flag off. Nothing in this file may run there.
    monkeypatch.setattr(iw, "ENABLE_GROUP_MERGE", False)
    monkeypatch.setattr(iw, "_group_supersedes_solo",
                        lambda *a: pytest.fail("suppression ran with the flag off"))
    assert iw.ENABLE_GROUP_MERGE is False


def test_the_group_id_is_read_from_the_lead_when_a_member_has_no_group_id(monkeypatch):
    # The LEAD carries no group_id of its own — the group id IS its session id.
    # Reading row["group_id"] alone would return None for the lead and its own
    # solo final would never be suppressed.
    monkeypatch.setattr(iw.meeting_session, "get",
                        lambda conn, sid: {"group_id": None})
    seen = []
    monkeypatch.setattr(iw.session_group, "get",
                        lambda conn, gid: seen.append(gid) or None)
    iw._group_for_session(object(), "sid" + GID)
    assert seen == [GID], "a lead's own session id is its group id"


def test_the_updated_result_key_cannot_collide_with_the_solo_one():
    # reconcile reads session_finalize_results/{sessionId}.json to settle a
    # claimed session. A member can be counted settled by quietness while still
    # `finalizing`, so an updated-email result landing on the same key could be
    # read as the solo outcome.
    written = []
    iw._enqueue_updated_emails(
        {"groupId": GID, "memberSessions": [GID]},
        put=lambda key, body: written.append(key))
    assert written[0].endswith("-updated.json")


# ---- mark_result must run while the connection is still OPEN -----------

def test_mark_result_is_inside_the_connection_block():
    """It was outside it, and that made every successful merge look stuck.

    psycopg3's `with conn:` CLOSES the connection on exit -- db/connection.py
    says so in its own docstring. `mark_result` sat after the block, so it
    raised on every merge, was swallowed by a blanket except, and was mis-logged
    as an email failure. merge_result stayed NULL while merged_at was set, which
    is precisely the signature the stuck-group recovery matches: every
    successful merge would have been re-merged, re-emailed, and finally marked
    `failed` with a log line claiming the members still had their own topics --
    which by then they did not.

    The unit suite could not see any of this: FakeConn does not close on exit,
    so the call that always failed in production always succeeded in tests.
    This checks the structural property instead, by source position -- the
    behavioural version would need a connection double that closes, which is a
    fair thing to want but a much larger change to every test in this file.
    """
    import inspect
    import lambda_item_writer as iw

    src = inspect.getsource(iw.write_extraction_items)
    lines = src.splitlines()
    open_at = next(i for i, l in enumerate(lines) if "with get_connection() as conn" in l)
    indent = len(lines[open_at]) - len(lines[open_at].lstrip())
    mark_at = next(i for i, l in enumerate(lines) if "mark_result(" in l)
    assert mark_at > open_at, "mark_result must come after the block opens"

    # Find where the block ends: the first line at or below the `with`'s own
    # indentation after it.
    end_at = next(i for i in range(open_at + 1, len(lines))
                  if lines[i].strip() and
                  (len(lines[i]) - len(lines[i].lstrip())) <= indent)
    assert mark_at < end_at, (
        "mark_result runs on a CLOSED connection — it must be inside the "
        "`with get_connection()` block, not after it")


def test_a_failed_email_enqueue_no_longer_hides_a_failed_mark_result():
    # The two were in one try/except, so a mark_result failure was reported as
    # an email failure -- which is how this survived: the log line named the
    # wrong subsystem.
    import inspect
    import lambda_item_writer as iw
    src = inspect.getsource(iw.write_extraction_items)
    block = src[src.index("_enqueue_updated_emails(extraction)"):]
    assert "mark_result" not in block[:400], \
        "mark_result must not share the email try/except — it mis-attributes the failure"
