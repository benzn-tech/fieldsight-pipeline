"""Unit: a member sees the merged record on their own timeline (Phase C, Task 7).

The merge deletes each member's own topics and writes ONE set owned by the lead.
Without a union the joiners' day goes blank — they contributed to the meeting and
their timeline shows nothing.

The union key is the merged artifact's source_s3_key, NOT the lead's identity.
Keying on the lead fails three ways, all of them silent:

  * a graded-role member has author_ids active, which filters merged topics out
    (they carry the LEAD's user_id, not theirs);
  * a member without membership on the LEAD's site is excluded by the site
    filter;
  * adding the lead's user_id to author_ids would leak the lead's OTHER solo
    topics that day to every member.

source_s3_key names exactly the merged rows and nothing else.
"""
import pytest

from tests.unit.test_meeting_session_repo import FakeConn

t = pytest.importorskip("repositories.topics", reason="requires psycopg (installed in CI)")
ms = pytest.importorskip("repositories.meeting_session")

MERGED = "extractions/Lead_Folder/2026-08-07/grpAAA.json"


def test_merged_keys_are_unioned_not_filtered():
    conn = FakeConn(results=[[], [], [], []])
    t.list_topics_for_date(conn, ["s1"], "2026-08-07",
                           author_ids=["u2"], merged_keys=[MERGED])
    sql = conn.calls[0]["sql"]
    where = sql[sql.index("WHERE"):]
    assert "t.source_s3_key = ANY(" in where
    assert " OR " in where, "merged topics must be OR-ed in, not AND-ed away"
    assert MERGED in conn.calls[0]["params"][-1]


def test_the_union_survives_the_author_filter():
    # A graded-role member has author_ids active. The merged topics carry the
    # LEAD's user_id, so without the OR they are filtered out for everyone
    # except the lead — the exact case this exists for.
    conn = FakeConn(results=[[], [], [], []])
    t.list_topics_for_date(conn, ["s1"], "2026-08-07",
                           author_ids=["member-uuid"], merged_keys=[MERGED])
    sql = conn.calls[0]["sql"]
    author_clause = "t.user_id = ANY(%s::uuid[])"
    assert author_clause in sql
    where = sql[sql.index("WHERE"):]
    assert where.index("t.source_s3_key") > where.index(author_clause), \
        "the union must wrap the author filter, not precede it"


def test_no_merged_keys_leaves_todays_query_untouched():
    # The overwhelmingly common path. A solo day must not pay for this.
    conn = FakeConn(results=[[], [], [], []])
    t.list_topics_for_date(conn, ["s1"], "2026-08-07")
    sql = conn.calls[0]["sql"]
    assert "t.source_s3_key = ANY(" not in sql[sql.index("WHERE"):]


def test_empty_merged_keys_is_the_same_as_none():
    conn = FakeConn(results=[[], [], [], []])
    t.list_topics_for_date(conn, ["s1"], "2026-08-07", merged_keys=[])
    sql = conn.calls[0]["sql"]
    assert "t.source_s3_key = ANY(" not in sql[sql.index("WHERE"):]


def test_groups_for_user_on_date_includes_the_LEAD():
    # THE trap. `WHERE group_id IS NOT NULL` reads correctly and is wrong: the
    # lead's group_id is NULL by design, so that filter would make the person
    # holding the meeting the one member who loses the merged record.
    conn = FakeConn(results=[[]])
    ms.groups_for_user_on_date(conn, "u-1", "2026-08-07")
    sql = conn.calls[0]["sql"]
    assert "m.user_id = %s" in sql
    assert "FROM session_group g" in sql, \
        "session_group is the authority on what a group is"
    assert "m.group_id = g.group_id OR m.session_id = g.group_id" in sql, \
        "the lead is matched by its session_id, the joiners by their group_id"


def test_a_failed_group_lookup_does_not_500_the_timeline():
    """The enrichment must never cost the read.

    Found by breaking it: the resolver was first called inside the
    list_topics_for_date(...) argument list, where an exception escapes the
    callee's try entirely — every /live-items request 500ed. A missing merged
    record is a stale timeline; an exception is no timeline at all."""
    org = pytest.importorskip("lambda_org_api", reason="requires psycopg")

    class _Boom:
        def cursor(self, **kw):
            raise RuntimeError("db down")

    assert org._merged_keys_for_caller(_Boom(), {"id": "u-1"}, "2026-08-07") == []


def test_groups_for_user_uses_the_NZ_day():
    # opened_at is UTC; the timeline's date is the device's NZ day. Comparing
    # them raw is BUG-37, which already produced a "No summary" once.
    conn = FakeConn(results=[[]])
    ms.groups_for_user_on_date(conn, "u-1", "2026-08-07")
    assert "Pacific/Auckland" in conn.calls[0]["sql"]
