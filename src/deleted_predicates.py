"""The SQL that hides a deleted or superseded recording, as strings and nothing else.

Spec: docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md
      docs/superpowers/specs/2026-09-24-event-graph-and-jev-assessment.md §2.1 (supersession)

This module exists because `repositories/search_sql.py` carries a hard rule — "MUST NOT
import psycopg" — and `repositories/redactions.py` imports `psycopg.rows`. Search is one of
the surfaces a customer will check first ("不能再被别人搜出来"), so it needs these
predicates, and the alternative to a shared pure module is a second copy of the SQL living
somewhere else. This repo has already shipped a whole feature that did nothing because a
writer and a reader spelled the same key twice and drifted; the fix each time is one
definition, not a matching pair.

**Both DELETION arms or neither.** The tombstone has two, because they cover different
moments:

* the **topic** arm covers the rows that exist right now;
* the **source** arm covers the rows the pipeline will re-create tomorrow, with new uuids
  that no topic-keyed tombstone names.

A read path that takes only the first passes every test written today and leaks overnight.

**Supersession is a third, separate condition**, added for Track B (migration 0071:
`topics.superseded_at`): re-extracting a source key no longer DELETEs its prior topics, it
marks them superseded and leaves the row (and its action_items/findings/chunks children) in
the table. A read that shows content must exclude those too, or two passes of the same
recording show side by side. It is orthogonal to the deletion tombstone above — a topic can
be superseded without ever being deleted, and vice versa — which is why it is a THIRD arm,
ANDed in by `visible_topics_predicate`/`visible_chunks_predicate`, not folded into either
existing one.
"""

# {alias}.id — for a table whose primary key IS the topic id (topics t).
DELETED_TOPIC_PREDICATE = (
    "NOT EXISTS (SELECT 1 FROM redactions r "
    "WHERE r.target_type = 'topic' AND r.target_id = {alias}.id "
    "AND r.scope = 'deleted' AND r.reverted_at IS NULL)"
)

# {alias}.source_s3_key LIKE the tombstoned prefix.
DELETED_SOURCE_PREDICATE = (
    "NOT EXISTS (SELECT 1 FROM redactions r "
    "WHERE r.target_type = 'recording' AND r.scope = 'deleted' "
    "AND r.reverted_at IS NULL AND r.target_key IS NOT NULL "
    "AND {alias}.source_s3_key LIKE r.target_key || '%%')"
)

# {alias}.topic_id — for a table that REFERENCES a topic (report_chunks c). Same rule,
# different column, and getting the column wrong is silent: the subquery simply never
# matches and every deleted row stays searchable.
DELETED_CHUNK_TOPIC_PREDICATE = (
    "NOT EXISTS (SELECT 1 FROM redactions r "
    "WHERE r.target_type = 'topic' AND r.target_id = {alias}.topic_id "
    "AND r.scope = 'deleted' AND r.reverted_at IS NULL)"
)

# {alias}.superseded_at — a topic's own supersession flag (migration 0071). A superseded
# topic is not deleted (no redaction row, nothing tombstoned) — it is the STALE half of one
# source key's two passes, kept in the table on purpose so Track B can carry stable ids and
# decision-record provenance across a re-extraction instead of destroying them. A read that
# only checked the deletion arms above would show both passes side by side the moment Task 3
# stops DELETEing the old pass.
LIVE_TOPIC_PREDICATE = "{alias}.superseded_at IS NULL"

# The complement of LIVE_TOPIC_PREDICATE, for the rare query that has to name the superseded
# rows AFFIRMATIVELY rather than exclude them — e.g. building an exclusion-id set. Kept here,
# not inlined at the call site, for the same reason every other predicate in this module is:
# one definition instead of a second copy that drifts.
SUPERSEDED_TOPIC_PREDICATE = "{alias}.superseded_at IS NOT NULL"

