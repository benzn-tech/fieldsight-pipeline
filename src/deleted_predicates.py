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

**Supersession is a third, separate condition**, added for Track B (migration 0073:
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

# {alias}.superseded_at — a topic's own supersession flag (migration 0073). A superseded
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


def visible_decision_records_predicate(alias: str = "d") -> str:
    """Ruling R7 (Track B Task 6b), corrected by Ruling R17 (fix round 1):
    decision_records has no topic_id column of its own -- a record's visibility depends
    on resolving its SUBJECT to the topic(s) that carry it and checking whether any of
    them is deletion-tombstoned.

    Deliberately TWO deletion arms, not three: the deletion tombstone (topic id AND
    source prefix, same reasoning as visible_topics_predicate above), but NEVER
    LIVE_TOPIC_PREDICATE. Task 6's whole point is that a decision record outlives the row
    it was about being superseded by a later extraction pass -- Track A's eval export
    (list_for_eval) must still show the record for a re-extracted topic, only an actual
    customer deletion hides it. Folding in the live arm here would make every
    re-extraction erase its own history the moment the newer pass writes fresh records,
    which is backwards: the whole reason Task 3 stopped DELETEing superseded topics was
    to keep exactly this kind of row alive.

    **Ruling R17 -- why this is EXISTS/NOT EXISTS over every carrier, not a scalar
    subquery picking one.** The first version of this predicate used `(SELECT x.topic_id
    FROM <table> x WHERE x.stable_id = subject_stable_id)` -- a SCALAR subquery, which
    silently assumed `stable_id` names at most one row. It does not: Task 4's
    carry-forward (`findings.carry_identity` / `action_items.carry_identity` /
    `_carry_forward_one_table` for topic_decisions/topic_questions) moves a stable_id
    FORWARD onto the new pass's row while the OLD, now-superseded row keeps it too -- one
    stable_id, two rows, two different topics, the moment a source is re-extracted once.
    Reviewer reproduced this against real Postgres: `CardinalityViolation: more than one
    row returned by a subquery used as an expression`, raised out of list_for_eval for
    the whole company the first time ANY finding/action_item/decision/question got
    carried forward.

    The semantics that replaces it: a non-topic record is visible iff (a) at least one
    row still carries that stable_id, AND (b) NONE of the carrying rows' topics is
    deletion-tombstoned. Deletion wins over supersession by design -- a recording delete
    tombstones every pass of it (both the live and the superseded rows, same source
    prefix), and a topic-level redaction of just the LIVE incarnation must still hide the
    record even though the superseded carrier's topic is untouched; showing it via the
    old row would leak exactly what the redaction was for. Supersession alone (neither
    carrier deleted) never hides -- unchanged from the original R7 ruling.

    subject_type 'topic': subject_stable_id IS the topic id directly (topics are not
    re-keyed -- decision_records.subject_stable_id's own column comment, migration 0073;
    a topic's own id is never carried onto another row, so this arm has no
    cardinality question to begin with). Every other subject_type names a CHILD table
    keyed by its OWN `stable_id`. A subject_stable_id with no carrier at all (or an
    unrecognised subject_type) matches no OR-branch below and is therefore NOT visible --
    fail-closed rather than showing an orphaned record nothing can attribute to a topic."""
    def _topic_not_deleted(topic_alias: str) -> str:
        return (f"({DELETED_TOPIC_PREDICATE.format(alias=topic_alias)} AND "
                f"{DELETED_SOURCE_PREDICATE.format(alias=topic_alias)})")

    topic_branch = (
        f"({alias}.subject_type = 'topic' AND EXISTS ("
        f"SELECT 1 FROM topics t WHERE t.id = {alias}.subject_stable_id "
        f"AND {_topic_not_deleted('t')}))"
    )

    def _child_branch(subject_type: str, table: str, child_alias: str, topic_alias: str) -> str:
        # (a) at least one carrier exists, AND (b) no carrier's topic is deletion-
        # tombstoned -- NOT EXISTS a carrier whose topic fails _topic_not_deleted, which
        # covers "deleted by topic id" OR "deleted by source prefix" without spelling
        # either out a second time.
        return (
            f"({alias}.subject_type = '{subject_type}' AND "
            f"EXISTS (SELECT 1 FROM {table} {child_alias} "
            f"WHERE {child_alias}.stable_id = {alias}.subject_stable_id) AND "
            f"NOT EXISTS (SELECT 1 FROM {table} {child_alias} "
            f"JOIN topics {topic_alias} ON {topic_alias}.id = {child_alias}.topic_id "
            f"WHERE {child_alias}.stable_id = {alias}.subject_stable_id "
            f"AND NOT {_topic_not_deleted(topic_alias)}))"
        )

    return "(" + " OR ".join([
        topic_branch,
        _child_branch("finding", "findings", "f", "tf"),
        _child_branch("action_item", "action_items", "ai", "tai"),
        _child_branch("decision", "topic_decisions", "td", "ttd"),
        _child_branch("question", "topic_questions", "tq", "ttq"),
    ]) + ")"
