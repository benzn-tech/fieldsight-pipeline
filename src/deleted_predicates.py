"""The SQL that hides a deleted recording, as strings and nothing else.

Spec: docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md

This module exists because `repositories/search_sql.py` carries a hard rule — "MUST NOT
import psycopg" — and `repositories/redactions.py` imports `psycopg.rows`. Search is one of
the surfaces a customer will check first ("不能再被别人搜出来"), so it needs these
predicates, and the alternative to a shared pure module is a second copy of the SQL living
somewhere else. This repo has already shipped a whole feature that did nothing because a
writer and a reader spelled the same key twice and drifted; the fix each time is one
definition, not a matching pair.

**Both arms or neither.** The tombstone has two, because they cover different moments:

* the **topic** arm covers the rows that exist right now;
* the **source** arm covers the rows the pipeline will re-create tomorrow, with new uuids
  that no topic-keyed tombstone names.

A read path that takes only the first passes every test written today and leaks overnight.
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


def visible_topics_predicate(alias: str = "t") -> str:
    """Both arms, ANDed. What a topic read path carries."""
    return (f"{DELETED_TOPIC_PREDICATE.format(alias=alias)} AND "
            f"{DELETED_SOURCE_PREDICATE.format(alias=alias)}")


def visible_chunks_predicate(alias: str = "c") -> str:
    """Both arms, ANDed, for the search/RAG chunk table.

    The source arm is the load-bearing one here: `lambda_ingest` re-creates a superseded
    day's topics with new uuids and the embedder re-chunks them, so a chunk written after
    the delete has a topic_id no tombstone names — but it still carries the deleted
    recording's `source_s3_key`.
    """
    return (f"{DELETED_CHUNK_TOPIC_PREDICATE.format(alias=alias)} AND "
            f"{DELETED_SOURCE_PREDICATE.format(alias=alias)}")