# A CHILD table's exclusion correlates on the child's OWN `topic_id`, never on a `topics`
# alias -- `FROM action_items` and `FROM topic_photos` have no such alias in scope, and
# Postgres resolves that at analysis time: `missing FROM-clause entry for table "t"`, every
# call, not a weak filter but a crash. Two inlined copies named an alias that did not exist
# in their own statement and were `missing FROM-clause entry`, every call, until a real
# database was finally asked -- see tests/integration/test_topic_reads_execute.py.
#
# Both a deletion tombstone AND supersession exclude a child: the second EXISTS names the
# child's own topic by id and checks it is still live, mirroring visible_chunks_predicate's
# EXISTS shape for report_chunks.topic_id -- the same "does the parent still count" check,
# reused rather than re-derived.
CHILD_OF_VISIBLE_TOPIC = (
    "NOT EXISTS (SELECT 1 FROM redactions r WHERE r.target_type = 'topic' "
    "AND r.target_id = {alias}.topic_id AND r.scope = 'deleted' "
    "AND r.reverted_at IS NULL) "
    "AND EXISTS (SELECT 1 FROM topics t WHERE t.id = {alias}.topic_id "
    "AND t.superseded_at IS NULL)"
)

# The DELETION half of CHILD_OF_VISIBLE_TOPIC, alone -- no live arm. For the rare child read
# that must survive supersession while still honouring a deletion tombstone: R3's
# get_topic_full (the per-topic reindex builder) is unconditionally unfiltered for
# supersession on its topic row and its action_items/safety_observations/findings children,
# but its topic_photos child had been quietly filtered on deletion since before this task
# and reused CHILD_OF_VISIBLE_TOPIC when that constant grew the live arm -- silently pulling
# get_topic_full's photos out from under R3's own "stays unfiltered" ruling. This predicate
# is what topic_photos should carry there: still hidden from a customer-facing delete
# (get_topic_full's caller does not itself re-check that), but visible on a topic that is
# merely superseded, matching every other child on that read.
CHILD_OF_UNDELETED_TOPIC = (
    "NOT EXISTS (SELECT 1 FROM redactions r WHERE r.target_type = 'topic' "
    "AND r.target_id = {alias}.topic_id AND r.scope = 'deleted' "
    "AND r.reverted_at IS NULL)"
)


def visible_topics_predicate(alias: str = "t") -> str:
    """All three arms, ANDed. What a topic read path carries: not deleted (by topic id),
    not deleted (by source prefix), and not superseded by a later pass of the same source."""
    return (f"{DELETED_TOPIC_PREDICATE.format(alias=alias)} AND "
            f"{DELETED_SOURCE_PREDICATE.format(alias=alias)} AND "
            f"{LIVE_TOPIC_PREDICATE.format(alias=alias)}")


def visible_chunks_predicate(alias: str = "c") -> str:
    """All three arms, ANDed, for the search/RAG chunk table.

    The source arm is load-bearing for deletion: `lambda_ingest` re-creates a superseded
    day's topics with new uuids and the embedder re-chunks them, so a chunk written after
    the delete has a topic_id no tombstone names — but it still carries the deleted
    recording's `source_s3_key`.

    The third arm is load-bearing for supersession, and for the OPPOSITE reason: a chunk's
    `topic_id` is `ON DELETE SET NULL`, so once Task 3 stops deleting the superseded topic
    row there is no NULL to catch — the chunk keeps pointing at a topic that still exists but
    must no longer be shown. `topic_id IS NULL` chunks (the unassigned transcript windows)
    are unaffected by either arm and stay visible.
    """
    return (f"{DELETED_CHUNK_TOPIC_PREDICATE.format(alias=alias)} AND "
            f"{DELETED_SOURCE_PREDICATE.format(alias=alias)} AND "
            f"(EXISTS (SELECT 1 FROM topics t WHERE t.id = {alias}.topic_id "
            f"AND t.superseded_at IS NULL) OR {alias}.topic_id IS NULL)")
